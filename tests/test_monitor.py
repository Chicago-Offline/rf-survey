import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rfsurvey import config, monitor, targets as targets_mod
from rfsurvey.config import ConfigError
from rfsurvey.store import Store
from rfsurvey.location import Fix

PLAN = os.path.join(os.path.dirname(__file__), "..", "plans",
                    "plan-chicago-monitor.yml")


def _t(name="T", freq_mhz=462.675, expect=None, **kw):
    raw = {"name": name, "freq_mhz": freq_mhz}
    if expect is not None:
        raw["expect"] = expect
    raw.update(kw)
    return targets_mod.normalize(raw)


class TestTargetNormalize(unittest.TestCase):
    def test_freq_mhz_rounds_not_truncates(self):
        # int(462.675 * 1e6) truncates to 462674999 in binary float.
        self.assertEqual(_t(freq_mhz=462.675)["freq_hz"], 462675000)

    def test_requires_name_and_freq(self):
        with self.assertRaises(ConfigError):
            targets_mod.normalize({"freq_mhz": 462.675})
        with self.assertRaises(ConfigError):
            targets_mod.normalize({"name": "no freq"})

    def test_unknown_expect_key_rejected(self):
        # A misspelled expectation that silently grades "no claim" is worse
        # than no expectation: the page would read clean.
        with self.assertRaises(ConfigError) as cm:
            _t(expect={"ctcss": 141.3})
        self.assertIn("unknown expect key", str(cm.exception))

    def test_expect_absent_vs_claimed_absent_differ(self):
        unknown = _t(expect={})
        claimed = _t(expect={"ctcss_hz": None})
        self.assertNotIn("ctcss_hz", unknown["expect"])
        self.assertIn("ctcss_hz", claimed["expect"])

    def test_duplicate_targets_rejected(self):
        mon = {"targets": [{"name": "A", "freq_mhz": 462.675},
                           {"name": "A", "freq_mhz": 462.675}]}
        with self.assertRaises(ConfigError):
            targets_mod.load_targets(mon)

    def test_priority_must_be_positive_int(self):
        with self.assertRaises(ConfigError):
            _t(priority=0)


class TestGrading(unittest.TestCase):
    def setUp(self):
        self.tone = _t(expect={"ctcss_hz": 141.3})

    def _g(self, target, meta, heard=True):
        return {k: v["state"]
                for k, v in monitor.verify_params(target, meta, heard).items()}

    def test_tone_match_verifies(self):
        self.assertEqual(
            self._g(self.tone, {"active": 1, "audio_bytes": 9,
                                "ctcss_hz": 141.3}),
            {"ctcss_hz": "verified"})

    def test_tone_mismatch_conflicts(self):
        self.assertEqual(
            self._g(self.tone, {"active": 1, "audio_bytes": 9,
                                "ctcss_hz": 107.2}),
            {"ctcss_hz": "conflict"})

    def test_unresolved_tone_is_unverified_not_conflict(self):
        # Failing to measure a tone does not disprove it.
        self.assertEqual(
            self._g(self.tone, {"active": 1, "audio_bytes": 9}),
            {"ctcss_hz": "unverified"})

    def test_hum_alias_never_verifies(self):
        # 179.9 Hz is a standard tone AND the 3rd harmonic of 60 Hz mains.
        self.assertEqual(
            self._g(_t(expect={"ctcss_hz": 179.9}),
                    {"active": 1, "audio_bytes": 9, "ctcss_hz": 179.9,
                     "ctcss_suspect_hum": True}),
            {"ctcss_hz": "suspect"})

    def test_csq_claim_verified_by_absence(self):
        self.assertEqual(
            self._g(_t(expect={"ctcss_hz": None}),
                    {"active": 1, "audio_bytes": 9}),
            {"ctcss_hz": "verified"})

    def test_csq_claim_conflicts_when_tone_present(self):
        # Detecting a tone that should not exist IS positive evidence.
        self.assertEqual(
            self._g(_t(expect={"ctcss_hz": None}),
                    {"active": 1, "audio_bytes": 9, "ctcss_hz": 141.3}),
            {"ctcss_hz": "conflict"})

    def test_silent_check_grades_nothing(self):
        self.assertEqual(monitor.verify_params(self.tone, {}, False), {})

    def test_dcs_presence_without_code_is_unverified(self):
        out = monitor.verify_params(_t(expect={"dcs_code": "023"}),
                                    {"active": 1, "audio_bytes": 9,
                                     "dcs_present": True}, True)
        self.assertEqual(out["dcs_code"]["state"], "unverified")
        self.assertIn("not implemented", out["dcs_code"]["note"])

    def test_dcs_carrier_conflicts_with_no_dcs_claim(self):
        out = monitor.verify_params(_t(expect={"dcs_code": None}),
                                    {"active": 1, "audio_bytes": 9,
                                     "dcs_present": True}, True)
        self.assertEqual(out["dcs_code"]["state"], "conflict")

    def test_color_code_match_in_observed_set(self):
        tgt = _t(expect={"color_code": 7}, decoder="dmr")
        self.assertEqual(self._g(tgt, {"active": 1, "color_codes": [7, 1]}),
                         {"color_code": "verified"})
        self.assertEqual(self._g(tgt, {"active": 1, "color_codes": [1]}),
                         {"color_code": "conflict"})

    def test_no_claim_grades_nothing(self):
        self.assertEqual(monitor.verify_params(
            _t(expect={}), {"active": 1, "audio_bytes": 9,
                            "ctcss_hz": 141.3}, True), {})


class _FakeFix(Fix):
    def __init__(self):
        pass

    def as_tuple(self):
        return (41.9, -87.7, 180.0, "site")


class TestMonitorStore(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.store = Store(self.path)
        self.rx = "SN1"

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _chk(self, target, heard, meta, receiver=None):
        self.store.add_monitor_check(
            receiver or self.rx, target, heard, 14.0 if heard else 1.0,
            monitor.verify_params(target, meta, heard), meta, _FakeFix())

    def test_single_station_earns_verified(self):
        # Eric, 2026-09-28: one station CAN earn verified.
        tgt = _t(expect={"ctcss_hz": 141.3})
        meta = {"active": 1, "audio_bytes": 9, "ctcss_hz": 141.3}
        self._chk(tgt, True, meta)
        st = self.store.param_state(tgt["name"], tgt["freq_hz"])
        self.assertEqual(st["ctcss_hz"]["state"], "measured")
        self._chk(tgt, True, meta)
        st = self.store.param_state(tgt["name"], tgt["freq_hz"])
        self.assertEqual(st["ctcss_hz"]["state"], "verified")
        # ...but one station is not independent corroboration.
        self.assertFalse(st["ctcss_hz"]["corroborated"])

    def test_two_receivers_are_corroborated(self):
        tgt = _t(expect={"ctcss_hz": 141.3})
        meta = {"active": 1, "audio_bytes": 9, "ctcss_hz": 141.3}
        self._chk(tgt, True, meta, receiver="SN1")
        self._chk(tgt, True, meta, receiver="SN2")
        st = self.store.param_state(tgt["name"], tgt["freq_hz"])
        self.assertTrue(st["ctcss_hz"]["corroborated"])

    def test_conflict_is_sticky(self):
        tgt = _t(expect={"ctcss_hz": 141.3})
        self._chk(tgt, True, {"active": 1, "audio_bytes": 9,
                              "ctcss_hz": 107.2})
        for _ in range(3):
            self._chk(tgt, True, {"active": 1, "audio_bytes": 9,
                                  "ctcss_hz": 141.3})
        st = self.store.param_state(tgt["name"], tgt["freq_hz"])
        self.assertEqual(st["ctcss_hz"]["state"], "conflict")

    def test_silence_does_not_downgrade(self):
        tgt = _t(expect={"ctcss_hz": 141.3})
        meta = {"active": 1, "audio_bytes": 9, "ctcss_hz": 141.3}
        self._chk(tgt, True, meta)
        self._chk(tgt, True, meta)
        self._chk(tgt, False, {})
        st = self.store.param_state(tgt["name"], tgt["freq_hz"])
        self.assertEqual(st["ctcss_hz"]["state"], "verified")

    def test_silent_check_records_last_checked_but_not_last_heard(self):
        # The whole point of the table: "looked, heard nothing" is a row.
        tgt = _t()
        self._chk(tgt, False, {})
        row = self.store.monitor_status(receiver=self.rx)[0]
        self.assertEqual(row["checks"], 1)
        self.assertEqual(row["hearings"], 0)
        self.assertIsNone(row["last_heard"])
        self.assertIsNotNone(row["last_checked"])

    def test_status_includes_never_checked_targets(self):
        tgt = _t(name="Checked")
        other = _t(name="NeverChecked", freq_mhz=462.7)
        self._chk(tgt, True, {"active": 1, "audio_bytes": 9})
        rows = self.store.monitor_status(receiver=self.rx,
                                         targets=[tgt, other])
        names = {r["target"]: r for r in rows}
        self.assertIn("NeverChecked", names)
        self.assertEqual(names["NeverChecked"]["checks"], 0)
        self.assertIsNone(names["NeverChecked"]["last_checked"])

    def test_same_freq_two_targets_stay_separate(self):
        # Contested channels (two GMRS machines on 462.650 distinguished
        # only by tone) must not be folded together.
        a = _t(name="NSEA 650", freq_mhz=462.65,
               expect={"ctcss_hz": 107.2})
        b = _t(name="Forest View 650", freq_mhz=462.65,
               expect={"ctcss_hz": 203.5})
        self._chk(a, True, {"active": 1, "audio_bytes": 9,
                            "ctcss_hz": 107.2})
        self._chk(b, True, {"active": 1, "audio_bytes": 9,
                            "ctcss_hz": 107.2})
        rows = {r["target"]: r for r in
                self.store.monitor_status(receiver=self.rx)}
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows["NSEA 650"]["params"]["ctcss_hz"]["state"], "measured")
        self.assertEqual(
            rows["Forest View 650"]["params"]["ctcss_hz"]["state"], "conflict")

    def test_batch_does_not_claim_monitor_checks_yet(self):
        # build_batch() packages observations and beacons only -- ssrf-obs
        # has no ingest path for monitor checks yet.  Claiming them here
        # would mark them submitted without sending them, and
        # unsubmitted_monitor_checks() filters on batch_id IS NULL, so the
        # evidence could never be resent once the server side lands.
        # They must stay pending until that ingest exists.
        self._chk(_t(), False, {})
        self.assertEqual(len(self.store.unsubmitted_monitor_checks()), 1)
        self.store.mark_batched("batch-1", "{}")
        self.assertEqual(len(self.store.unsubmitted_monitor_checks()), 1)


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.store = Store(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_never_checked_targets_are_due(self):
        tgts = [_t(name="A"), _t(name="B", freq_mhz=462.7)]
        self.assertEqual(len(monitor.due_targets(self.store, "SN1", tgts)), 2)

    def test_just_checked_target_is_not_due(self):
        tgt = _t(name="A", interval_s=900)
        self.store.add_monitor_check("SN1", tgt, False, 1.0, {}, {},
                                     _FakeFix())
        self.assertEqual(monitor.due_targets(self.store, "SN1", [tgt]), [])

    def test_silent_check_still_resets_the_schedule(self):
        # If only hearings counted, a permanently quiet target would look
        # forever overdue and starve everything else.
        tgt = _t(name="Quiet", interval_s=900)
        self.store.add_monitor_check("SN1", tgt, False, 1.0, {}, {},
                                     _FakeFix())
        self.assertEqual(monitor.due_targets(self.store, "SN1", [tgt]), [])

    def test_priority_orders_equally_overdue_targets(self):
        hi = _t(name="hi", freq_mhz=462.65, priority=1)
        lo = _t(name="lo", freq_mhz=462.7, priority=3)
        due = monitor.due_targets(self.store, "SN1", [lo, hi])
        self.assertEqual([t["name"] for t in due], ["hi", "lo"])

    def test_budget_stops_before_starting_unfinishable_target(self):
        # budget_s=0 must check nothing rather than overrun the sweep.
        checked, heard, conflicts = monitor.run_due(
            0, self.store, "SN1", [_t(name="A", duration_s=20)],
            lambda: _FakeFix(), budget_s=0)
        self.assertEqual((checked, heard, conflicts), (0, 0, 0))


class TestPlanMonitorBlock(unittest.TestCase):
    def test_seed_plan_loads_and_resolves_targets(self):
        plan = config.load_plan(PLAN)
        mon = plan["monitor"]
        self.assertTrue(mon["resolved"])
        names = {t["name"]: t for t in mon["resolved"]}
        # The 462.550 repeater is a real overlay record (chioff-ssrf-shared),
        # not a hand-pinned guess: it carries an ssrf_id so evidence can be
        # written back against it.  ssrf-lite CORE disagrees on this
        # frequency (CTCSS 156.7 vs DCS 023) and that conflict is
        # deliberately left for monitoring to settle.
        chio = names["ChiO REPEATER"]
        self.assertEqual(chio["ssrf_id"], "asg_chio_repeater")
        self.assertEqual(chio["expect"]["dcs_code"], "023")
        self.assertEqual(chio["expect"]["dcs_polarity"], "N")
        self.assertNotIn("ctcss_hz", chio["expect"])
        # Catalog-derived NSEA target keeps its ssrf_id for writeback.
        self.assertEqual(names["NSEA 675"]["ssrf_id"], "asgn_nsea_675")
        self.assertEqual(names["NSEA 675"]["expect"]["ctcss_hz"], 141.3)

    def test_duty_pct_bounds_enforced(self):
        import yaml
        plan = yaml.safe_load(open(PLAN))
        plan["monitor"]["duty_pct"] = 95
        plan["monitor"].pop("targets_file", None)
        with tempfile.NamedTemporaryFile("w", suffix=".yml",
                                         delete=False) as f:
            yaml.safe_dump(plan, f)
            p = f.name
        try:
            with self.assertRaises(ConfigError):
                config.load_plan(p)
        finally:
            os.unlink(p)

    def test_discovery_only_plan_still_loads(self):
        import yaml
        plan = yaml.safe_load(open(PLAN))
        plan.pop("monitor")
        with tempfile.NamedTemporaryFile("w", suffix=".yml",
                                         delete=False) as f:
            yaml.safe_dump(plan, f)
            p = f.name
        try:
            loaded = config.load_plan(p)
            self.assertEqual(loaded["monitor"], {})
        finally:
            os.unlink(p)


if __name__ == "__main__":
    unittest.main()
