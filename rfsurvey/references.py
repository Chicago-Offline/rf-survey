"""Reference beacon sets and per-observer antenna coverage (NETWORK.md S7).

Beacons bound HARDWARE variance, not propagation.  The coverage rules here
are the mechanical half of S7 rule 5: an observer is only scored against
references its antenna can actually hear, and an observer that cannot hear a
band is `no_reference` for it -- never healthy by omission.
"""
import os

from .config import ConfigError, load_yaml

SCHEMA_ID = "rfsurvey.references.v1"

# Bundled sets live inside the package so they are available after
# `pipx install`.  During a repo checkout the old top-level references/
# directory is gone (contents moved to rfsurvey/data/references/ by the
# onboarding-setup commit), but a dev clone will have the data there too.
BUNDLED_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "references")

REQUIRED_REF_KEYS = ("id", "freq_hz", "band", "kind")

# Coverage verdicts.
OK = "ok"                      # antenna declared and covers this reference
NO_REFERENCE = "no_reference"  # antenna declared and does NOT cover it
UNVERIFIED = "unverified"      # no antenna.bands_mhz declared -- unknown reach


def bundled_regions():
    if not os.path.isdir(BUNDLED_DIR):
        return []
    return sorted(f[:-4] for f in os.listdir(BUNDLED_DIR)
                  if f.endswith(".yml"))


def resolve_path(path=None, region="chicago"):
    """Explicit path wins; otherwise the bundled set for the region."""
    if path:
        p = os.path.expanduser(path)
        if not os.path.exists(p):
            raise ConfigError(f"reference set not found: {p}")
        return p
    p = os.path.join(BUNDLED_DIR, f"{region}.yml")
    if not os.path.exists(p):
        raise ConfigError(
            f"no bundled reference set for region {region!r} "
            f"(have: {bundled_regions()})")
    return p


def load_references(path=None, region="chicago"):
    """Load and validate a reference set -> list of dicts."""
    p = resolve_path(path, region)
    doc = load_yaml(p)
    schema = doc.get("schema")
    if schema != SCHEMA_ID:
        raise ConfigError(
            f"{p}: expected schema {SCHEMA_ID}, got {schema!r}")
    refs = doc.get("references") or []
    seen = set()
    for r in refs:
        for k in REQUIRED_REF_KEYS:
            if k not in r:
                raise ConfigError(f"{p}: reference missing {k}: {r}")
        if r["id"] in seen:
            raise ConfigError(f"{p}: duplicate reference id {r['id']!r}")
        seen.add(r["id"])
        r.setdefault("bandwidth_hz", 16000)
        r.setdefault("expect", "strong")
        r.setdefault("modulation", "nfm")
    return refs


def antenna_bands(device_cfg):
    """[(low_hz, high_hz)] from devices.<serial>.antenna.bands_mhz, or None.

    None means undeclared -- unknown reach, not universal reach.

    bands_mhz is USABLE RX REACH, not resonant/design bands.  An HT
    dual-band whip is nominally 2m/70cm but receives roughly 136-174 and
    400-520, and hears NOAA 162.550 without trouble.  Entering the resonant
    bands instead marks every reference out of range and silently disables
    calibration for that observer -- the failure is quiet, so prefer the
    receive spec when in doubt.
    """
    ant = (device_cfg or {}).get("antenna") or {}
    bands = ant.get("bands_mhz")
    if not bands:
        return None
    out = []
    for b in bands:
        try:
            lo, hi = float(b[0]), float(b[1])
        except (TypeError, ValueError, IndexError):
            raise ConfigError(
                f"antenna.bands_mhz entries must be [low_mhz, high_mhz]: {b!r}")
        if hi < lo:
            lo, hi = hi, lo
        out.append((lo * 1e6, hi * 1e6))
    return out


def coverage(ref, device_cfg):
    """Can this observer legitimately be scored on this reference?"""
    bands = antenna_bands(device_cfg)
    if bands is None:
        return UNVERIFIED
    f = float(ref["freq_hz"])
    return OK if any(lo <= f <= hi for lo, hi in bands) else NO_REFERENCE


def plan_for(refs, device_cfg, include_uncovered=True):
    """[(ref, verdict)] for one observer.

    Every reference is measured by default, INCLUDING `no_reference` ones
    (Eric, 2026-09-28): an off-band antenna still receives, just badly, and
    the reading is worth having -- a blowtorch FM station that vanishes on a
    2 m whip is still a front-end datum.  What protects the scores is the
    verdict travelling WITH the reading: `no_reference` marks it
    unscoreable, exactly like `unverified` marks an undeclared antenna.
    The S7 rule 5 fence is about SCORING, not about refusing to look.

    include_uncovered=False restores the old skip-them behavior for callers
    that only want scoreable measurements.
    """
    out = []
    for r in refs:
        v = coverage(r, device_cfg)
        if v == NO_REFERENCE and not include_uncovered:
            continue
        out.append((r, v))
    return out
