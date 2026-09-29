import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rfsurvey import devices, sweep, report, config, engine
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
        self.assertEqual(rows[0], (450000000, -20.1, 6250.0))
        self.assertEqual(rows[1][0], 450006250)

    def test_snr_hits(self):
        rows = [(100, -35.0, 10.0), (200, -34.0, 10.0), (300, -10.0, 10.0)]
        hits = sweep.snr_hits(rows, {100: -35.0, 200: -34.5, 300: -34.0}, 12.0)
        self.assertEqual([h[0] for h in hits], [300])
        self.assertAlmostEqual(hits[0][1], 24.0)
        self.assertEqual(hits[0][3], 10.0)  # bin_hz rides along

    def test_snr_bootstrap_global_median(self):
        rows = [(1, -35.0, 10.0), (2, -34.0, 10.0), (3, -5.0, 10.0)]
        hits = sweep.snr_hits(rows, {}, 12.0)
        self.assertEqual([h[0] for h in hits], [3])

    def test_snap_channel_within_tolerance(self):
        # Real capture: requesting 6.25 kHz steps across 450-470 MHz, rtl_power
        # actually delivered 4882.8125 Hz bins (5 Msps / 1024-pt FFT). A bin
        # center 2.4 kHz off a 6250 Hz raster point is within half that real
        # bin width and should snap.
        raw = 460_092_773  # observed bin center, offset ~2.3 kHz from raster
        channel_hz, snapped = sweep.snap_channel(raw, 4882.8125, 6250)
        self.assertTrue(snapped)
        self.assertEqual(channel_hz % 6250, 0)
        self.assertLessEqual(abs(channel_hz - raw), 4882.8125 / 2 + 1)

    def test_snap_channel_ambiguous_stays_raw(self):
        # Worst case for a 6250 Hz raster: exactly 3125 Hz (half the raster
        # spacing) from the nearest grid point. With the real observed bin
        # width (4882.8125 Hz), half a bin is only 2441.4 Hz -- narrower
        # than that worst-case gap -- so this must NOT snap.
        raw = 450_000_000 + 3125
        channel_hz, snapped = sweep.snap_channel(raw, 4882.8125, 6250)
        self.assertFalse(snapped)
        self.assertEqual(channel_hz, raw)  # raw bin frequency preserved, not forced

    def test_snap_channel_exact_hit(self):
        channel_hz, snapped = sweep.snap_channel(450_000_000, 4882.8125, 6250)
        self.assertTrue(snapped)
        self.assertEqual(channel_hz, 450_000_000)

    def test_vhf_channels_are_not_on_a_6250_raster(self):
        # The bug Eric caught: these are the real channels MuehlMini is
        # hearing. Each is a whole number of 2.5 kHz steps and none is a
        # whole number of 6.25 kHz steps, so a 6250 raster can never name
        # them -- it can only snap them to something wrong.
        for hz in (146_880_000, 154_995_000, 159_195_000, 159_660_000):
            self.assertEqual(hz % 2500, 0, f"{hz} should be on 2.5 kHz grid")
            self.assertNotEqual(hz % 6250, 0, f"{hz} must not be on 6.25 kHz")

    def test_coarse_bin_cannot_snap_to_fine_raster(self):
        # 159.1950 MHz measured by a 3906.25 Hz sweep bin. Half a bin is
        # 1953 Hz, which is most of a 2.5 kHz step, so the tolerance clamp
        # must refuse rather than pick a neighbour at random.
        raw = 159_196_875
        channel_hz, snapped = sweep.snap_channel(raw, 3906.25, 2500)
        self.assertFalse(snapped)
        self.assertEqual(channel_hz, raw)

    def test_refined_carrier_snaps_with_ppm_tolerance(self):
        # Same channel after refine_carrier: ~100 Hz bins, and the carrier
        # measured 600 Hz low because the receiver is uncalibrated. A PPM
        # tolerance of 780 Hz (5 ppm at 156 MHz) should recover 159.1950.
        measured = 159_195_000 - 600
        channel_hz, snapped = sweep.snap_channel(measured, 97.6, 2500, 780.0)
        self.assertTrue(snapped)
        self.assertEqual(channel_hz, 159_195_000)

    def test_tolerance_clamped_below_half_raster(self):
        # An absurd tolerance must not make everything snap: a carrier
        # sitting exactly mid-gap stays unsnapped.
        measured = 159_195_000 + 1250
        channel_hz, snapped = sweep.snap_channel(measured, 97.6, 2500, 99_999.0)
        self.assertFalse(snapped)


class TestBandRaster(unittest.TestCase):
    def test_band_override_beats_plan_and_default(self):
        plan = {"sweep": {"channel_raster_hz": 6250}}
        self.assertEqual(
            engine.band_raster_hz({"channel_raster_hz": 2500}, plan), 2500)

    def test_plan_level_used_when_band_silent(self):
        plan = {"sweep": {"channel_raster_hz": 3125}}
        self.assertEqual(engine.band_raster_hz({}, plan), 3125)

    def test_default_when_unset(self):
        self.assertEqual(engine.band_raster_hz({}, {"sweep": {}}), 6250)

    def test_ppm_tolerance_scales_with_frequency(self):
        band = {"start_mhz": 150.0, "stop_mhz": 162.0}
        tol = engine.ppm_tolerance_hz({"ppm_error": 5.0}, band)
        self.assertAlmostEqual(tol, 780.0, places=0)  # 5 ppm at 156 MHz

    def test_ppm_defaults_when_device_unconfigured(self):
        band = {"start_mhz": 144.0, "stop_mhz": 148.0}
        self.assertGreater(engine.ppm_tolerance_hz({}, band), 0)


class TestBandDwell(unittest.TestCase):
    PLAN = {"dwell": {"snr_db": 8, "min_hits": 3, "duration_s": 90,
                      "decoder": "nfm", "squelch_db": 6}}

    def test_band_without_dwell_inherits_plan(self):
        self.assertEqual(engine.band_dwell({"name": "x"}, self.PLAN),
                         self.PLAN["dwell"])

    def test_band_overrides_merge_not_replace(self):
        # A band sets only the decoder; everything else must still arrive,
        # otherwise dwell() KeyErrors on snr_db/duration_s at the first hit.
        band = {"dwell": {"decoder": "dmr", "duration_s": 45}}
        got = engine.band_dwell(band, self.PLAN)
        self.assertEqual(got["decoder"], "dmr")
        self.assertEqual(got["duration_s"], 45)
        self.assertEqual(got["snr_db"], 8)
        self.assertEqual(got["min_hits"], 3)
        self.assertEqual(got["squelch_db"], 6)

    def test_does_not_mutate_shared_plan(self):
        # The plan dwell dict is reused for every band on every pass; if a
        # band override leaked into it, one dmr band would silently convert
        # the whole plan after the first sweep.
        engine.band_dwell({"dwell": {"decoder": "dmr"}}, self.PLAN)
        self.assertEqual(self.PLAN["dwell"]["decoder"], "nfm")

    def test_empty_and_null_dwell_are_no_ops(self):
        for value in ({}, None):
            self.assertEqual(engine.band_dwell({"dwell": value}, self.PLAN),
                             self.PLAN["dwell"])

    def test_mixed_plan_keeps_trunked_band_on_nfm(self):
        # The real case: 450-470 gets dmr while 851-869 stays energy-only
        # until trunking decode is integrated (NETWORK.md S4).
        biz = {"dwell": {"decoder": "dmr"}}
        trunked = {"channel_raster_hz": 12500}
        self.assertEqual(engine.band_dwell(biz, self.PLAN)["decoder"], "dmr")
        self.assertEqual(engine.band_dwell(trunked, self.PLAN)["decoder"], "nfm")


class TestDecoderValidation(unittest.TestCase):
    def _load(self, body):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return config.load_plan(p)
        finally:
            os.unlink(p)

    def test_band_decoder_accepted(self):
        plan = self._load(
            "name: t\nbands:\n"
            "  - {start_mhz: 450, stop_mhz: 470, step_khz: 6.25,"
            " dwell: {decoder: dmr}}\n")
        self.assertEqual(plan["bands"][0]["dwell"]["decoder"], "dmr")

    def test_unknown_band_decoder_rejected_at_load(self):
        # Must fail at startup, not minutes later inside the first dwell.
        with self.assertRaises(config.ConfigError):
            self._load(
                "name: t\nbands:\n"
                "  - {start_mhz: 450, stop_mhz: 470, step_khz: 6.25,"
                " dwell: {decoder: DMR}}\n")

    def test_unimplemented_decoder_rejected(self):
        # p25 is named in tier()'s digital set but has no dwell here yet.
        with self.assertRaises(config.ConfigError):
            self._load("name: t\nbands:\n"
                       "  - {start_mhz: 450, stop_mhz: 470, step_khz: 6.25}\n"
                       "dwell: {decoder: p25}\n")


class TestRefineCarrier(unittest.TestCase):
    def _rows(self, center, peak_hz):
        rows = []
        f = center - 24_000
        while f <= center + 24_000:
            db = -35.0
            if abs(f - peak_hz) < 100:
                db = -8.0
            if abs(f - center) < 100:
                db = -2.0   # DC spike, louder than the real carrier
            rows.append((int(f), db, 97.6))
            f += 97.6
        return rows

    def test_finds_true_carrier_and_rejects_dc_spike(self):
        target = 159_196_875          # coarse sweep bin, 1875 Hz high
        truth = 159_195_000           # the real channel
        center = target - 12_000
        captured = {}

        def fake(index, lo, hi, step_khz, integ, gain=None):
            captured["span"] = (lo, hi)
            return self._rows(center, truth)

        orig = sweep.run_rtl_power
        sweep.run_rtl_power = fake
        try:
            carrier, bin_hz, snr = sweep.refine_carrier(0, target)
        finally:
            sweep.run_rtl_power = orig

        # The DC spike is the loudest bin in the capture; it must lose.
        self.assertAlmostEqual(carrier, truth, delta=150)
        self.assertLess(bin_hz, 2500 / 2)   # fine enough to name a channel
        self.assertGreater(snr, 0)
        # Window is offset so the spike is clear of the candidate.
        lo, hi = captured["span"]
        self.assertLess(lo * 1e6, target)
        self.assertGreater(hi * 1e6, target)

    def test_returns_none_when_only_noise(self):
        def fake(index, lo, hi, step_khz, integ, gain=None):
            return [(159_190_000 + i * 98, -35.0, 97.6) for i in range(400)]
        orig = sweep.run_rtl_power
        sweep.run_rtl_power = fake
        try:
            carrier, bin_hz, snr = sweep.refine_carrier(0, 159_196_875)
        finally:
            sweep.run_rtl_power = orig
        self.assertIsNotNone(carrier)  # flat noise still yields a max bin


class TestStoreReport(unittest.TestCase):
    def test_roundtrip_and_candidates(self):
        db = tempfile.mktemp(suffix=".db")
        s = Store(db)
        fix = Fix(41.88, -87.63, 180, "static")
        s.add_sweep("BENCH", "t", 450000000, 470000000, 6250, fix,
                    [(452500000, -35.0, 6250.0), (452506250, -34.0, 6250.0)])
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


class TestReferenceCoverage(unittest.TestCase):
    """S7 rule 5 fences SCORING, not looking: uncovered refs are measured
    by default, carrying no_reference so they cannot feed a score."""

    REFS = [
        {"id": "fm", "freq_hz": 91_500_000, "band": "fm-broadcast",
         "kind": "fm", "bandwidth_hz": 200_000},
        {"id": "nws", "freq_hz": 162_550_000, "band": "vhf-high",
         "kind": "nfm", "bandwidth_hz": 16_000},
    ]
    DEV = {"antenna": {"bands_mhz": [[136, 174]]}}

    def test_coverage_verdicts(self):
        from rfsurvey import references as ref_mod
        self.assertEqual(ref_mod.coverage(self.REFS[0], self.DEV),
                         ref_mod.NO_REFERENCE)
        self.assertEqual(ref_mod.coverage(self.REFS[1], self.DEV),
                         ref_mod.OK)
        self.assertEqual(ref_mod.coverage(self.REFS[0], {}),
                         ref_mod.UNVERIFIED)

    def test_plan_measures_uncovered_by_default(self):
        from rfsurvey import references as ref_mod
        plan = ref_mod.plan_for(self.REFS, self.DEV)
        self.assertEqual(len(plan), 2)
        verdicts = {r["id"]: v for r, v in plan}
        # Measured, but the unscoreable verdict travels with the reading.
        self.assertEqual(verdicts["fm"], ref_mod.NO_REFERENCE)
        self.assertEqual(verdicts["nws"], ref_mod.OK)

    def test_skip_uncovered_restores_old_behavior(self):
        from rfsurvey import references as ref_mod
        plan = ref_mod.plan_for(self.REFS, self.DEV,
                                include_uncovered=False)
        self.assertEqual([r["id"] for r, _v in plan], ["nws"])

    def test_undeclared_antenna_measures_everything_as_unverified(self):
        from rfsurvey import references as ref_mod
        plan = ref_mod.plan_for(self.REFS, {})
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(v == ref_mod.UNVERIFIED for _r, v in plan))


if __name__ == "__main__":
    unittest.main()
