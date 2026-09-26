"""Spectrum sweep via rtl_power; per-bin SNR against this receiver's own median."""
import subprocess
import tempfile
import os
import statistics


class SweepError(Exception):
    pass


def run_rtl_power(index, start_mhz, stop_mhz, step_khz, integration_s, gain=None):
    """One rtl_power pass -> [(freq_hz, db)]. Bounded lifetime via -e."""
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
                rows.append((int(low + i * step), db))
    return rows


def snr_hits(rows, medians, threshold_db):
    """Bins whose level exceeds their own historical median by threshold.

    medians: {freq_hz: median_db} for THIS receiver.  Bins without history
    fall back to the current pass's global median (first-pass bootstrap).
    """
    if not rows:
        return []
    global_med = statistics.median(d for _, d in rows)
    hits = []
    for f, d in rows:
        base = medians.get(f, global_med)
        snr = d - base
        if snr >= threshold_db:
            hits.append((f, snr, d))
    return hits
