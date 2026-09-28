"""Spectrum sweep via rtl_power; per-bin SNR against this receiver's own median."""
import subprocess
import tempfile
import os
import statistics


class SweepError(Exception):
    pass


# rtl_power cannot tune faster than this sample rate (USB bandwidth hard cap).
_RTL_MAX_RATE_HZ = 3_200_000


def _rtl_pass_windows(start_mhz, stop_mhz):
    """Number of re-tuning windows rtl_power needs to cover the span.

    rtl_power re-tunes in _RTL_MAX_RATE_HZ-wide windows and collects
    integration_s seconds at each one.  The total one-pass wall time is
    therefore n_windows * integration_s.  This must be strictly less than
    the -e exit timer, or rtl_power never reaches its own exit check and
    blocks until the Python timeout fires ~120 s later.
    """
    import math
    span_hz = (stop_mhz - start_mhz) * 1e6
    return max(1, math.ceil(span_hz / _RTL_MAX_RATE_HZ))


def run_rtl_power(index, start_mhz, stop_mhz, step_khz, integration_s, gain=None):
    """One rtl_power pass -> [(freq_hz, db, bin_hz)]. Bounded lifetime via -e.

    bin_hz is rtl_power's ACTUAL reported bin width for that row, not the
    requested step_khz -- rtl_power auto-selects its own FFT size and does
    not honor the requested step exactly.  Every caller must carry this
    through rather than assume the configured step; see snap_channel().

    -e is set to pass_s + 30 where pass_s = n_windows * integration_s.
    Using a flat integration_s + 30 was wrong for wide spans: a 12 MHz
    VHF band needs 4 tuning windows so one pass takes 40 s with -i 10,
    which equalled -e 40 exactly -- rtl_power never checked its timer
    between windows and blocked until the Python timeout fired.
    """
    n_windows = _rtl_pass_windows(start_mhz, stop_mhz)
    pass_s = n_windows * integration_s
    e_s = pass_s + 30          # must be strictly > pass_s
    hard_timeout = pass_s + 90  # Python-side hard kill, well after -e
    out = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    out.close()
    cmd = ["rtl_power", "-d", str(index),
           "-f", f"{start_mhz}M:{stop_mhz}M:{step_khz}k",
           "-i", str(integration_s), "-1",
           "-e", str(e_s)]
    if gain is not None:
        cmd += ["-g", str(gain)]
    cmd.append(out.name)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=hard_timeout)
    except subprocess.TimeoutExpired:
        raise SweepError("rtl_power hung past its own -e bound")
    finally:
        rows = _parse_csv(out.name)
        os.unlink(out.name)
    if not rows:
        raise SweepError(f"rtl_power produced no bins: {p.stderr[-400:]}")
    return rows


def _parse_csv(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                low = float(parts[2]); step = float(parts[4])
                dbs = [float(x) for x in parts[6:]]
            except ValueError:
                continue
            for i, db in enumerate(dbs):
                rows.append((int(low + i * step), db, step))
    return rows


def snap_channel(freq_hz, bin_hz, raster_hz=6250, tolerance_hz=None):
    """Best-effort snap of a measured frequency onto the channel raster.

    rtl_power auto-selects its own FFT bin width from internal constraints
    (FFT size vs. sample rate) -- it does NOT honor the configured
    step_khz.  Observed on meshpi: requesting 6.25 kHz steps across
    450-470 MHz actually produced 4882.8125 Hz bins (5 Msps / 1024-point
    FFT), which shares no common divisor with the 12.5/25 kHz LMR channel
    grid.  A raw bin center is therefore essentially never a real channel
    frequency -- it's somewhere within +/- half a bin of one.

    Snap only when that's unambiguous: the measurement must land within
    tolerance of a raster point.  Otherwise say so honestly rather than
    force a guess -- the caller keeps the raw frequency and flags it
    unsnapped.

    tolerance_hz overrides the default half-bin gate.  That default is
    right for a raw wideband sweep bin, where bin width dominates the
    error.  After a narrow refine_carrier() pass the bin is ~100 Hz and
    the limiting error is instead the receiver's own frequency accuracy
    (PPM), so the caller passes a tolerance reflecting that.

    Either way the tolerance is clamped below half the raster spacing:
    a looser gate would "snap" every frequency, including energy sitting
    genuinely between channels, which is how a coarse bin turns into a
    confidently wrong channel number.

    The hard gate comes first: if the bin is wider than the raster
    spacing, the +/- half-bin uncertainty covers more than one raster
    point and NO amount of closeness identifies a channel.  A coarse bin
    frequently lands near some raster point purely by chance, and often
    the wrong one -- a 3906 Hz sweep bin at 159.196875 MHz sits 625 Hz
    from 159.1975 while the real carrier is 159.1950.  Refuse outright
    rather than emit a confidently wrong channel; refine_carrier() exists
    to make the measurement good enough to pass this gate.

    Returns (channel_hz, snapped: bool).
    """
    if bin_hz >= raster_hz:
        return freq_hz, False
    snapped_hz = round(freq_hz / raster_hz) * raster_hz
    tol = bin_hz / 2 if tolerance_hz is None else tolerance_hz
    tol = min(tol, raster_hz * 0.4)
    if abs(freq_hz - snapped_hz) <= tol:
        return snapped_hz, True
    return freq_hz, False


def refine_carrier(index, freq_hz, integration_s=4, gain=None,
                   search_hz=5000, span_hz=48_000, dc_offset_hz=12_000):
    """Narrow high-resolution re-measure of a candidate's true center.

    The wideband sweep's bin width is coarser than the channel raster
    (measured on MuehlMini VHF: 3906.25 Hz bins against a 2.5 kHz raster),
    so a sweep bin center cannot identify a channel by itself.  Two
    adjacent bins straddling one real carrier snap to two different raster
    points -- that is exactly how 159.1950 MHz got recorded as both
    159.19375 and 159.196875.  Re-measure a narrow span so bin width falls
    far below the raster, and report where the energy actually peaks.

    The window is deliberately placed off-center.  rtl_power leaves a DC
    spike at each hop center; at this span that spike would sit right on
    the candidate and masquerade as the carrier.  Shifting by
    dc_offset_hz puts it clear of the search region, and a guard band
    drops it outright.

    Returns (carrier_hz, bin_hz, snr_db).  carrier_hz is None when nothing
    rises above the local noise floor.
    """
    center = freq_hz - dc_offset_hz
    lo = (center - span_hz / 2) / 1e6
    hi = (center + span_hz / 2) / 1e6
    rows = run_rtl_power(index, lo, hi, 0.1, integration_s, gain)
    if not rows:
        return None, None, 0.0
    med = statistics.median(d for _, d, _s in rows)
    guard = max(2000.0, rows[0][2] * 2)
    cand = [(f, d, s) for f, d, s in rows
            if abs(f - freq_hz) <= search_hz and abs(f - center) > guard]
    if not cand:
        return None, None, 0.0
    f_peak, d_peak, bin_hz = max(cand, key=lambda r: r[1])
    return f_peak, bin_hz, round(d_peak - med, 1)


def snr_hits(rows, medians, threshold_db):
    """Bins whose level exceeds their own historical median by threshold.

    medians: {freq_hz: median_db} for THIS receiver.  Bins without history
    fall back to the current pass's global median (first-pass bootstrap).
    rows are (freq_hz, db, bin_hz); bin_hz rides along so a hit carries its
    own actual bin width for channel_snap() -- never assume step_khz.
    """
    if not rows:
        return []
    global_med = statistics.median(d for _, d, _step in rows)
    hits = []
    for f, d, step in rows:
        base = medians.get(f, global_med)
        snr = d - base
        if snr >= threshold_db:
            hits.append((f, snr, d, step))
    return hits
