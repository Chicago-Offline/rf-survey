"""Spectrum sweep via rtl_power; per-bin SNR against this receiver's own median."""
import subprocess
import tempfile
import os
import statistics


class SweepError(Exception):
    pass


def run_rtl_power(index, start_mhz, stop_mhz, step_khz, integration_s, gain=None):
    """One rtl_power pass -> [(freq_hz, db, bin_hz)]. Bounded lifetime via -e.

    bin_hz is rtl_power's ACTUAL reported bin width for that row, not the
    requested step_khz -- rtl_power auto-selects its own FFT size and does
    not honor the requested step exactly.  Every caller must carry this
    through rather than assume the configured step; see snap_channel().
    """
    out = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
    out.close()
    cmd = ["rtl_power", "-d", str(index),
           "-f", f"{start_mhz}M:{stop_mhz}M:{step_khz}k",
           "-i", str(integration_s), "-1",
           "-e", str(integration_s + 30)]
    if gain is not None:
        cmd += ["-g", str(gain)]
    cmd.append(out.name)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=integration_s + 120)
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


def snap_channel(freq_hz, bin_hz, raster_hz=6250):
    """Best-effort snap of a raw FFT bin center onto the channel raster.

    rtl_power auto-selects its own FFT bin width from internal constraints
    (FFT size vs. sample rate) -- it does NOT honor the configured
    step_khz.  Observed on meshpi: requesting 6.25 kHz steps across
    450-470 MHz actually produced 4882.8125 Hz bins (5 Msps / 1024-point
    FFT), which shares no common divisor with the 12.5/25 kHz LMR channel
    grid.  A raw bin center is therefore essentially never a real channel
    frequency -- it's somewhere within +/- half a bin of one.

    Snap only when that's unambiguous: the bin center must land within
    half the ACTUAL (reported) bin width of a raster point.  Otherwise
    say so honestly rather than force a guess -- the caller keeps the raw
    bin frequency and flags it unsnapped.

    Returns (channel_hz, snapped: bool).
    """
    snapped_hz = round(freq_hz / raster_hz) * raster_hz
    if abs(freq_hz - snapped_hz) <= bin_hz / 2:
        return snapped_hz, True
    return freq_hz, False


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
