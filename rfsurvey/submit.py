"""MQTT batch publisher: store-and-forward submission (NETWORK.md M7).

Packages unsubmitted rows into a signed, versioned batch, publishes over
MQTT (QoS 1, gzip JSON), and marks batches acked only on broker confirm.
Unacked batches replay on the next run — batch UUIDs make replays
idempotent aggregator-side. Requires the 'submit' extra (paho-mqtt).
"""
import gzip
import json
import logging
import statistics
import time
import uuid

log = logging.getLogger(__name__)

SCHEMA_ID = "rfsurvey.obs.v1"


def observer_descriptors(cfg):
    """Per-observer descriptors for the batch `receivers` block (NETWORK.md S1).

    The observer is `(station_id, receiver)` — one SDR plus the antenna and
    placement actually attached to it. The aggregator needs the antenna to
    decide which beacon references an observer may legitimately be scored
    against; without it, readings are recorded but unscoreable.
    """
    out = []
    for serial, d in sorted((cfg.get("devices") or {}).items()):
        d = d or {}
        desc = {"receiver": serial}
        for k in ("role", "gain", "floor_offset_db"):
            if d.get(k) is not None:
                desc[k] = d[k]
        if d.get("antenna"):
            desc["antenna"] = d["antenna"]
        if d.get("placement"):
            desc["placement"] = d["placement"]
        out.append(desc)
    return out


def build_batch(store, station, site=None, receivers=None):
    """Package all unsubmitted evidence into one signed envelope.

    Sweep bins go up as per-(receiver, freq) summaries — median/max/hits —
    never raw bins. Dwell observations go up as events. Returns the
    envelope dict, or None when there is nothing to send.

    `receivers` is additive (NETWORK.md S1): rfsurvey.obs.v1 consumers ignore
    it, and a missing descriptor is treated as unknown rather than rejecting
    the batch. Bump the schema only once scoring requires it.
    """
    bins, obs = store.unsubmitted()
    beacons = store.unsubmitted_beacons()
    if not bins and not obs and not beacons:
        return None
    groups = {}
    for receiver, freq, db in bins:
        groups.setdefault((receiver, freq), []).append(db)
    summaries = [
        {"receiver": r, "freq_hz": f,
         "median_db": round(statistics.median(v), 2),
         "max_db": round(max(v), 2), "hits": len(v)}
        for (r, f), v in sorted(groups.items())]
    observations = [
        {"id": o[0], "ts": o[1], "receiver": o[2], "freq_hz": o[3],
         "snr_db": o[4], "duration_s": o[5], "decoder": o[6],
         "gated": bool(o[7]), "meta": json.loads(o[8] or "{}"),
         "lat": o[9], "lon": o[10], "alt_m": o[11], "fix": o[12]}
        for o in obs]
    batch = {
        "schema": SCHEMA_ID,
        "batch_id": str(uuid.uuid4()),
        "station_id": station.station_id,
        "generated_at": time.time(),
        "site": site or {},
        "receivers": receivers or [],
        "sweep_summaries": summaries,
        "observations": observations,
        "beacon_readings": beacons,
    }
    envelope = station.sign(batch)
    store.mark_batched(batch["batch_id"], json.dumps(envelope))
    log.info("packaged batch %s: %d sweep summaries, %d observations, "
             "%d beacon readings", batch["batch_id"], len(summaries),
             len(observations), len(beacons))
    return envelope


class Publisher:
    """Thin paho-mqtt wrapper for the chioff broker conventions:
    WebSockets + TLS + token auth (username = station_id, password = token).
    """

    def __init__(self, cfg, station):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise RuntimeError(
                "paho-mqtt not installed — pip install 'rf-survey[submit]'")
        m = (cfg.get("station") or {}).get("mqtt") or {}
        self.host = m.get("server")
        if not self.host:
            raise RuntimeError("config missing station.mqtt.server")
        self.port = int(m.get("port", 443))
        self.prefix = m.get("topic_prefix", "rfsurvey")
        self.station = station
        transport = m.get("transport", "websockets")
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"rfsurvey-{station.station_id}",
            transport=transport)
        if m.get("tls", True):
            self.client.tls_set()
        token = m.get("token")
        token_file = m.get("token_file")
        if token_file and not token:
            with open(token_file) as f:
                token = f.read().strip()
        if token:
            self.client.username_pw_set(station.station_id, token)

    def __enter__(self):
        self.client.connect(self.host, self.port, keepalive=60)
        self.client.loop_start()
        return self

    def __exit__(self, *exc):
        self.client.loop_stop()
        self.client.disconnect()

    def publish_pending(self, store, timeout=30):
        """Send every unacked batch; ack in the store on broker confirm."""
        sent = 0
        for batch_id, payload in store.pending_batches():
            topic = f"{self.prefix}/obs/{self.station.station_id}"
            info = self.client.publish(
                topic, gzip.compress(payload.encode()), qos=1)
            info.wait_for_publish(timeout)
            if info.is_published():
                store.ack_batch(batch_id)
                sent += 1
                log.info("batch %s acked", batch_id)
            else:
                log.warning("batch %s NOT confirmed; will retry next run",
                            batch_id)
                break  # keep ordering; don't spray retries
        return sent

    def heartbeat(self, extra=None):
        status = {"station_id": self.station.station_id, "ts": time.time(),
                  "schema": "rfsurvey.status.v1"}
        status.update(extra or {})
        self.client.publish(f"{self.prefix}/status/{self.station.station_id}",
                            json.dumps(status), qos=0, retain=True)
