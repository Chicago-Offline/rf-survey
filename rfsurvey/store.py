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
            [(sid, f, d) for f, d in rows])
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

    def observations_for(self, freq_hz, tol_hz=6250):
        return self.db.execute(
            "SELECT ts,receiver,snr_db,duration_s,decoder,gated,meta FROM observations"
            " WHERE freq_hz BETWEEN ? AND ? ORDER BY ts",
            (freq_hz - tol_hz, freq_hz + tol_hz)).fetchall()
