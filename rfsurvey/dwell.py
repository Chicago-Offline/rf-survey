"""Dwell on a hit frequency: energy-gate, then decode with bounded lifetime.

Rule: dsd-family decoders fabricate sync on pure noise.  A decode only
counts if the energy gate agreed the channel was active (gated=1).
"""
import re
import subprocess

# dsd-fme log harvest patterns (tolerant of format drift)
CC_RE = re.compile(r"Color Code[=:\s]+(\d+)", re.I)
TG_RE = re.compile(r"(?:TGT|TG|Talkgroup)[=:\s]+(\d+)", re.I)
RID_RE = re.compile(r"(?:SRC|RID|Radio ID)[=:\s]+(\d+)", re.I)
SYNC_RE = re.compile(r"Sync:\s*([+-]?\w[\w\s]*?)(?:\s{2,}|$)", re.I)


def energy_gate(index, freq_hz, seconds=5, squelch_db=6.0, gain=None):
    """Quick rtl_power spot-check: is there energy on/near this frequency now?

    Compares the target bin against the median of a 200 kHz window around it.
    """
    from .sweep import run_rtl_power
    import statistics
    lo = (freq_hz - 100_000) / 1e6
    hi = (freq_hz + 100_000) / 1e6
    rows = run_rtl_power(index, lo, hi, 5, seconds, gain)
    if not rows:
        return False, 0.0
    med = statistics.median(d for _, d, _step in rows)
    near = [d for f, d, _step in rows if abs(f - freq_hz) <= 6250]
    if not near:
        return False, 0.0
    snr = max(near) - med
    return snr >= squelch_db, snr


def dwell_nfm(index, freq_hz, duration_s, gain=None):
    """Analog NFM dwell: record that audio was present (level via rtl_fm stderr)."""
    cmd = ["timeout", str(duration_s + 15), "rtl_fm", "-d", str(index),
           "-f", str(freq_hz), "-M", "fm", "-s", "12500", "-l", "30"]
    if gain is not None:
        cmd += ["-g", str(gain)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = p.communicate(timeout=duration_s + 20)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
    audio_bytes = len(out or b"")
    return {"decoder": "nfm", "audio_bytes": audio_bytes,
            "active": audio_bytes > 48_000}  # >2s of unsquelched audio


def dwell_dmr(index, freq_hz, duration_s, gain=None):
    """DMR dwell via dsd-fme; harvest CC / TGs / radio IDs from its log."""
    g = str(gain if gain is not None else 0)
    # No -N here. The NCurses UI needs a TTY; under systemd there is
    # none (Environment= is empty, so not even TERM), and dsd-fme dies
    # with "Error opening terminal: unknown" BEFORE emitting any decode
    # log -- silently harvesting zero CC/TG/RID. Plain stdout/stderr is
    # exactly what the regexes below want anyway.
    # Bandwidth field is 12 (kHz) explicitly: 2 is not a valid dsd-fme
    # bandwidth and was being silently coerced to 12, so this is a no-op
    # in behaviour that stops relying on that coercion.
    cmd = ["timeout", str(duration_s + 15), "dsd-fme",
           "-i", f"rtl:{index}:{freq_hz}:{g}:0:12:0",
           "-fs", "-o", "null"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=duration_s + 30, errors="replace")
        log = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        log = ""
    except FileNotFoundError:
        return {"decoder": "dmr", "error": "dsd-fme not installed"}
    ccs = sorted({int(x) for x in CC_RE.findall(log)})
    tgs = sorted({int(x) for x in TG_RE.findall(log)})
    rids = sorted({int(x) for x in RID_RE.findall(log)})
    syncs = len(SYNC_RE.findall(log))
    return {"decoder": "dmr", "color_codes": ccs, "talkgroups": tgs,
            "radio_ids": rids, "sync_lines": syncs,
            "active": bool(ccs or tgs or rids)}


DECODERS = {"nfm": dwell_nfm, "dmr": dwell_dmr}


def dwell(index, freq_hz, plan_dwell, gain=None):
    """Gate, then decode. Returns (gated, snr, meta)."""
    gated, snr = energy_gate(index, freq_hz,
                             squelch_db=plan_dwell["squelch_db"], gain=gain)
    fn = DECODERS.get(plan_dwell["decoder"])
    if fn is None:
        raise ValueError(f"unknown decoder {plan_dwell['decoder']!r}")
    meta = fn(index, freq_hz, plan_dwell["duration_s"], gain)
    meta["gate_snr_db"] = round(snr, 1)
    return gated, snr, meta
