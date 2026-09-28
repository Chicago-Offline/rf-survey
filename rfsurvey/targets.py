"""Monitor targets: the channels we already care about.

This is the half of the system that ssrf-lite drives.  A *target* is a
claim from the catalog -- "NSEA 675 is a GMRS repeater on 462.675 with
141.3 Hz PL" -- and the monitor's job is to go look at it on a schedule
whether or not the sweep saw energy there.

Why targets are not bands: a band is a question ("what is out there
between 462 and 463?"), a target is a hypothesis ("this specific system
exists with these specific parameters").  A band that happens to contain
a target cannot answer "when was NSEA 675 last heard", because a quiet
channel produces no sweep hit and therefore no record at all -- silence
and never-looked are indistinguishable.  Targets fix that by recording
every check, including the silent ones.

The `expect` block is what makes verification possible.  Each key is a
parameter the catalog claims; the monitor compares what it measured
against it and grades per-parameter (see monitor.verify_params).  The
distinction that matters:

  expect: {ctcss_hz: 141.3}   -- catalog claims a tone; measure it
  expect: {ctcss_hz: null}    -- catalog claims CSQ / no tone on output
  expect: {}                  -- catalog says nothing; nothing to verify

Those three are NOT the same.  A missing ctcss_rx_hz in ssrf-lite can
genuinely mean "carrier squelch output" -- the O'Hare .575 GMRS machine
is documented that way, PL on the input only, and programming a receive
tone squelches it out.  So "absent" must be expressible as a positive
claim, distinct from "unknown".  `null` is the claim, omission is the
unknown.
"""
import os

import yaml

from .config import ConfigError, load_yaml

# Parameters a target may claim.  Anything else in `expect` is a typo and
# is rejected at load time rather than silently never verified -- a
# misspelled expectation that quietly grades "no expectation" is worse
# than no expectation at all, because the page reads clean.
EXPECT_KEYS = ("ctcss_hz", "dcs_code", "dcs_polarity", "color_code",
               "timeslots", "bandwidth_khz", "mode")

# Sentinel distinguishing "claimed absent" from "not claimed".  yaml `null`
# lands as None, and None is also what .get() returns for a missing key,
# so presence must be tested with `in`, never by truthiness.
CLAIMED_ABSENT = None


def _freq_hz(t):
    if "freq_hz" in t:
        return int(t["freq_hz"])
    if "freq_mhz" in t:
        # Round, never truncate: 462.675 * 1e6 is 462674999.99... in binary
        # float, and int() would silently shift the target 1 Hz low.
        return int(round(float(t["freq_mhz"]) * 1e6))
    raise ConfigError(f"monitor target needs freq_mhz or freq_hz: {t}")


def normalize(t, defaults=None):
    """One raw YAML target -> the dict the scheduler and store use."""
    defaults = defaults or {}
    if not isinstance(t, dict):
        raise ConfigError(f"monitor target must be a mapping: {t!r}")
    out = dict(t)
    out["freq_hz"] = _freq_hz(t)
    out.pop("freq_mhz", None)
    if not out.get("name"):
        raise ConfigError(f"monitor target needs a name: {t}")
    # ssrf_id is the join back to the catalog.  Optional, because a target
    # can be a local hypothesis not yet in ssrf-lite, but without it no
    # observation can ever be written back to a catalog record.
    out.setdefault("ssrf_id", None)
    out.setdefault("priority", 2)
    out.setdefault("decoder", defaults.get("decoder", "nfm"))
    out.setdefault("duration_s", defaults.get("duration_s", 20))
    out.setdefault("squelch_db", defaults.get("squelch_db", 6.0))
    out.setdefault("interval_s", defaults.get("interval_s", 900))

    expect = out.get("expect")
    if expect is None:
        expect = {}
    if not isinstance(expect, dict):
        raise ConfigError(
            f"monitor target {out['name']!r}: expect must be a mapping")
    unknown = set(expect) - set(EXPECT_KEYS)
    if unknown:
        raise ConfigError(
            f"monitor target {out['name']!r}: unknown expect key(s) "
            f"{', '.join(sorted(unknown))}; known keys are "
            f"{', '.join(EXPECT_KEYS)}")
    out["expect"] = expect

    if not isinstance(out["priority"], int) or out["priority"] < 1:
        raise ConfigError(
            f"monitor target {out['name']!r}: priority must be a positive int")
    return out


def load_targets(monitor, base_dir=None):
    """Targets from a plan's `monitor` block: inline list and/or a file.

    Both are allowed together so a plan can pull in a generated catalog
    export and still pin a couple of local hypotheses by hand.
    """
    if not monitor:
        return []
    defaults = {k: monitor[k] for k in
                ("decoder", "duration_s", "squelch_db", "interval_s")
                if k in monitor}
    raw = list(monitor.get("targets") or [])
    # targets_file takes one path or a list, because the natural unit of a
    # generated file is one catalog scope (NSEA GMRS, local ham, ...) and a
    # plan usually wants several.  Forcing one file per plan would mean
    # either regenerating a merged file on every catalog change or
    # duplicating the plan per service.
    files = monitor.get("targets_file") or []
    if isinstance(files, str):
        files = [files]
    for path in files:
        path = os.path.expanduser(path)
        if base_dir and not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        if not os.path.exists(path):
            raise ConfigError(f"monitor targets_file not found: {path}")
        doc = load_yaml(path)
        raw += list((doc or {}).get("targets") or [])

    out = [normalize(t, defaults) for t in raw]

    # Duplicate (freq, name) pairs would each get their own schedule slot
    # and quietly halve the revisit rate of everything else.
    seen = {}
    for t in out:
        key = (t["freq_hz"], t["name"])
        if key in seen:
            raise ConfigError(
                f"duplicate monitor target {t['name']!r} on "
                f"{t['freq_hz']/1e6:.4f} MHz")
        seen[key] = True
    return out


def dump_targets(targets):
    """Serialize back to the targets_file shape (for generators)."""
    rows = []
    for t in targets:
        r = {"name": t["name"], "freq_mhz": round(t["freq_hz"] / 1e6, 6)}
        for k in ("ssrf_id", "decoder", "priority", "interval_s",
                  "duration_s"):
            if t.get(k) is not None:
                r[k] = t[k]
        if t.get("expect"):
            r["expect"] = t["expect"]
        rows.append(r)
    return yaml.safe_dump({"targets": rows}, sort_keys=False,
                          default_flow_style=False)
