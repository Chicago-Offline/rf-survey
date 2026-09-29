"""Tests for the onboarding / first-run additions:
  - rfsurvey.plans  (bundled plan registry + resolve)
  - rfsurvey.deps   (dependency checks)
  - rfsurvey.setup_wizard (config rendering)
  - rfsurvey.references (BUNDLED_DIR relocated)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rfsurvey import plans, deps, references
from rfsurvey.setup_wizard import render_config, write_config


# ---------------------------------------------------------------------------
# plans
# ---------------------------------------------------------------------------

class TestBundledPlans(unittest.TestCase):
    def test_bundled_names_nonempty(self):
        names = plans.bundled_names()
        self.assertIn("uhf-dmr", names)
        self.assertIn("2m-fm", names)
        self.assertIn("chicago-ham", names)

    def test_bundled_names_no_target_files(self):
        # targets-*.yml are not plans; they must not appear in the list
        for name in plans.bundled_names():
            self.assertFalse(name.startswith("targets-"),
                             "targets file leaked into bundled_names: %s" % name)

    def test_bundled_path_exists(self):
        for name in plans.bundled_names():
            path = plans.bundled_path(name)
            self.assertIsNotNone(path)
            self.assertTrue(os.path.isfile(path), "missing: %s" % path)

    def test_bundled_catalog_returns_triples(self):
        catalog = plans.bundled_catalog()
        self.assertTrue(len(catalog) >= 3)
        for name, path, desc in catalog:
            self.assertIsInstance(name, str)
            self.assertTrue(os.path.isfile(path))
            self.assertIsInstance(desc, str)

    def test_chicago_ham_has_description(self):
        cat = {n: d for n, _p, d in plans.bundled_catalog()}
        self.assertTrue(cat.get("chicago-ham"), "chicago-ham missing description")

    def test_resolve_by_name(self):
        path = plans.resolve("uhf-dmr")
        self.assertTrue(os.path.isfile(path))

    def test_resolve_by_name_with_suffix(self):
        path = plans.resolve("uhf-dmr.yml")
        self.assertTrue(os.path.isfile(path))

    def test_resolve_legacy_example_path(self):
        # Old docs said: survey run examples/plan-uhf-dmr.yml
        # That file no longer exists on disk; it must resolve to the bundled copy.
        path = plans.resolve("examples/plan-uhf-dmr.yml")
        self.assertTrue(os.path.isfile(path))
        self.assertIn("uhf-dmr", os.path.basename(path))

    def test_resolve_plan_prefix_stripped(self):
        # plan-uhf-dmr should also resolve
        path = plans.resolve("plan-uhf-dmr")
        self.assertTrue(os.path.isfile(path))

    def test_resolve_existing_file_wins(self):
        # A real path on disk always wins over a bundled name
        real = plans.bundled_path("uhf-dmr")
        path = plans.resolve(real)
        self.assertEqual(path, real)

    def test_resolve_unknown_raises(self):
        with self.assertRaises(plans.PlanError):
            plans.resolve("no-such-plan-xyzzy")

    def test_bundled_plans_loadable(self):
        # Each bundled plan should pass config.load_plan without error
        from rfsurvey import config as config_mod
        for name, path, _desc in plans.bundled_catalog():
            try:
                plan = config_mod.load_plan(path)
            except Exception as exc:
                self.fail("load_plan(%s) raised: %s" % (name, exc))
            self.assertEqual(plan["name"], name,
                             "plan 'name:' key should match filename for %s" % name)


# ---------------------------------------------------------------------------
# deps
# ---------------------------------------------------------------------------

class TestDeps(unittest.TestCase):
    def test_check_all_returns_list(self):
        results = deps.check_all()
        self.assertIsInstance(results, list)
        self.assertTrue(len(results) > 0)

    def test_all_results_have_required_keys(self):
        for r in deps.check_all():
            for k in ("name", "status", "detail", "required"):
                self.assertIn(k, r, "result missing key %r: %s" % (k, r))

    def test_status_values_valid(self):
        valid = {deps.OK, deps.MISSING, deps.WARN}
        for r in deps.check_all():
            self.assertIn(r["status"], valid)

    def test_format_results_is_string(self):
        results = deps.check_all()
        out = deps.format_results(results)
        self.assertIsInstance(out, str)
        self.assertTrue(len(out) > 0)

    def test_aggregate_hint_none_when_all_present(self):
        # On CI all rtl-sdr tools may be missing; skip if not.
        import shutil
        all_present = all(shutil.which(name) for name, _pkg, _why
                          in deps.REQUIRED_BINS + deps.SHELL_BINS)
        if not all_present:
            self.skipTest("not all required bins present on this host")
        results = deps.check_all()
        self.assertIsNone(deps.aggregate_install_hint(results))

    def test_os_family_string(self):
        family = deps.FAMILY
        self.assertIn(family, ("debian", "fedora", "arch", "macos", "linux"))

    def test_install_hint_rtlsdr(self):
        hint = deps.install_hint("rtl-sdr")
        self.assertIsNotNone(hint)
        self.assertIn("rtl", hint)


# ---------------------------------------------------------------------------
# references relocation
# ---------------------------------------------------------------------------

class TestReferences(unittest.TestCase):
    def test_bundled_dir_inside_package(self):
        pkg_dir = os.path.dirname(os.path.abspath(
            __import__("rfsurvey.references", fromlist=["references"]).__file__))
        self.assertTrue(
            references.BUNDLED_DIR.startswith(pkg_dir),
            "BUNDLED_DIR %s is not inside the package at %s"
            % (references.BUNDLED_DIR, pkg_dir))

    def test_chicago_region_loadable(self):
        refs = references.load_references(region="chicago")
        self.assertTrue(len(refs) > 0)

    def test_bundled_regions_contains_chicago(self):
        self.assertIn("chicago", references.bundled_regions())


# ---------------------------------------------------------------------------
# setup_wizard config rendering
# ---------------------------------------------------------------------------

class TestRenderConfig(unittest.TestCase):
    LOCATION_STATIC = {
        "mode": "static", "lat": 41.88, "lon": -87.63, "alt_m": 180
    }
    DEVICE_CFG = {
        "BENCH": {
            "role": "digital", "gain": 49.6,
            "antenna": {"model": "discone", "type": "discone",
                        "gain_dbi": 2.0, "bands_mhz": [[25, 1300]]},
            "placement": {"location": "attic", "height_m": 6},
        }
    }

    def test_renders_without_error(self):
        text = render_config("mystation", self.LOCATION_STATIC,
                             self.DEVICE_CFG,
                             "~/.local/share/rf-survey/observations.db",
                             "~/.config/rf-survey/station.key")
        self.assertIsInstance(text, str)
        self.assertIn("mystation", text)
        self.assertIn("41.88", text)
        self.assertIn("BENCH", text)
        self.assertIn("discone", text)
        self.assertIn("bands_mhz", text)

    def test_renders_gpsd_location(self):
        loc = {"mode": "gpsd", "host": "127.0.0.1", "port": 2947}
        text = render_config("gpsd-station", loc, {},
                             "~/.local/share/rf-survey/observations.db",
                             "~/.config/rf-survey/station.key")
        self.assertIn("gpsd", text)
        self.assertIn("2947", text)

    def test_renders_empty_devices(self):
        text = render_config("nodev", self.LOCATION_STATIC, {},
                             "~/.local/share/rf-survey/observations.db",
                             "~/.config/rf-survey/station.key")
        self.assertIn("devices: {}", text)

    def test_no_trailing_dot_zero(self):
        """Non-integer floats render as-is; integer-valued floats drop the .0."""
        text = render_config("s", self.LOCATION_STATIC, self.DEVICE_CFG,
                             "~/.local/share/rf-survey/observations.db",
                             "~/.config/rf-survey/station.key")
        # gain 49.6 is a real float — must appear as "49.6", not "49"
        self.assertIn("gain: 49.6", text)
        # height_m 6.0 is integer-valued — must appear as "6", not "6.0"
        self.assertIn("height_m: 6", text)
        self.assertNotIn("height_m: 6.0", text)

    def test_write_config_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "config.yml")
            text = "db: /tmp/test.db\n"
            written, backup = write_config(path, text)
            self.assertTrue(os.path.isfile(written))
            self.assertIsNone(backup)
            with open(written) as fh:
                self.assertEqual(fh.read(), text)

    def test_write_config_backs_up_existing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.yml")
            with open(path, "w") as fh:
                fh.write("old: content\n")
            written, backup = write_config(path, "new: content\n")
            self.assertIsNotNone(backup)
            self.assertTrue(os.path.isfile(backup))
            with open(backup) as fh:
                self.assertEqual(fh.read(), "old: content\n")
            with open(written) as fh:
                self.assertEqual(fh.read(), "new: content\n")


if __name__ == "__main__":
    unittest.main()
