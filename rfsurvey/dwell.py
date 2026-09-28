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

# Active timeslot.  dsd-fme's status line shows BOTH slots every frame and
# marks the active one with brackets:  " [SLOT1]  slot2 " / "  slot1  [SLOT2] ".
# Matching bare "Slot %d" would tag every frame with both slots, and would also
# catch Cap+/Con+ trunking chatter ("Slot 1 Free; Slot 2 Busy"), so anchor on
# the bracketed form only.
SLOT_RE = re.compile(r"\[\s*SLOT\s*([12])\s*\]", re.I)

# Encryption.  dsd-fme prints ALG/KEY ID for encrypted traffic, but several
# CLEAR-traffic strings also contain the word "Encrypted"
# ("Slot 1 No Encrypted Call Trunking", "...Encryption Identifiers Disabled"),
# so those are stripped before the positive test.
ENC_NEG_RE = re.compile(r"No Encrypted|Encryption Identifiers Disabled", re.I)
ENC_RE = re.compile(r"\*?Encrypted\*?|ENC PDU|ENC LO", re.I)
ALG_RE = re.compile(r"ALG(?:\s*ID)?[:=\s]+(?:0x)?([0-9A-Fa-f]{1,4})", re.I)
KID_RE = re.compile(r"(?:KEY\s*ID|KID)[:=\s]+(?:0x)?([0-9A-Fa-f]{1,4})", re.I)

# EIA/TIA-603 CTCSS tones (Hz).
CTCSS_TONES = [67.0,69.3,71.9,74.4,77.0,79.7,82.5,85.4,88.5,91.5,94.8,97.4,
               100.0,103.5,107.2,110.9,114.8,118.8,123.0,127.3,131.8,136.5,
               141.3,146.2,151.4,156.7,159.8,162.2,165.5,167.9,171.3,173.8,
               177.3,179.9,183.5,186.2,189.9,192.8,196.6,199.5,203.5,206.5,
               210.7,218.1,225.7,229.1,233.6,241.8,250.3,254.1]


def detect_ctcss(pcm, fs=12500, decim=8):
    """Find a CTCSS subaudible tone in rtl_fm's raw s16le mono audio.

    Returns {} when numpy is missing or there is too little audio to resolve
    the ~0.5 Hz spacing down at the bottom of the tone list.

    Caveat recorded in the result: mains hum harmonics at 120/180/240 Hz sit
    almost exactly on standard tones 123.0 / 179.9 / 241.8, so a hit on one of
    those is flagged rather than trusted.
    """
    try:
        import numpy as np
    except ImportError:
        return {"ctcss_error": "numpy not installed"}
    a = np.frombuffer(pcm, dtype="<i2")
    if a.size < fs * 4:                       # <4 s of audio: not resolvable
        return {"ctcss_samples": int(a.size)}
    a = a.astype(np.float32)
    # mean-decimate 12500 -> 1562.5 Hz (Nyquist 781 Hz, well above 254 Hz)
    n = (a.size // decim) * decim
    d = a[:n].reshape(-1, decim).mean(axis=1)
    fsd = fs / decim
    # Welch-style average over 4 s blocks for a stable noise reference.
    blk = int(fsd * 4)
    blocks = [d[i:i + blk] for i in range(0, d.size - blk + 1, blk)]
    if not blocks:
        return {"ctcss_samples": int(a.size)}
    win = np.hanning(blk)
    acc = None
    for b in blocks:
        m = np.abs(np.fft.rfft((b - b.mean()) * win))
        acc = m if acc is None else acc + m
    spec = acc / len(blocks)
    freqs = np.fft.rfftfreq(blk, 1.0 / fsd)
    band = (freqs >= 60) & (freqs <= 260)
    if not band.any():
        return {}
    ref = float(np.median(spec[band]))
    if ref <= 0:
        return {}
    best, best_snr = None, 0.0
    for t in CTCSS_TONES:
        sel = np.abs(freqs - t) <= 0.8
        if not sel.any():
            continue
        snr = 20.0 * np.log10(float(spec[sel].max()) / ref)
        if snr > best_snr:
            best, best_snr = t, snr
    if best is None or best_snr < 8.0:        # 8 dB over in-band median
        return {"ctcss_hz": None, "ctcss_blocks": len(blocks)}
    return {"ctcss_hz": best,
            "ctcss_snr_db": round(best_snr, 1),
            "ctcss_blocks": len(blocks),
            "ctcss_suspect_hum": any(abs(best - h) < 1.5
                                     for h in (120.0, 180.0, 240.0))}


# DCS / DPL: a 23-bit Golay word sent continuously at 134.4 bps, so the
# whole word repeats every 23 / 134.4 s = 171.1 ms (5.843 Hz word rate).
# That repetition period is the detection handle used below.
DCS_BAUD = 134.4
DCS_WORD_BITS = 23
DCS_WORD_PERIOD_S = DCS_WORD_BITS / DCS_BAUD      # 0.17113 s


def detect_dcs(pcm, fs=12500, decim=8):
    """Detect a DCS/DPL subaudible data carrier in rtl_fm s16le mono audio.

    SCOPE -- read before trusting this.  This detects that a DCS carrier is
    *present*; it does NOT recover which of the ~104 codes it is.  Code
    recovery needs the 23,12 Golay decode of the recovered bitstream, which
    is deliberately not attempted here, so `dcs_code` is always None and
    monitor._grade_dcs reports "DCS present, code decode not implemented"
    rather than inventing a code.

    Presence alone is still decisive for the case that motivated this: a
    channel whose catalog entry claims a CTCSS tone but which is actually
    running DCS.  A DCS carrier contradicts a CTCSS claim regardless of
    which code it carries, so the conflict is detectable now and the exact
    code becomes a follow-up measurement.

    Method: DCS is a squarewave-ish data stream, so unlike CTCSS it has no
    single dominant narrow tone -- its energy is spread across the
    subaudible band but is strongly periodic at the 171 ms word period.
    So we band-limit to the subaudible region and look for autocorrelation
    at that lag.  CTCSS fails this test (it is periodic at its own ~6 ms
    tone period, not at 171 ms) and noise fails it too, which is what makes
    the two distinguishable from the same recording.
    """
    try:
        import numpy as np
    except ImportError:
        return {"dcs_error": "numpy not installed"}
    a = np.frombuffer(pcm, dtype="<i2")
    # Need several word periods to establish periodicity; 4 s gives ~23.
    if a.size < fs * 4:
        return {"dcs_samples": int(a.size)}
    a = a.astype(np.float32)
    n = (a.size // decim) * decim
    d = a[:n].reshape(-1, decim).mean(axis=1)
    fsd = fs / decim                            # 1562.5 Hz
    d = d - d.mean()

    # Keep the subaudible band only.  Voice energy above ~300 Hz is orders
    # of magnitude stronger and would dominate the autocorrelation.
    spec = np.fft.rfft(d)
    freqs = np.fft.rfftfreq(d.size, 1.0 / fsd)
    spec[(freqs < 20) | (freqs > 300)] = 0
    low = np.fft.irfft(spec, n=d.size)
    rms = float(np.sqrt(np.mean(low ** 2)))
    if rms <= 0:
        return {"dcs_present": False, "dcs_reason": "no subaudible energy"}

    # Normalized autocorrelation at the word-period lag.
    low = low / rms
    lag = int(round(DCS_WORD_PERIOD_S * fsd))    # ~267 samples
    if low.size < lag * 4:
        return {"dcs_samples": int(a.size)}
    ac = np.correlate(low, low, mode="full")[low.size - 1:]
    ac = ac / ac[0]
    # Allow +/-3% for baud tolerance and decimation rounding.
    tol = max(2, int(lag * 0.03))
    win = ac[lag - tol:lag + tol + 1]
    if win.size == 0:
        return {"dcs_present": False, "dcs_reason": "lag out of range"}
    peak = float(win.max())
    # Reference: typical correlation away from any harmonic of the word
    # period, so a genuinely periodic signal stands out from a merely
    # coloured one.
    ref_zone = ac[int(lag * 1.35):int(lag * 1.85)]
    ref = float(np.percentile(np.abs(ref_zone), 90)) if ref_zone.size else 0.0

    tone = detect_ctcss(pcm, fs=fs, decim=decim)
    ctcss_hz = tone.get("ctcss_hz")
    # A strong clean CTCSS tone explains subaudible energy on its own; do
    # not also report DCS, or every tone-squelched repeater reads as both.
    if ctcss_hz is not None and (tone.get("ctcss_snr_db") or 0) >= 12.0:
        return {"dcs_present": False,
                "dcs_reason": f"dominant CTCSS tone {ctcss_hz} Hz"}

    present = peak >= 0.35 and peak >= ref * 2.0
    out = {"dcs_present": bool(present),
           "dcs_word_corr": round(peak, 3),
           "dcs_corr_ref": round(ref, 3),
           "dcs_word_period_ms": round(lag / fsd * 1000.0, 1)}
    if present:
        # Explicitly None, not absent: the grader distinguishes "carrier
        # seen but code unknown" from "nothing seen".
        out["dcs_code"] = None
        out["dcs_note"] = "presence only; 23,12 Golay code decode not implemented"
    return out


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
    meta = {"decoder": "nfm", "audio_bytes": audio_bytes,
            "active": audio_bytes > 48_000}  # >2s of unsquelched audio
    if audio_bytes:
        meta.update(detect_ctcss(out))
        # Run DCS detection too, not just when CTCSS came up empty: a
        # channel can read as a weak false tone while actually carrying
        # DCS, and detect_dcs does its own CTCSS-dominance check to decide
        # which explanation wins.
        meta.update(detect_dcs(out))
    return meta


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
    slots = sorted({int(x) for x in SLOT_RE.findall(log)})
    clean = ENC_NEG_RE.sub("", log)
    algs = sorted({int(x, 16) for x in ALG_RE.findall(clean)})
    kids = sorted({int(x, 16) for x in KID_RE.findall(clean)})
    # DMR ALG ID 0x00 is "clear"; a non-zero ALG or an explicit Encrypted
    # marker is the real signal.
    encrypted = bool(ENC_RE.search(clean)) or any(a != 0 for a in algs)
    return {"decoder": "dmr", "color_codes": ccs, "talkgroups": tgs,
            "radio_ids": rids, "sync_lines": syncs,
            "timeslots": slots, "encrypted": encrypted,
            "alg_ids": algs, "key_ids": kids,
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
