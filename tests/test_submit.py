"""M7 submit-path tests: identity, batch packaging, store-and-forward."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rfsurvey.store import Store            # noqa: E402
from rfsurvey import submit as submit_mod   # noqa: E402
from rfsurvey import station as station_mod # noqa: E402


class FakeFix:
    def as_tuple(self):
        return (41.97, -87.69, 180.0, "static")


class FakeStation:
    station_id = "test-station"
    public_key_b64 = "fake"

    def sign(self, batch):
        return {"batch": batch, "sig": "unsigned-test", "station_id": self.station_id}


def seeded_store(path):
    s = Store(path)
    s.add_sweep("BENCH", "uhf-dmr", 450_000_000, 470_000_000, 6250, FakeFix(),
                [(460_000_000, -40.0, 6250.0), (460_000_000, -38.0, 6250.0),
                 (460_006_250, -60.0, 6250.0)])
    s.add_observation("BENCH", 460_000_000, 18.5, 90, "dmr", True,
                      {"cc": 9, "tgs": [100]}, FakeFix())
    return s


class TestBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = seeded_store(os.path.join(self.tmp, "obs.db"))

    def test_build_and_ack_cycle(self):
        env = submit_mod.build_batch(self.store, FakeStation())
        b = env["batch"]
        self.assertEqual(b["schema"], "rfsurvey.obs.v1")
        self.assertEqual(b["station_id"], "test-station")
        self.assertEqual(len(b["observations"]), 1)
        self.assertTrue(b["observations"][0]["gated"])
        s460 = [x for x in b["sweep_summaries"] if x["freq_hz"] == 460_000_000][0]
        self.assertEqual(s460["hits"], 2)
        self.assertEqual(s460["max_db"], -38.0)
        self.assertEqual(s460["median_db"], -39.0)
        pending = self.store.pending_batches()
        self.assertEqual(len(pending), 1)
        self.assertEqual(json.loads(pending[0][1])["batch"]["batch_id"],
                         b["batch_id"])
        self.assertIsNone(submit_mod.build_batch(self.store, FakeStation()))
        self.store.ack_batch(b["batch_id"])
        self.assertEqual(self.store.pending_batches(), [])

    def test_new_rows_after_batch_get_new_batch(self):
        submit_mod.build_batch(self.store, FakeStation())
        self.store.add_observation("BENCH", 452_387_500, 12.0, 60, "dmr", True,
                                   {}, FakeFix())
        env2 = submit_mod.build_batch(self.store, FakeStation())
        self.assertEqual(len(env2["batch"]["observations"]), 1)
        self.assertEqual(len(self.store.pending_batches()), 2)


@unittest.skipUnless(station_mod.HAVE_CRYPTO, "cryptography not installed")
class TestIdentity(unittest.TestCase):
    def test_sign_verify_roundtrip(self):
        tmp = tempfile.mkdtemp()
        key = os.path.join(tmp, "station.key")
        st = station_mod.Station.create("unit-test", key)
        env = st.sign({"schema": "rfsurvey.obs.v1", "batch_id": "x"})
        self.assertTrue(station_mod.verify(env, st.public_key_b64))
        env["batch"]["batch_id"] = "tampered"
        self.assertFalse(station_mod.verify(env, st.public_key_b64))
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        with self.assertRaises(station_mod.StationError):
            station_mod.Station.create("unit-test", key)
        st2 = station_mod.Station.load({"station": {"id": "unit-test", "key": key}})
        self.assertEqual(st2.public_key_b64, st.public_key_b64)


class TestStoreLiveness(unittest.TestCase):
    """last_sweep/last_monitor_check back the heartbeat's liveness fields."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "obs.db"))

    def _sweep(self, rx):
        self.store.add_sweep(rx, "p", 1, 2, 1, FakeFix(), [(1, -1.0, 1.0)])

    def test_empty_when_nothing_recorded(self):
        self.assertEqual(self.store.last_sweep(), {})
        self.assertEqual(self.store.last_monitor_check(), {})

    def test_tracks_each_receiver(self):
        self._sweep("BENCH")
        self._sweep("ADSB")
        self.assertEqual(set(self.store.last_sweep()), {"BENCH", "ADSB"})

    def test_filters_by_receiver(self):
        self._sweep("BENCH")
        self.assertEqual(set(self.store.last_sweep("BENCH")), {"BENCH"})
        self.assertEqual(self.store.last_sweep("NOPE"), {})

    def test_returns_the_latest_not_the_first(self):
        self._sweep("BENCH")
        first = self.store.last_sweep()["BENCH"]
        self._sweep("BENCH")
        self.assertGreaterEqual(self.store.last_sweep()["BENCH"], first)


class TestStationHealth(unittest.TestCase):
    """Heartbeat liveness must track the radio, not just the submit loop."""

    TGT = {"name": "Downtown 575", "freq_hz": 462_575_000}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "obs.db"))
        self.cfg = {"devices": {"BENCH": {}, "ADSB": {}}}

    def _sweep(self, rx="BENCH"):
        self.store.add_sweep(rx, "uhf", 450_000_000, 470_000_000, 6250,
                             FakeFix(), [(460_000_000, -40.0, 6250.0)])

    def _check(self, rx="BENCH"):
        self.store.add_monitor_check(rx, self.TGT, False, 1.0, {}, {},
                                     FakeFix())

    def test_fresh_sweep_is_healthy(self):
        self._sweep()
        h = submit_mod.station_health(self.store, self.cfg)
        bench = h["receivers"]["BENCH"]
        self.assertTrue(bench["healthy"])
        self.assertLess(bench["last_sweep_age_s"], 5)
        self.assertEqual(h["receivers_total"], 2)

    def test_configured_but_never_seen_is_unhealthy(self):
        self._sweep("BENCH")
        adsb = submit_mod.station_health(self.store, self.cfg)["receivers"]["ADSB"]
        self.assertFalse(adsb["healthy"])
        self.assertNotIn("last_sweep_ts", adsb)
        self.assertNotIn("last_check_ts", adsb)

    def test_stale_sweep_goes_unhealthy(self):
        """The meshpi regression: submit loop fine, radio dead for days."""
        self._sweep("BENCH")
        self._sweep("ADSB")
        h = submit_mod.station_health(self.store, self.cfg,
                                      now=time.time() + 3 * 86400)
        self.assertFalse(h["receivers"]["BENCH"]["healthy"])
        self.assertGreater(h["receivers"]["BENCH"]["last_sweep_age_s"], 86400)
        self.assertEqual(h["receivers_healthy"], 0)

    def test_stale_after_is_configurable(self):
        self._sweep()
        cfg = dict(self.cfg, station={"stale_after_s": 10})
        h = submit_mod.station_health(self.store, cfg, now=time.time() + 60)
        self.assertEqual(h["stale_after_s"], 10)
        self.assertFalse(h["receivers"]["BENCH"]["healthy"])

    def test_monitor_check_alone_keeps_receiver_alive(self):
        """A monitor-only receiver never sweeps; it must not read as dead."""
        self._check("BENCH")
        bench = submit_mod.station_health(self.store, self.cfg)["receivers"]["BENCH"]
        self.assertTrue(bench["healthy"])
        self.assertIn("last_check_age_s", bench)
        self.assertNotIn("last_sweep_ts", bench)

    def test_stale_sweep_but_fresh_check_stays_healthy(self):
        self._sweep()
        self._check()
        h = submit_mod.station_health(self.store, self.cfg)
        self.assertTrue(h["receivers"]["BENCH"]["healthy"])

    def test_healthy_count_is_per_receiver(self):
        self._sweep("BENCH")
        h = submit_mod.station_health(self.store, self.cfg)
        self.assertEqual(h["receivers_healthy"], 1)
        self.assertEqual(h["receivers_total"], 2)

    def test_no_devices_configured_is_empty_not_crash(self):
        h = submit_mod.station_health(self.store, {})
        self.assertEqual(h["receivers"], {})
        self.assertEqual(h["receivers_total"], 0)


if __name__ == "__main__":
    unittest.main()
