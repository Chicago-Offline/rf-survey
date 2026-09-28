"""Survey engine: sweep -> hit tracking -> gated dwell -> store. One engine per device."""
import collections
import logging
import time

from . import devices, sweep, dwell as dwell_mod
from .store import Store
from .location import make_location

log = logging.getLogger("rfsurvey")

DEFAULT_RASTER_HZ = 6250   # UHF/800 narrowband; VHF LMR and 2m need 2500
DEFAULT_PPM = 5.0


def band_raster_hz(band, plan):
    """Channel raster for this band: band override, then plan, then default.

    One raster per plan is wrong once a plan spans services.  6.25 kHz is
    a UHF/800 grid; VHF land mobile, railroad AAR and the 2m band all sit
    on multiples of 2.5 kHz and are simply not representable on it --
    154.9950, 159.1950, 159.6600 and 146.8800 are each a whole number of
    2.5 kHz steps and none is a whole number of 6.25 kHz steps.
    """
    if "channel_raster_hz" in band:
        return band["channel_raster_hz"]
    return plan["sweep"].get("channel_raster_hz", DEFAULT_RASTER_HZ)


def band_dwell(band, plan):
    """Dwell settings for this band: band overrides merged over the plan's.

    One dwell block per plan is wrong for the same reason one raster is
    (band_raster_hz above): a plan that spans services spans decoders too.
    450-470 business LMR wants the dmr decoder, while 851-869 is trunked
    and must stay on the energy-only nfm path until trunking decode exists
    -- NETWORK.md S4 is explicit that the route there is integrating
    trunk-recorder/sdrtrunk as a side decoder, not reimplementing one here.

    Merged rather than replaced, so a band can set just "decoder:" without
    restating snr_db / min_hits / duration_s / squelch_db.
    """
    merged = dict(plan["dwell"])
    merged.update(band.get("dwell") or {})
    return merged


def ppm_tolerance_hz(devcfg, band):
    """Receiver frequency error in Hz across this band, from configured PPM.

    Once a carrier has been refined the snap tolerance is set by receiver
    accuracy, not FFT bin width.  An uncalibrated RTL-SDR runs tens of
    ppm, and at 150 MHz even 5 ppm is 780 Hz -- a third of a 2.5 kHz
    raster step.  Set ppm_error per device in config.yml once known;
    meta.residual_hz on every observation is the data to calibrate from.
    """
    ppm = devcfg.get("ppm_error", devcfg.get("ppm", DEFAULT_PPM))
    mid_hz = (band["start_mhz"] + band["stop_mhz"]) / 2 * 1e6
    return abs(float(ppm)) * mid_hz / 1e6


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
                bdwell = band_dwell(band, plan)
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
                hits = sweep.snr_hits(rows, med_now, bdwell["snr_db"])
                log.info("pass %d %s-%s MHz: %d bins, %d hits",
                         n, band["start_mhz"], band["stop_mhz"], len(rows), len(hits))
                raster_hz = band_raster_hz(band, plan)
                ppm_hz = ppm_tolerance_hz(devcfg, band)
                for f, snr, _db, bin_hz in sorted(hits, key=lambda h: -h[1]):
                    hits_count[f] += 1
                    if hits_count[f] < bdwell["min_hits"]:
                        continue
                    if time.time() - dwelled.get(f, 0) < 300:
                        continue  # don't re-dwell the same channel within 5 min
                    dwelled[f] = time.time()
                    log.info("dwell %.4f MHz (snr %.1f dB, %d hits)",
                             f / 1e6, snr, hits_count[f])
                    # The sweep bin is up to half a coarse bin off the real
                    # carrier: too coarse to name a channel, and enough to
                    # detune the decoder.  Re-measure narrow and fine first,
                    # then both dwell and report at the refined carrier.
                    tune_hz, meas_bin_hz, refined = f, bin_hz, False
                    try:
                        c_hz, c_bin, c_snr = sweep.refine_carrier(
                            idx, f, gain=gain)
                        if c_hz is not None:
                            tune_hz, meas_bin_hz, refined = c_hz, c_bin, True
                            log.info("  refined -> %.4f MHz (%+d Hz, snr %.1f dB)",
                                     c_hz / 1e6, c_hz - f, c_snr)
                        else:
                            log.info("  refine found no peak, using sweep bin")
                    except sweep.SweepError as e:
                        log.warning("  refine failed, using sweep bin: %s", e)

                    gated, gsnr, meta = dwell_mod.dwell(
                        idx, tune_hz, bdwell, gain)
                    tol = max(meas_bin_hz / 2, ppm_hz) if refined else None
                    channel_hz, snapped = sweep.snap_channel(
                        tune_hz, meas_bin_hz, raster_hz, tol)
                    meta = dict(meta or {})
                    meta["bin_freq_hz"] = f
                    meta["carrier_hz"] = tune_hz if refined else None
                    meta["refined"] = refined
                    meta["raster_hz"] = raster_hz
                    # Signed distance to the nearest raster point, recorded
                    # whether or not we snapped: a consistent bias here is
                    # the receiver PPM error, ready to calibrate out.
                    meta["residual_hz"] = int(
                        tune_hz - round(tune_hz / raster_hz) * raster_hz)
                    meta["channel_snap"] = snapped
                    store.add_observation(serial, channel_hz,
                                          round(max(snr, gsnr), 1),
                                          bdwell["duration_s"],
                                          bdwell["decoder"],
                                          gated, meta, loc.get())
            if passes and n >= passes:
                log.info("completed %d passes, exiting cleanly", n)
                return
