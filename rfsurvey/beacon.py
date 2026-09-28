"""Beacon calibration pass (NETWORK.md S7).

Measures known-constant emitters to bound an observer's HARDWARE variance
over time -- dead dongle, wet feedline, detached or moved antenna, drifting
gain.  Never propagation, and never evidence about a surveyed channel.

Two hard rules enforced here:

1. Gain must be pinned.  An absolute dB level taken under AGC is not
   comparable to yesterday's, so an unpinned observer is measured but its
   readings are marked `unpinned` and must not feed a baseline.
2. Baselines are long-term medians (days).  A single pass is a sample, not
   a verdict; nothing in this module flags drift off one reading.
"""
import logging
import statistics
import time

from . import devices, references
from .sweep import SweepError, run_rtl_power

log = logging.getLogger(__name__)

# Status values stored per reading.
OK = "ok"
NOT_HEARD = "not_heard"
ERROR = "error"

# Minimum SNR over the local noise floor to call a reference "heard".
HEARD_SNR_DB = 6.0


def _window(ref):
    """(span_hz, step_hz) around a reference: wide enough for noise shoulders."""
    bw = float(ref.get("bandwidth_hz") or 16000)
    span = max(bw * 12.0, 200_000.0)
    step = max(bw / 8.0, 1000.0)
    return span, step


def measure(index, ref, gain=None, integration_s=8):
    """One reference -> dict of signal/noise/snr, or a not_heard/error row.

    signal = strongest bin within the reference's own bandwidth.
    noise   = median of the shoulder bins outside 2x bandwidth.
    Both absolute dB are kept: SNR survives a gain change, the absolute
    level is what catches a front end slowly going deaf.
    """
    center = float(ref["freq_hz"])
    span, step = _window(ref)
    start_mhz = (center - span / 2.0) / 1e6
    stop_mhz = (center + span / 2.0) / 1e6
    t0 = time.time()
    try:
        rows = run_rtl_power(index, round(start_mhz, 6), round(stop_mhz, 6),
                             step / 1000.0, integration_s, gain=gain)
    except SweepError as e:
        log.warning("beacon %s: sweep failed: %s", ref["id"], e)
        return {"status": ERROR, "error": str(e), "dur_s": time.time() - t0}

    bw = float(ref.get("bandwidth_hz") or 16000)
    inband = [d for f, d in rows if abs(f - center) <= bw / 2.0]
    shoulder = [d for f, d in rows if abs(f - center) > bw * 2.0]
    if not inband or len(shoulder) < 8:
        return {"status": ERROR,
                "error": f"insufficient bins (in={len(inband)} out={len(shoulder)})",
                "dur_s": time.time() - t0}

    signal_db = max(inband)
    noise_db = statistics.median(shoulder)
    snr_db = signal_db - noise_db
    return {
        "status": OK if snr_db >= HEARD_SNR_DB else NOT_HEARD,
        "signal_db": round(signal_db, 2),
        "noise_db": round(noise_db, 2),
        "snr_db": round(snr_db, 2),
        "bins": len(rows),
        "dur_s": round(time.time() - t0, 1),
    }


def check(cfg, store, serial, refs, integration_s=8, force=False,
          include_uncovered=False):
    """Run a full beacon-check pass for one observer.

    Returns [(ref, verdict, reading)] and persists every reading.
    """
    dev_cfg = (cfg.get("devices") or {}).get(serial) or {}
    gain = dev_cfg.get("gain")
    if gain is None:
        log.warning(
            "observer %s has no pinned gain — readings will be recorded but "
            "marked unpinned and excluded from baselines (AGC makes absolute "
            "dB incomparable between passes)", serial)

    plan = references.plan_for(refs, dev_cfg,
                               include_uncovered=include_uncovered)
    skipped = [r for r in refs
               if references.coverage(r, dev_cfg) == references.NO_REFERENCE]
    for r in skipped:
        # Recorded, not measured: a no_reference band must stay visibly
        # uncalibrated rather than silently absent.
        store.add_beacon_reading(
            receiver=serial, ref_id=r["id"], freq_hz=int(r["freq_hz"]),
            band=r.get("band"), coverage=references.NO_REFERENCE,
            signal_db=None, noise_db=None, snr_db=None, gain=gain,
            status=references.NO_REFERENCE, pinned=gain is not None)

    results = []
    if not plan:
        return results
    with devices.Claim(serial, force=force) as claim:
        for ref, verdict in plan:
            reading = measure(claim.index, ref, gain=gain,
                              integration_s=integration_s)
            store.add_beacon_reading(
                receiver=serial, ref_id=ref["id"], freq_hz=int(ref["freq_hz"]),
                band=ref.get("band"), coverage=verdict,
                signal_db=reading.get("signal_db"),
                noise_db=reading.get("noise_db"),
                snr_db=reading.get("snr_db"),
                gain=gain, status=reading["status"],
                pinned=gain is not None)
            results.append((ref, verdict, reading))
            log.info("beacon %-14s %s %s", ref["id"], verdict,
                     reading.get("snr_db", reading.get("error", "")))
    return results


def drift(store, serial, ref_id, days=7, min_samples=5):
    """Deviation of the latest reading from this observer's own median.

    Returns None when there is not enough history -- the correct answer
    early on, and better than a confident number built from two samples.
    """
    hist = store.beacon_history(serial, ref_id, days=days, pinned_only=True)
    usable = [h for h in hist if h["status"] == OK and h["snr_db"] is not None]
    if len(usable) < min_samples:
        return None
    latest = usable[0]
    baseline = statistics.median(h["snr_db"] for h in usable[1:]) \
        if len(usable) > 1 else None
    if baseline is None:
        return None
    return {
        "latest_snr_db": latest["snr_db"],
        "baseline_snr_db": round(baseline, 2),
        "delta_db": round(latest["snr_db"] - baseline, 2),
        "samples": len(usable),
        "days": days,
    }
