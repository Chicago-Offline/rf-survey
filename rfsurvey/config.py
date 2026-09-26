"""Config + scan plan loading."""
import os
import yaml

DEFAULT_CONFIG = os.path.expanduser("~/.config/rf-survey/config.yml")


class ConfigError(Exception):
    pass


def load_yaml(path):
    with open(os.path.expanduser(path)) as f:
        return yaml.safe_load(f) or {}


def load_config(path=None):
    """Site config: location, device roles, db path."""
    path = path or os.environ.get("RF_SURVEY_CONFIG", DEFAULT_CONFIG)
    cfg = load_yaml(path) if os.path.exists(os.path.expanduser(path)) else {}
    cfg.setdefault("db", "~/.local/share/rf-survey/observations.db")
    cfg.setdefault("location", {"mode": "static"})
    cfg.setdefault("devices", {})  # serial -> {role, tuner, gain, floor_offset_db}
    return cfg


REQUIRED_PLAN_KEYS = ("name", "bands")


def load_plan(path):
    """Scan plan: bands, sweep params, dwell rules.

    bands: [{start_mhz, stop_mhz, step_khz}]
    sweep: {integration_s, gain, passes}
    dwell: {snr_db, min_hits, duration_s, decoder, squelch_db}
    """
    plan = load_yaml(path)
    for k in REQUIRED_PLAN_KEYS:
        if k not in plan:
            raise ConfigError(f"scan plan missing required key: {k}")
    plan.setdefault("sweep", {})
    plan["sweep"].setdefault("integration_s", 10)
    plan["sweep"].setdefault("gain", None)  # None = tuner AGC off, max sane gain chosen by device profile
    plan["sweep"].setdefault("passes", 0)   # 0 = run forever
    plan.setdefault("dwell", {})
    plan["dwell"].setdefault("snr_db", 12.0)
    plan["dwell"].setdefault("min_hits", 3)
    plan["dwell"].setdefault("duration_s", 60)
    plan["dwell"].setdefault("decoder", "nfm")
    plan["dwell"].setdefault("squelch_db", 6.0)
    for b in plan["bands"]:
        for k in ("start_mhz", "stop_mhz", "step_khz"):
            if k not in b:
                raise ConfigError(f"band missing {k}: {b}")
    return plan
