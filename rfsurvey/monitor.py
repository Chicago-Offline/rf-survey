"""Scheduled monitoring of known channels, and parameter verification.

Discovery (sweep -> gated dwell, engine.py) answers "what is out there".
Monitoring answers two different questions that discovery structurally
cannot:

  1. When was this specific channel last heard -- which requires a record
     of having looked and heard nothing, not just an absence of rows.
  2. Do its real parameters match what the catalog claims -- which
     requires knowing what was claimed before you measure.

Both are recorded per check, so "last heard 3m ago" and "checked 40s ago,
silent" are separately answerable, and a parameter grade always carries
the measurement it came from.

Airtime: monitoring and discovery share one dongle, so the monitor takes
a bounded slice of each pass (monitor.duty_pct) and yields the radio back.
It never blocks a sweep indefinitely, and it never holds the device --
engine.py owns the claim and passes the index in.
"""
import logging
import time

from . import dwell as dwell_mod

log = logging.getLogger("rfsurvey")

# Per-parameter verification verdicts.
VERIFIED = "verified"        # measured, and it matches the catalog
CONFLICT = "conflict"        # measured, and it does NOT match the catalog
UNVERIFIED = "unverified"    # claimed, but not measurable from this check
NO_CLAIM = "no_claim"        # catalog says nothing -- nothing to verify
SUSPECT = "suspect"          # measured, but the measurement is not trustworthy

# CTCSS tones are spaced ~0.5 Hz apart at the bottom of the list, and the
# detector bins at +/-0.8 Hz, so anything inside 0.5 Hz is the same tone.
CTCSS_MATCH_HZ = 0.5


def _grade_ctcss(expect, meta):
    """Grade an observed subaudible tone against the catalog's claim.

    Asymmetry that matters: measuring no tone does NOT disprove a claimed
    tone.  A short dwell on a weak or briefly-keyed signal routinely fails
    to resolve CTCSS (the detector needs >=4 s of unsquelched audio just to
    resolve the tone spacing), so "claimed 141.3, heard nothing" is
    UNVERIFIED, not CONFLICT.  Only a *different* tone is a conflict.

    The reverse is not symmetric either: a claim of CSQ (explicit null)
    that measures a real tone IS a conflict, because detecting a tone that
    should not exist is positive evidence, not a failure to measure.
    """
    claimed = expect["ctcss_hz"]
    observed = meta.get("ctcss_hz")

    # The detector flags mains-hum harmonics (120/180/240 Hz) because they
    # sit almost exactly on standard tones 123.0/179.9/241.8.  Grading on
    # one of those would "verify" a power supply.
    if observed is not None and meta.get("ctcss_suspect_hum"):
        return SUSPECT, observed, "possible mains hum harmonic, not trusted"

    if claimed is None:                      # catalog claims CSQ output
        if observed is None:
            return VERIFIED, None, "no tone observed, catalog claims CSQ"
        return CONFLICT, observed, f"catalog claims CSQ, observed {observed} Hz"

    if observed is None:
        why = "no tone resolved this check"
        if meta.get("ctcss_error"):
            why = meta["ctcss_error"]
        elif meta.get("ctcss_samples") is not None:
            why = "too little audio to resolve tone spacing"
        return UNVERIFIED, None, why

    if abs(float(observed) - float(claimed)) <= CTCSS_MATCH_HZ:
        return VERIFIED, observed, None
    return CONFLICT, observed, f"catalog claims {claimed} Hz"


def _grade_dcs(expect, meta):
    """Grade DCS/DPL against the catalog's claim.

    Presence detection and code recovery are separate capabilities and are
    graded separately.  Knowing a subaudible *data* carrier is running is
    already enough to contradict a CTCSS claim on the same channel, which
    is the immediate use; recovering which of the 100-odd codes it is
    needs the 23-bit Golay decode, which detect_dcs does not do yet.
    So a channel whose catalog entry claims DPL 023 reads UNVERIFIED with
    "DCS present, code decode not implemented" -- honest, and still useful.
    """
    claimed = expect["dcs_code"]
    observed = meta.get("dcs_code")
    present = meta.get("dcs_present")

    if claimed is None:                      # catalog claims no DCS
        if present:
            return CONFLICT, observed, "catalog claims no DCS, DCS carrier present"
        return VERIFIED, None, "no DCS carrier observed"

    if observed is None:
        if present:
            return UNVERIFIED, None, "DCS present, code decode not implemented"
        return UNVERIFIED, None, "no DCS carrier resolved this check"

    if str(observed).zfill(3) == str(claimed).zfill(3):
        return VERIFIED, observed, None
    return CONFLICT, observed, f"catalog claims DPL {claimed}"


def _grade_color_code(expect, meta):
    """Grade DMR colour code.

    meta carries color_codes as a list because one dwell can see more than
    one -- adjacent-channel bleed, or two systems sharing a frequency.  A
    claimed code that appears anywhere in the observed set is verified;
    observing only other codes is a conflict.  Both are recorded so the
    disagreement is inspectable later.
    """
    claimed = expect["color_code"]
    observed = meta.get("color_codes") or []
    if isinstance(observed, (int, str)):
        observed = [observed]
    observed = [int(c) for c in observed if c is not None]

    if claimed is None:
        return (NO_CLAIM, observed or None, None)
    if not observed:
        return UNVERIFIED, None, "no DMR burst decoded this check"
    if int(claimed) in observed:
        return VERIFIED, observed, None
    return CONFLICT, observed, f"catalog claims CC{claimed}"


def _grade_mode(expect, meta):
    """Grade modulation family, coarsely.

    Deliberately coarse: the decoder tells us which family produced a
    result, not an emission designator.  "FM" vs "20K0F3E" bandwidth is a
    different measurement and is not attempted here.
    """
    claimed = (expect["mode"] or "").upper() or None
    if claimed is None:
        return NO_CLAIM, None, None
    dmr_seen = bool(meta.get("color_codes") or meta.get("sync_lines"))
    analog_seen = bool(meta.get("audio_bytes"))
    observed = "DMR" if dmr_seen else ("FM" if analog_seen else None)
    if observed is None:
        return UNVERIFIED, None, "no decode this check"
    if claimed in ("DMR",) and observed == "DMR":
        return VERIFIED, observed, None
    if claimed in ("FM", "NFM", "FMN") and observed == "FM":
        return VERIFIED, observed, None
    return CONFLICT, observed, f"catalog claims {claimed}"


GRADERS = {
    "ctcss_hz": _grade_ctcss,
    "dcs_code": _grade_dcs,
    "color_code": _grade_color_code,
    "mode": _grade_mode,
}


def verify_params(target, meta, heard):
    """Compare this check's measurements against the target's claims.

    Returns {param: {state, observed, note}}.  Only parameters the catalog
    actually claims are graded; `expect: {}` yields {} rather than a row
    of green ticks, because verifying nothing is not the same as verifying
    successfully.

    A silent check grades nothing at all.  Nothing was measured, so every
    claim keeps whatever state it earned on an earlier check -- a silent
    check must never downgrade a parameter that was previously verified.
    """
    if not heard:
        return {}
    out = {}
    for key, grader in GRADERS.items():
        if key not in target["expect"]:
            continue
        state, observed, note = grader(target["expect"], meta)
        out[key] = {"state": state, "observed": observed, "note": note}
    return out


def _heard(meta, gated):
    """Did we actually hear this channel on this check?

    Energy alone is not "heard": the gate fires on adjacent-channel splash
    and on the noise floor moving.  Require a decoder-level indication --
    unsquelched audio for analog, a decoded burst for DMR -- and fall back
    to the gate only when the decoder produced nothing at all to judge.
    """
    if meta.get("color_codes") or meta.get("talkgroups") or meta.get("radio_ids"):
        return True
    if meta.get("active"):
        return True
    if meta.get("audio_bytes"):
        return bool(meta.get("active"))
    return bool(gated) and meta.get("audio_bytes") is None


def check_target(index, target, store, receiver, fix, gain=None):
    """Look at one target once and record the result, heard or not.

    The silent path is the point of this function.  It costs one energy
    gate (~5 s) and writes a row saying "checked, nothing there", which is
    what turns "no observations" into "confirmed quiet".
    """
    t0 = time.time()
    gated, gsnr = dwell_mod.energy_gate(
        index, target["freq_hz"], squelch_db=target["squelch_db"], gain=gain)

    meta = {}
    if gated:
        # Only spend the full dwell when there is something to decode.
        # This is what keeps a ~50-target round robin affordable on one
        # dongle: quiet targets cost 5 s, active ones cost duration_s.
        fn = dwell_mod.DECODERS[target["decoder"]]
        meta = fn(index, target["freq_hz"], target["duration_s"],
                  gain=gain) or {}

    heard = _heard(meta, gated) if gated else False
    params = verify_params(target, meta, heard)
    meta = dict(meta)
    meta["monitor"] = True
    meta["gate_snr_db"] = round(gsnr, 1)
    meta["check_s"] = round(time.time() - t0, 1)

    store.add_monitor_check(
        receiver=receiver, target=target, heard=heard,
        snr_db=round(gsnr, 1), params=params, meta=meta, fix=fix)

    # An active target is real evidence and belongs in the observation
    # stream too, so discovery-side reporting and the existing submit
    # path keep working with no changes.
    if heard:
        store.add_observation(receiver, target["freq_hz"], round(gsnr, 1),
                              target["duration_s"], target["decoder"],
                              True, meta, fix)

    conflicts = [k for k, v in params.items() if v["state"] == CONFLICT]
    log.info("monitor %-18s %.4f MHz  %s%s", target["name"],
             target["freq_hz"] / 1e6,
             "HEARD" if heard else "silent",
             f"  CONFLICT: {', '.join(conflicts)}" if conflicts else "")
    return heard, params


def due_targets(store, receiver, targets, now=None):
    """Targets whose revisit interval has elapsed, most overdue first.

    Priority breaks ties by scaling how overdue a target is, so a
    priority-1 target effectively gets a shorter interval than a
    priority-3 one without either ever being starved: every target's
    overdue ratio keeps growing, so anything waiting long enough
    eventually outranks a frequently-checked high-priority channel.
    """
    now = now or time.time()
    last = store.last_checked(receiver)
    out = []
    for t in targets:
        seen = last.get((t["freq_hz"], t["name"]), 0)
        age = now - seen
        if age < t["interval_s"]:
            continue
        weight = age / t["interval_s"] * (4 - min(t["priority"], 3))
        out.append((weight, t))
    out.sort(key=lambda r: -r[0])
    return [t for _w, t in out]


def run_due(index, store, receiver, targets, fix_provider, gain=None,
            budget_s=120, now=None):
    """Check as many due targets as fit in the airtime budget.

    Returns (checked, heard, conflicts).  Stops cleanly when the budget is
    spent rather than mid-target, so the sweep gets the radio back on
    schedule even with a long target list.
    """
    if not targets:
        return 0, 0, 0
    deadline = (now or time.time()) + budget_s
    checked = heard_n = conflict_n = 0
    for t in due_targets(store, receiver, targets, now=now):
        # Worst case for one target is gate + full dwell; don't start one
        # we cannot finish inside the budget.
        if time.time() + 5 + t["duration_s"] > deadline:
            break
        try:
            heard, params = check_target(index, t, store, receiver,
                                         fix_provider(), gain=gain)
        except Exception as e:                # one bad target must not
            log.warning("monitor %s failed: %s", t["name"], e)
            continue
        checked += 1
        heard_n += 1 if heard else 0
        conflict_n += sum(1 for v in params.values()
                          if v["state"] == CONFLICT)
    return checked, heard_n, conflict_n
