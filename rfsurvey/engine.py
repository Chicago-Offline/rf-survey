"""Survey engine: sweep -> hit tracking -> gated dwell -> store. One engine per device."""
import collections
import logging
import time

from . import devices, sweep, dwell as dwell_mod
from .store import Store
from .location import make_location

log = logging.getLogger("rfsurvey")


def run(cfg, plan, serial, force=False, max_passes=None):
    store = Store(cfg["db"])
    loc = make_location(cfg)
    devcfg = cfg.get("devices", {}).get(serial, {})
    gain = plan["sweep"].get("gain", devcfg.get("gain"))
    passes = max_passes if max_passes is not None else plan["sweep"]["passes"]
    hits_count = collections.Counter()
    dwelled = {}
    medians = {}

    with devices.Claim(serial, force=force) as claim:
        idx = claim.index
        log.info("claimed %s at index %d", serial, idx)
        n = 0
        while True:
            n += 1
            for band in plan["bands"]:
                fix = loc.get()
                try:
                    rows = sweep.run_rtl_power(
                        idx, band["start_mhz"], band["stop_mhz"],
                        band["step_khz"], plan["sweep"]["integration_s"], gain)
                except sweep.SweepError as e:
                    log.warning("sweep failed: %s", e)
                    if not devices.is_claimable(idx):
                        devices.release(idx)
                    continue
                store.add_sweep(serial, plan["name"],
                                int(band["start_mhz"] * 1e6),
                                int(band["stop_mhz"] * 1e6),
                                int(band["step_khz"] * 1e3), fix, rows)
                for f, d, _step in rows:
                    medians.setdefault(f, []).append(d)
                med_now = {f: sorted(v)[len(v) // 2] for f, v in medians.items()}
                hits = sweep.snr_hits(rows, med_now, plan["dwell"]["snr_db"])
                log.info("pass %d %s-%s MHz: %d bins, %d hits",
                         n, band["start_mhz"], band["stop_mhz"], len(rows), len(hits))
                raster_hz = plan["sweep"].get("channel_raster_hz", 6250)
                for f, snr, _db, bin_hz in sorted(hits, key=lambda h: -h[1]):
                    hits_count[f] += 1
                    if hits_count[f] < plan["dwell"]["min_hits"]:
                        continue
                    if time.time() - dwelled.get(f, 0) < 300:
                        continue  # don't re-dwell the same channel within 5 min
                    dwelled[f] = time.time()
                    log.info("dwell %.4f MHz (snr %.1f dB, %d hits)",
                             f / 1e6, snr, hits_count[f])
                    # Dwell/decode at the raw measured bin -- that's where the
                    # energy actually was.  Snapping is purely a reporting
                    # decision, applied only to what we record as freq_hz.
                    gated, gsnr, meta = dwell_mod.dwell(
                        idx, f, plan["dwell"], gain)
                    channel_hz, snapped = sweep.snap_channel(f, bin_hz, raster_hz)
                    meta = dict(meta or {})
                    meta["bin_freq_hz"] = f
                    meta["channel_snap"] = snapped
                    store.add_observation(serial, channel_hz,
                                          round(max(snr, gsnr), 1),
                                          plan["dwell"]["duration_s"],
                                          plan["dwell"]["decoder"],
                                          gated, meta, loc.get())
            if passes and n >= passes:
                log.info("completed %d passes, exiting cleanly", n)
                return
