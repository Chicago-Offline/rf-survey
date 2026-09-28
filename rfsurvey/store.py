"""SQLite observation store. Append-only; receiver-tagged; location-tagged."""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sweeps (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  receiver TEXT NOT NULL,
  plan TEXT NOT NULL,
  start_hz INTEGER, stop_hz INTEGER, step_hz INTEGER,
  lat REAL, lon REAL, alt_m REAL, fix TEXT
);
CREATE TABLE IF NOT EXISTS bins (
  sweep_id INTEGER NOT NULL REFERENCES sweeps(id),
  freq_hz INTEGER NOT NULL,
  db REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS bins_freq ON bins(freq_hz);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  receiver TEXT NOT NULL,
  freq_hz INTEGER NOT NULL,
  snr_db REAL,
  duration_s REAL,
  decoder TEXT,
  gated INTEGER DEFAULT 0,          -- 1 = energy gate confirmed activity
  meta TEXT,                        -- decoder-specific JSON (CC, TGs, RIDs...)
  lat REAL, lon REAL, alt_m REAL, fix TEXT
);
CREATE INDEX IF NOT EXISTS obs_freq ON observations(freq_hz);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY,
  created REAL NOT NULL,
  acked INTEGER DEFAULT 0,          -- 1 = broker confirmed receipt
  payload TEXT NOT NULL             -- signed envelope JSON, for retransmit
);
-- Beacon calibration readings (NETWORK.md S7).  Keyed by OBSERVER
-- (receiver), never by station: two dongles on one host have different
-- antennas, gains and front ends, and averaging them smears exactly the
-- hardware variance these readings exist to detect.
CREATE TABLE IF NOT EXISTS beacon_readings (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  receiver TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  freq_hz INTEGER NOT NULL,
  band TEXT,
  coverage TEXT NOT NULL,           -- ok | no_reference | unverified
  signal_db REAL,                   -- absolute: catches a front end going deaf
  noise_db REAL,
  snr_db REAL,                      -- survives a gain change; baseline metric
  gain REAL,
  pinned INTEGER DEFAULT 0,         -- 1 = gain pinned; unpinned never baselines
  status TEXT NOT NULL,             -- ok | not_heard | no_reference | error
  batch_id TEXT
);
CREATE INDEX IF NOT EXISTS beacon_rx ON beacon_readings(receiver, ref_id, ts);
-- Monitoring of known channels (targets), as opposed to discovery.
-- Every check is recorded INCLUDING the silent ones: that is the whole
-- point of the table.  Without a row for "looked, heard nothing" there
-- is no way to distinguish a quiet channel from one nobody ever visited,
-- and "last heard" becomes unanswerable -- which is exactly the gap this
-- table closes.  heard=0 rows are the majority and are not noise.
--
-- Keyed by (receiver, freq_hz, target) rather than freq alone: two
-- targets can legitimately share a frequency (contested channels, e.g.
-- two GMRS machines on 462.650 discriminated only by tone), and folding
-- them together would average away the disagreement we are trying to see.
CREATE TABLE IF NOT EXISTS monitor_checks (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  receiver TEXT NOT NULL,
  target TEXT NOT NULL,             -- target name, stable across runs
  ssrf_id TEXT,                     -- catalog join key, NULL for local hypotheses
  freq_hz INTEGER NOT NULL,
  decoder TEXT,
  heard INTEGER NOT NULL DEFAULT 0, -- 1 = decoder-level activity, not just energy
  snr_db REAL,                      -- energy-gate SNR, recorded even when silent
  params TEXT,                      -- {param: {state, observed, note}} JSON
  meta TEXT,
  lat REAL, lon REAL, alt_m REAL, fix TEXT,
  batch_id TEXT
);
CREATE INDEX IF NOT EXISTS mon_target ON monitor_checks(receiver, freq_hz, target, ts);
CREATE INDEX IF NOT EXISTS mon_heard ON monitor_checks(target, heard, ts);
"""

# Added columns for existing databases; failures mean the column exists.
MIGRATIONS = (
    "ALTER TABLE observations ADD COLUMN batch_id TEXT",
    "ALTER TABLE sweeps ADD COLUMN batch_id TEXT",
)


class Store:
    def __init__(self, path):
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        for m in MIGRATIONS:
            try:
                self.db.execute(m)
            except sqlite3.OperationalError:
                pass
        self.db.commit()

    def add_sweep(self, receiver, plan, start_hz, stop_hz, step_hz, fix, rows):
        cur = self.db.execute(
            "INSERT INTO sweeps(ts,receiver,plan,start_hz,stop_hz,step_hz,lat,lon,alt_m,fix)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (time.time(), receiver, plan, start_hz, stop_hz, step_hz, *fix.as_tuple()))
        sid = cur.lastrowid
        self.db.executemany(
            "INSERT INTO bins(sweep_id,freq_hz,db) VALUES(?,?,?)",
            [(sid, f, d) for f, d, _step in rows])
        self.db.commit()
        return sid

    def channel_median(self, receiver, freq_hz, tol_hz=0):
        """Per-channel noise median for THIS receiver only (never cross-receiver)."""
        cur = self.db.execute(
            "SELECT b.db FROM bins b JOIN sweeps s ON s.id=b.sweep_id"
            " WHERE s.receiver=? AND b.freq_hz BETWEEN ? AND ? ORDER BY b.db",
            (receiver, freq_hz - tol_hz, freq_hz + tol_hz))
        vals = [r[0] for r in cur.fetchall()]
        return vals[len(vals) // 2] if vals else None

    def add_observation(self, receiver, freq_hz, snr_db, duration_s, decoder,
                        gated, meta, fix):
        self.db.execute(
            "INSERT INTO observations(ts,receiver,freq_hz,snr_db,duration_s,decoder,gated,meta,lat,lon,alt_m,fix)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), receiver, freq_hz, snr_db, duration_s, decoder,
             1 if gated else 0, json.dumps(meta or {}), *fix.as_tuple()))
        self.db.commit()

    # ---- monitoring of known channels ----

    def add_monitor_check(self, receiver, target, heard, snr_db, params, meta,
                          fix):
        self.db.execute(
            "INSERT INTO monitor_checks(ts,receiver,target,ssrf_id,freq_hz,"
            "decoder,heard,snr_db,params,meta,lat,lon,alt_m,fix)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), receiver, target["name"], target.get("ssrf_id"),
             target["freq_hz"], target.get("decoder"), 1 if heard else 0,
             snr_db, json.dumps(params or {}), json.dumps(meta or {}),
             *fix.as_tuple()))
        self.db.commit()

    def last_checked(self, receiver):
        """{(freq_hz, target): ts} of the most recent check, heard or not.

        Drives the scheduler, so it must count silent checks: if it only
        counted hearings, a permanently quiet target would look forever
        overdue and starve every other target on the list.
        """
        return {(r[0], r[1]): r[2] for r in self.db.execute(
            "SELECT freq_hz, target, MAX(ts) FROM monitor_checks"
            " WHERE receiver=? GROUP BY freq_hz, target", (receiver,))}

    def monitor_status(self, receiver=None, targets=None):
        """Per-target rollup: last heard, last checked, who heard it.

        This is the milestone-1 answer: one row per channel we care about,
        with last-heard and parameter state, including targets that have
        never been heard at all (those still get a row -- an unheard
        target is a finding, not an omission).
        """
        args = []
        where = ""
        if receiver:
            where = " WHERE receiver=?"
            args.append(receiver)
        rows = self.db.execute(
            "SELECT target, freq_hz, MAX(ssrf_id), COUNT(*) AS checks,"
            " SUM(heard) AS hearings,"
            " MAX(CASE WHEN heard=1 THEN ts END) AS last_heard,"
            " MAX(ts) AS last_checked,"
            " COUNT(DISTINCT CASE WHEN heard=1 THEN receiver END) AS rx_heard"
            " FROM monitor_checks" + where +
            " GROUP BY target, freq_hz ORDER BY target", args).fetchall()
        cols = ("target", "freq_hz", "ssrf_id", "checks", "hearings",
                "last_heard", "last_checked", "rx_heard")
        out = [dict(zip(cols, r)) for r in rows]
        for row in out:
            row["params"] = self.param_state(row["target"], row["freq_hz"])
        if targets:
            known = {(t["freq_hz"], t["name"]) for t in targets}
            seen = {(r["freq_hz"], r["target"]) for r in out}
            for t in targets:
                if (t["freq_hz"], t["name"]) not in seen:
                    out.append({"target": t["name"], "freq_hz": t["freq_hz"],
                                "ssrf_id": t.get("ssrf_id"), "checks": 0,
                                "hearings": 0, "last_heard": None,
                                "last_checked": None, "rx_heard": 0,
                                "params": {}})
            out = [r for r in out
                   if (r["freq_hz"], r["target"]) in known] or out
            out.sort(key=lambda r: r["target"])
        return out

    def param_state(self, target, freq_hz, hearings_for_verified=2):
        """Aggregate per-parameter verification across all checks.

        Promotion rule (Eric, 2026-09-28: a single station CAN earn
        verified): a parameter is `verified` once it has been measured
        consistently on `hearings_for_verified` separate hearings, from
        one receiver or many.  Corroborating receivers are reported
        alongside rather than required, so a one-station site is not
        permanently stuck at unverified -- while still distinguishing
        "measured twice here" from "independently corroborated".

        A conflict is sticky and always wins: once two checks disagree
        with the catalog, later agreement does not erase it.  Silence
        never downgrades anything, because a silent check grades nothing.
        """
        rows = self.db.execute(
            "SELECT receiver, params, ts FROM monitor_checks"
            " WHERE target=? AND freq_hz=? AND heard=1 AND params IS NOT NULL"
            " ORDER BY ts", (target, freq_hz)).fetchall()
        agg = {}
        for receiver, praw, ts in rows:
            try:
                params = json.loads(praw) or {}
            except (TypeError, ValueError):
                continue
            for key, v in params.items():
                a = agg.setdefault(key, {
                    "state": "unverified", "observed": None, "note": None,
                    "hits": 0, "conflicts": 0, "receivers": set(),
                    "last_ts": None, "observed_values": []})
                st = v.get("state")
                a["last_ts"] = ts
                if st == "conflict":
                    a["conflicts"] += 1
                    a["state"] = "conflict"
                    a["observed"] = v.get("observed")
                    a["note"] = v.get("note")
                elif st == "verified":
                    a["hits"] += 1
                    a["receivers"].add(receiver)
                    if a["state"] != "conflict":
                        a["observed"] = v.get("observed")
                elif st == "suspect" and a["state"] == "unverified":
                    a["note"] = v.get("note")
                    a["state"] = "suspect"
                elif a["state"] == "unverified":
                    a["note"] = v.get("note")
                if v.get("observed") is not None:
                    a["observed_values"].append(v["observed"])
        for key, a in agg.items():
            a["receivers"] = sorted(a["receivers"])
            if a["state"] != "conflict" and a["hits"] >= hearings_for_verified:
                a["state"] = "verified"
            elif a["state"] not in ("conflict", "suspect") and a["hits"]:
                a["state"] = "measured"   # seen once; needs one more hearing
            a["corroborated"] = len(a["receivers"]) >= 2
            a.pop("observed_values", None)
        return agg

    def unsubmitted_monitor_checks(self):
        cols = ("id", "ts", "receiver", "target", "ssrf_id", "freq_hz",
                "decoder", "heard", "snr_db", "params", "meta",
                "lat", "lon", "alt_m", "fix")
        return [dict(zip(cols, r)) for r in self.db.execute(
            "SELECT id,ts,receiver,target,ssrf_id,freq_hz,decoder,heard,"
            "snr_db,params,meta,lat,lon,alt_m,fix FROM monitor_checks"
            " WHERE batch_id IS NULL").fetchall()]

    def active_channels(self, receiver=None, min_snr=6.0):
        """Channels with observed activity, with hit counts and best SNR."""
        q = ("SELECT freq_hz, COUNT(*), MAX(snr_db), MAX(ts), receiver,"
             " SUM(gated) FROM observations WHERE snr_db >= ?")
        args = [min_snr]
        if receiver:
            q += " AND receiver=?"
            args.append(receiver)
        q += " GROUP BY freq_hz, receiver ORDER BY COUNT(*) DESC"
        return self.db.execute(q, args).fetchall()

    # ---- network submission (NETWORK.md M7) ----

    def unsubmitted(self):
        """(sweep_bin_rows, observation_rows) not yet packaged into a batch."""
        bins = self.db.execute(
            "SELECT s.receiver, b.freq_hz, b.db FROM bins b"
            " JOIN sweeps s ON s.id=b.sweep_id WHERE s.batch_id IS NULL").fetchall()
        obs = self.db.execute(
            "SELECT id,ts,receiver,freq_hz,snr_db,duration_s,decoder,gated,meta,"
            "lat,lon,alt_m,fix FROM observations WHERE batch_id IS NULL").fetchall()
        return bins, obs

    def mark_batched(self, batch_id, envelope_json):
        """Stamp all unsubmitted rows with batch_id and store the envelope."""
        self.db.execute("UPDATE sweeps SET batch_id=? WHERE batch_id IS NULL",
                        (batch_id,))
        self.db.execute(
            "UPDATE observations SET batch_id=? WHERE batch_id IS NULL",
            (batch_id,))
        self.db.execute(
            "UPDATE beacon_readings SET batch_id=? WHERE batch_id IS NULL",
            (batch_id,))
        self.db.execute(
            "UPDATE monitor_checks SET batch_id=? WHERE batch_id IS NULL",
            (batch_id,))
        self.db.execute(
            "INSERT INTO batches(id,created,acked,payload) VALUES(?,?,0,?)",
            (batch_id, time.time(), envelope_json))
        self.db.commit()

    def pending_batches(self):
        """Unacked envelopes, oldest first — replayed on reconnect."""
        return self.db.execute(
            "SELECT id, payload FROM batches WHERE acked=0 ORDER BY created").fetchall()

    def ack_batch(self, batch_id):
        self.db.execute("UPDATE batches SET acked=1 WHERE id=?", (batch_id,))
        self.db.commit()

    # ---- beacon calibration (NETWORK.md S7) ----

    def add_beacon_reading(self, receiver, ref_id, freq_hz, band, coverage,
                           signal_db, noise_db, snr_db, gain, status,
                           pinned=False):
        self.db.execute(
            "INSERT INTO beacon_readings(ts,receiver,ref_id,freq_hz,band,"
            "coverage,signal_db,noise_db,snr_db,gain,pinned,status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), receiver, ref_id, freq_hz, band, coverage,
             signal_db, noise_db, snr_db, gain, 1 if pinned else 0, status))
        self.db.commit()

    def beacon_history(self, receiver, ref_id, days=7, pinned_only=True):
        """Readings for one observer/reference, newest first.

        pinned_only drops AGC readings: absolute dB taken under AGC is not
        comparable between passes, so it must never enter a baseline.
        """
        q = ("SELECT ts,signal_db,noise_db,snr_db,gain,status,coverage"
             " FROM beacon_readings WHERE receiver=? AND ref_id=? AND ts>=?")
        args = [receiver, ref_id, time.time() - days * 86400]
        if pinned_only:
            q += " AND pinned=1"
        q += " ORDER BY ts DESC"
        cols = ("ts", "signal_db", "noise_db", "snr_db", "gain", "status",
                "coverage")
        return [dict(zip(cols, r)) for r in self.db.execute(q, args).fetchall()]

    def beacon_latest(self, receiver=None):
        """Most recent reading per (receiver, ref_id) — the rffeed view."""
        q = ("SELECT b.receiver,b.ref_id,b.freq_hz,b.band,b.coverage,"
             "b.signal_db,b.noise_db,b.snr_db,b.gain,b.pinned,b.status,b.ts"
             " FROM beacon_readings b JOIN (SELECT receiver,ref_id,MAX(ts) t"
             " FROM beacon_readings GROUP BY receiver,ref_id) m"
             " ON m.receiver=b.receiver AND m.ref_id=b.ref_id AND m.t=b.ts")
        args = []
        if receiver:
            q += " WHERE b.receiver=?"
            args.append(receiver)
        q += " ORDER BY b.receiver, b.freq_hz"
        cols = ("receiver", "ref_id", "freq_hz", "band", "coverage",
                "signal_db", "noise_db", "snr_db", "gain", "pinned", "status",
                "ts")
        return [dict(zip(cols, r)) for r in self.db.execute(q, args).fetchall()]

    def unsubmitted_beacons(self):
        """Beacon readings not yet packaged into a batch."""
        cols = ("id", "ts", "receiver", "ref_id", "freq_hz", "band",
                "coverage", "signal_db", "noise_db", "snr_db", "gain",
                "pinned", "status")
        return [dict(zip(cols, r)) for r in self.db.execute(
            "SELECT id,ts,receiver,ref_id,freq_hz,band,coverage,signal_db,"
            "noise_db,snr_db,gain,pinned,status FROM beacon_readings"
            " WHERE batch_id IS NULL").fetchall()]

    def observations_for(self, freq_hz, tol_hz=6250):
        return self.db.execute(
            "SELECT ts,receiver,snr_db,duration_s,decoder,gated,meta FROM observations"
            " WHERE freq_hz BETWEEN ? AND ? ORDER BY ts",
            (freq_hz - tol_hz, freq_hz + tol_hz)).fetchall()
