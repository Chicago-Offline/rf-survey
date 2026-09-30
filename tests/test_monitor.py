import os, sys, tempfile, time, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rfsurvey import (config, engine, monitor, submit as submit_mod,
                      targets as targets_mod)
from rfsurvey.config import ConfigError
from rfsurvey.store import Store
from rfsurvey.location import Fix

PLAN = os.path.join(os.path.dirname(__file__), "..", "plans",
                    "plan-chicago-monitor.yml")


class _FakeStation:
    station_id = "test-station"
    public_key_b64 = "fake"

    def sign(self, batch):
        return {"batch": batch, "sig": "unsigned-test",
                "station_id": self.station_id}


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

    def test_batch_sends_monitor_checks_then_claims_them(self):
        # Stamping a batch_id on monitor_checks is only safe BECAUSE the
        # batch now carries them, so both halves are asserted together.
        # If build_batch() ever stops sending them while mark_batched()
        # keeps claiming them, the evidence is marked submitted without
        # ever leaving the station and can never be resent
        # (unsubmitted_monitor_checks() filters on batch_id IS NULL).
        # That regression must fail here.
        self._chk(_t(), False, {})
        self.assertEqual(len(self.store.unsubmitted_monitor_checks()), 1)
        env = submit_mod.build_batch(self.store, _FakeStation())
        sent = env["batch"]["monitor_checks"]
        self.assertEqual(len(sent), 1, "silent check must be SENT, not just claimed")
        self.assertFalse(sent[0]["heard"])
        self.assertEqual(len(self.store.unsubmitted_monitor_checks()), 0)
        # ...and not sent twice.
        self.assertIsNone(submit_mod.build_batch(self.store, _FakeStation()))

    def test_silent_only_station_still_produces_a_batch(self):
        # A station whose targets are all quiet has nothing in
        # observations/beacons.  It must still submit: "we looked at 38
        # Chicago repeaters and heard nothing" is the evidence that
        # separates stale from never-visited.  Before monitor_checks were
        # batched this returned None and the station stayed silent.
        self._chk(_t(name="Quiet A"), False, {})
        self._chk(_t(name="Quiet B", freq_mhz=462.7), False, {})
        env = submit_mod.build_batch(self.store, _FakeStation())
        self.assertIsNotNone(env)
        b = env["batch"]
        self.assertEqual(b["observations"], [])
        self.assertEqual(len(b["monitor_checks"]), 2)

    def test_batched_check_preserves_catalog_join_and_grades(self):
        # The add_observation side effect loses target name, ssrf_id and
        # the param grades, so a heard channel arriving only as a generic
        # observation cannot be tied back to its ssrf-lite record.
        tgt = _t(name="NS9RC 145.470", freq_mhz=145.47,
                 expect={"ctcss_hz": 107.2}, ssrf_id="ns9rc:145470")
        self._chk(tgt, True, {"active": 1, "audio_bytes": 9,
                              "ctcss_hz": 107.2})
        sent = submit_mod.build_batch(
            self.store, _FakeStation())["batch"]["monitor_checks"][0]
        self.assertEqual(sent["target"], "NS9RC 145.470")
        self.assertEqual(sent["ssrf_id"], "ns9rc:145470")
        self.assertEqual(sent["freq_hz"], 145_470_000)
        self.assertTrue(sent["heard"])
        self.assertEqual(sent["receiver"], self.rx)
        self.assertIsInstance(sent["params"], dict)
        self.assertIsInstance(sent["meta"], dict)


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


class TestMonitorOnlyPlan(unittest.TestCase):
    """A plan may skip discovery entirely and only check known channels."""

    def _load(self, body):
        with tempfile.NamedTemporaryFile("w", suffix=".yml",
                                         delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return config.load_plan(p)
        finally:
            os.unlink(p)

    MON_ONLY = ("name: targeted\n"
                "monitor:\n"
                "  targets:\n"
                "    - {name: WA9ORC, freq_mhz: 144.75, duration_s: 40}\n"
                "    - {name: RED Fire, freq_mhz: 154.13, duration_s: 30}\n")

    def test_loads_without_bands(self):
        plan = self._load(self.MON_ONLY)
        self.assertEqual(plan["bands"], [])
        self.assertEqual(len(plan["monitor"]["resolved"]), 2)

    def test_budget_defaults_to_one_full_pass(self):
        # duty_pct has no referent without a sweep, so the budget must be
        # pinned: enough airtime to visit every target once.
        plan = self._load(self.MON_ONLY)
        self.assertEqual(plan["monitor"]["budget_s"], (40 + 5) + (30 + 5))

    def test_budget_floor(self):
        plan = self._load("name: t\nmonitor:\n  targets:\n"
                          "    - {name: A, freq_mhz: 144.75, duration_s: 5}\n")
        self.assertEqual(plan["monitor"]["budget_s"], 60.0)

    def test_explicit_budget_wins(self):
        plan = self._load(self.MON_ONLY + "  budget_s: 300\n")
        self.assertEqual(plan["monitor"]["budget_s"], 300)

    def test_bad_budget_rejected(self):
        for bad in ("0", "-5", "nope"):
            with self.assertRaises(ConfigError):
                self._load(self.MON_ONLY + "  budget_s: %s\n" % bad)

    def test_no_bands_and_no_targets_rejected(self):
        # Would claim a dongle and do nothing at all.
        with self.assertRaises(ConfigError) as cm:
            self._load("name: empty\n")
        self.assertIn("nothing to do", str(cm.exception))

    def test_empty_targets_list_rejected(self):
        with self.assertRaises(ConfigError):
            self._load("name: empty\nmonitor:\n  targets: []\n")

    def test_bands_only_plan_gets_no_budget(self):
        # Discovery-only plans must keep loading exactly as before.
        plan = self._load(
            "name: sweep\nbands:\n"
            "  - {start_mhz: 450, stop_mhz: 470, step_khz: 6.25}\n")
        self.assertEqual(plan["monitor"], {})


class TestNextDueIn(unittest.TestCase):
    """Paces the monitor-only loop; without it the engine spins hot."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.store = Store(self.path)
        self.rx = "SN1"

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def _check(self, target, ts):
        self.store.add_monitor_check(
            self.rx, target, False, 1.0, {}, {"active": 0}, _FakeFix())
        self.store.db.execute(
            "UPDATE monitor_checks SET ts=? WHERE target=?",
            (ts, target["name"]))
        self.store.db.commit()

    def test_never_checked_is_due_now(self):
        t = _t(name="Fresh", interval_s=900)
        self.assertEqual(
            monitor.next_due_in(self.store, self.rx, [t]), 0.0)

    def test_no_targets_is_due_now(self):
        self.assertEqual(monitor.next_due_in(self.store, self.rx, []), 0.0)

    def test_waits_for_soonest_target(self):
        now = time.time()
        soon = _t(name="Soon", freq_mhz=144.75, interval_s=600)
        later = _t(name="Later", freq_mhz=145.11, interval_s=3600)
        self._check(soon, now - 300)    # 300s left
        self._check(later, now - 300)   # 3300s left
        wait = monitor.next_due_in(self.store, self.rx, [soon, later],
                                   now=now)
        self.assertAlmostEqual(wait, 300, delta=2)

    def test_overdue_target_returns_zero_not_negative(self):
        now = time.time()
        t = _t(name="Overdue", interval_s=600)
        self._check(t, now - 1200)
        self.assertEqual(
            monitor.next_due_in(self.store, self.rx, [t], now=now), 0.0)


class TestMonitorOnlyEngineLoop(unittest.TestCase):
    """The engine loop must not spin when there is no sweep to pace it."""

    MON_ONLY = ("name: paced\n"
                "sweep:\n  passes: 3\n"
                "monitor:\n"
                "  budget_s: 60\n"
                "  targets:\n"
                "    - {name: A, freq_mhz: 144.75}\n"
                "    - {name: B, freq_mhz: 462.55}\n")

    WITH_BANDS = ("name: both\n"
                  "sweep:\n  passes: 2\n"
                  "bands:\n"
                  "  - {start_mhz: 450, stop_mhz: 452, step_khz: 12.5}\n"
                  "monitor:\n"
                  "  targets:\n"
                  "    - {name: A, freq_mhz: 144.75}\n")

    def _plan(self, body):
        with tempfile.NamedTemporaryFile("w", suffix=".yml",
                                         delete=False) as f:
            f.write(body)
            p = f.name
        try:
            return config.load_plan(p)
        finally:
            os.unlink(p)

    def _run(self, plan):
        """Run the engine loop with the radio and the clock stubbed out."""
        db = tempfile.mktemp(suffix=".db")
        cfg = {"db": db, "location": {"mode": "static", "lat": 41.9,
                                      "lon": -87.6, "alt_m": 190},
               "devices": {"SN1": {"gain": 36}}}
        calls = {"sweeps": 0, "monitor": 0, "sleeps": []}

        class _Claim:
            index = 0

            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _run_due(*a, **kw):
            calls["monitor"] += 1
            return (2, 0, 0)

        def _sweep(*a, **kw):
            calls["sweeps"] += 1
            return []

        try:
            with mock.patch.object(engine.devices, "Claim", _Claim), \
                 mock.patch.object(engine.monitor_mod, "run_due", _run_due), \
                 mock.patch.object(engine.sweep, "run_rtl_power", _sweep), \
                 mock.patch.object(engine.monitor_mod, "next_due_in",
                                   lambda *a, **kw: 540.0), \
                 mock.patch.object(engine.time, "sleep",
                                   lambda s: calls["sleeps"].append(s)):
                engine.run(cfg, plan, "SN1")
        finally:
            if os.path.exists(db):
                os.remove(db)
        return calls

    def test_monitor_only_never_sweeps(self):
        calls = self._run(self._plan(self.MON_ONLY))
        self.assertEqual(calls["sweeps"], 0)
        self.assertEqual(calls["monitor"], 3)   # sweep.passes: 3

    def test_monitor_only_idles_between_passes(self):
        # Without this the loop would burn a core re-asking run_due for
        # targets that are not due for another nine minutes.
        calls = self._run(self._plan(self.MON_ONLY))
        self.assertTrue(calls["sleeps"], "monitor-only loop did not idle")
        # Capped at 60s even though next_due_in said 540s, so a plan edit
        # or clock jump is still noticed promptly.
        self.assertTrue(all(0 < s <= 60.0 for s in calls["sleeps"]),
                        calls["sleeps"])

    def test_last_pass_does_not_idle_before_exiting(self):
        calls = self._run(self._plan(self.MON_ONLY))
        self.assertEqual(len(calls["sleeps"]), 2)   # 3 passes, 2 gaps

    def test_plan_with_bands_still_sweeps_and_does_not_idle(self):
        calls = self._run(self._plan(self.WITH_BANDS))
        self.assertEqual(calls["sweeps"], 2)
        self.assertEqual(calls["monitor"], 2)
        self.assertEqual(calls["sleeps"], [])


if __name__ == "__main__":
    unittest.main()
