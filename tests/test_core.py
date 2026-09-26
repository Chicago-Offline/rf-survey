import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rfsurvey import devices, sweep, report, config
from rfsurvey.store import Store
from rfsurvey.location import Fix, StaticLocation


class TestDeviceParse(unittest.TestCase):
    def test_list_regex(self):
        line = "  1:  Realtek, RTL2838UHIDIR, SN: BENCH"
        m = devices.LIST_RE.match(line)
        self.assertEqual(m.group(1), "1")
        self.assertEqual(m.group(4), "BENCH")


class TestSweep(unittest.TestCase):
    def test_parse_csv(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("2026-09-26, 17:00:00, 450000000, 450025000, 6250, 100, -20.1, -35.2, -34.9, -36.0\n")
            p = f.name
        rows = sweep._parse_csv(p)
        os.unlink(p)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], (450000000, -20.1))
        self.assertEqual(rows[1][0], 450006250)

    def test_snr_hits(self):
        rows = [(100, -35.0), (200, -34.0), (300, -10.0)]
        hits = sweep.snr_hits(rows, {100: -35.0, 200: -34.5, 300: -34.0}, 12.0)
        self.assertEqual([h[0] for h in hits], [300])
        self.assertAlmostEqual(hits[0][1], 24.0)

    def test_snr_bootstrap_global_median(self):
        rows = [(1, -35.0), (2, -34.0), (3, -5.0)]
        hits = sweep.snr_hits(rows, {}, 12.0)
        self.assertEqual([h[0] for h in hits], [3])


class TestStoreReport(unittest.TestCase):
    def test_roundtrip_and_candidates(self):
        db = tempfile.mktemp(suffix=".db")
        s = Store(db)
        fix = Fix(41.88, -87.63, 180, "static")
        s.add_sweep("BENCH", "t", 450000000, 470000000, 6250, fix,
                    [(452500000, -35.0), (452506250, -34.0)])
        self.assertAlmostEqual(s.channel_median("BENCH", 452500000), -35.0)
        self.assertIsNone(s.channel_median("SONDE", 452500000))  # never cross-receiver
        for _ in range(3):
            s.add_observation("BENCH", 452500000, 18.0, 90, "dmr", True,
                              {"color_codes": [7], "talkgroups": [101]}, fix)
        s.add_observation("BENCH", 460000000, 14.0, 90, "dmr", False,
                          {"color_codes": [9]}, fix)  # ungated = decoder lie
        cands = report.candidates(s, min_gated=2)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["frequency_mhz"], 452.5)
        self.assertEqual(c["color_codes"], [7])
        self.assertEqual(c["talkgroups"], [101])
        self.assertIn(c["confidence"], ("low", "medium", "high"))
        txt = report.summary(s)
        self.assertIn("452.5", txt)
        os.unlink(db)


class TestPlan(unittest.TestCase):
    def test_plan_defaults(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write("name: t\nbands:\n  - {start_mhz: 450, stop_mhz: 470, step_khz: 6.25}\n")
            p = f.name
        plan = config.load_plan(p)
        os.unlink(p)
        self.assertEqual(plan["dwell"]["decoder"], "nfm")
        self.assertEqual(plan["sweep"]["passes"], 0)

    def test_plan_missing_key(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write("name: t\n")
            p = f.name
        with self.assertRaises(config.ConfigError):
            config.load_plan(p)
        os.unlink(p)


class TestStaticLocation(unittest.TestCase):
    def test_static(self):
        loc = StaticLocation({"lat": 1.0, "lon": 2.0})
        self.assertEqual(loc.get().as_tuple()[:2], (1.0, 2.0))


if __name__ == "__main__":
    unittest.main()
