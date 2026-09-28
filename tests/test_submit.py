"""M7 submit-path tests: identity, batch packaging, store-and-forward."""
import json
import os
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
