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

    bands: [{start_mhz, stop_mhz, step_khz, channel_raster_hz?, dwell?}]
    sweep: {integration_s, gain, passes, channel_raster_hz?}

    channel_raster_hz is the channel grid used to name a measured
    carrier.  It belongs per band, because one plan can span services
    with different grids: 6250 suits UHF/800 narrowband, while VHF land
    mobile, railroad AAR and 2m all land on multiples of 2500.  A band
    setting wins over the plan-level one; unset means 6250.
    dwell: {snr_db, min_hits, duration_s, decoder, squelch_db}

    A band may carry its own partial "dwell" block, merged over the
    plan-level one (engine.band_dwell), for the same reason the raster
    belongs per band: one plan can span services that need different
    decoders.  450-470 business LMR wants dmr, while 851-869 is trunked
    and stays on energy-only nfm until trunking decode is integrated
    (NETWORK.md S4).  Only the keys you set are overridden.
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
    for scope in [plan["sweep"]] + list(plan["bands"]):
        raster = scope.get("channel_raster_hz")
        if raster is not None and not (
                isinstance(raster, (int, float)) and raster > 0):
            raise ConfigError(
                f"channel_raster_hz must be a positive number: {raster!r}")
    # Reject an unknown decoder at load time. Otherwise the typo survives
    # startup and only raises deep inside the first dwell, minutes into an
    # unattended run, then again on every subsequent hit.
    from .dwell import DECODERS
    for scope in [plan["dwell"]] + [b.get("dwell") or {} for b in plan["bands"]]:
        dec = scope.get("decoder")
        if dec is not None and dec not in DECODERS:
            raise ConfigError(
                f"unknown decoder {dec!r}: known decoders are "
                f"{', '.join(sorted(DECODERS))}")

    # ---- monitor block (optional) ----
    # Monitoring is the other half of the mission: bands answer "what is
    # out there", targets answer "is this specific known system still
    # there, and are its published parameters real".  Optional, so every
    # existing discovery-only plan keeps loading unchanged.
    mon = plan.get("monitor") or {}
    if mon:
        mon.setdefault("interval_s", 900)   # revisit each target this often
        mon.setdefault("duration_s", 20)    # shorter than a discovery dwell
        mon.setdefault("decoder", "nfm")
        mon.setdefault("squelch_db", 6.0)
        # Share of each pass's airtime given to monitoring.  Capped well
        # below 100 because discovery must not starve: the two missions
        # share one dongle, and a plan that spends all its time
        # confirming what we already know stops finding anything new.
        mon.setdefault("duty_pct", 30)
        duty = mon["duty_pct"]
        if not (isinstance(duty, (int, float)) and 0 < duty <= 80):
            raise ConfigError(
                f"monitor.duty_pct must be >0 and <=80: {duty!r}")
        for key in ("interval_s", "duration_s"):
            v = mon[key]
            if not (isinstance(v, (int, float)) and v > 0):
                raise ConfigError(f"monitor.{key} must be positive: {v!r}")
        dec = mon.get("decoder")
        if dec is not None and dec not in DECODERS:
            raise ConfigError(
                f"unknown monitor decoder {dec!r}: known decoders are "
                f"{', '.join(sorted(DECODERS))}")
        # Resolve targets now so a bad target file or a misspelled expect
        # key fails at startup, not hours into an unattended run -- same
        # reasoning as the decoder check above.
        from .targets import load_targets
        targets = load_targets(mon, base_dir=os.path.dirname(
            os.path.abspath(os.path.expanduser(path))))
        for t in targets:
            if t["decoder"] not in DECODERS:
                raise ConfigError(
                    f"monitor target {t['name']!r}: unknown decoder "
                    f"{t['decoder']!r}")
        mon["resolved"] = targets
    plan["monitor"] = mon
    return plan
