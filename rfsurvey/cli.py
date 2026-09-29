"""survey CLI."""
import argparse
import json
import logging
import sys
import time

import yaml

from . import devices, config, report as report_mod
from .store import Store


def cmd_devices(args, cfg):
    devs = devices.list_devices()
    if not devs:
        print("no RTL-SDR devices found")
        return 1
    roles = cfg.get("devices", {})
    for d in devs:
        extra = roles.get(d["serial"], {})
        busy = "" if devices.is_claimable(d["index"]) else "  [BUSY]"
        role = extra.get("role", "")
        print(f"{d['index']}: SN={d['serial']:<10} {d['vendor']} {d['product']}"
              f"  {role}{busy}")
    return 0


def cmd_run(args, cfg):
    from . import engine
    plan = config.load_plan(args.plan)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        engine.run(cfg, plan, args.serial, force=args.force,
                   max_passes=args.passes)
    except devices.DeviceError as e:
        print(f"device error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_release(args, cfg):
    idx = devices.resolve(args.serial)
    ok = devices.release(idx)
    print(f"device {args.serial} (index {idx}): "
          f"{'released' if ok else 'STILL HELD — check holders manually'}")
    return 0 if ok else 1


def cmd_report(args, cfg):
    store = Store(cfg["db"])
    print(report_mod.summary(store, args.receiver, args.min_snr))
    return 0


def cmd_candidates(args, cfg):
    store = Store(cfg["db"])
    cands = report_mod.candidates(store, min_gated=args.min_gated)
    if not cands:
        print("# no candidates meet the evidence bar yet", file=sys.stderr)
        return 1
    print(yaml.safe_dump(cands, sort_keys=False, default_flow_style=False))
    print("# Review before adding to ssrf-lite — one tool run is not a system.",
          file=sys.stderr)
    return 0


def cmd_station_init(args, cfg):
    from . import station as station_mod
    st = station_mod.Station.create(args.id, args.key or station_mod.DEFAULT_KEY)
    print(f"station_id: {st.station_id}")
    print(f"public key (register with the aggregator): {st.public_key_b64}")
    print("add to config.yml:\n"
          f"station:\n  id: {st.station_id}\n"
          f"  key: {args.key or station_mod.DEFAULT_KEY}\n"
          "  mqtt:\n    server: wsmqtt-dev.chicagooffline.com\n"
          "    token_file: ~/.config/rf-survey/mqtt.token")
    return 0


def cmd_submit(args, cfg):
    import time as _time
    from . import station as station_mod, submit as submit_mod
    from .location import make_location as loc_provider
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    st = station_mod.Station.load(cfg)
    store = Store(cfg["db"])
    site = cfg.get("location", {})
    while True:
        submit_mod.build_batch(
            store, st,
            site={k: site.get(k) for k in ("lat", "lon", "alt_m", "mode")},
            receivers=submit_mod.observer_descriptors(cfg))
        with submit_mod.Publisher(cfg, st) as pub:
            n = pub.publish_pending(store)
            pub.heartbeat({"pending": len(store.pending_batches())})
        print(f"submitted {n} batch(es)")
        if not args.loop:
            return 0
        _time.sleep(args.loop)


def cmd_beacon_check(args, cfg):
    """Beacon calibration pass for one observer (NETWORK.md S7)."""
    from . import beacon as beacon_mod, references as ref_mod
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    refs = ref_mod.load_references(args.references, region=args.region)
    store = Store(cfg["db"])
    dev_cfg = (cfg.get("devices") or {}).get(args.serial) or {}

    if ref_mod.antenna_bands(dev_cfg) is None:
        print(f"# {args.serial}: no antenna.bands_mhz declared — readings are "
              "recorded as 'unverified' and cannot feed a score.\n"
              "# Declare the antenna in config.yml to enable scoring.",
              file=sys.stderr)

    results = beacon_mod.check(cfg, store, args.serial, refs,
                               integration_s=args.integration,
                               force=args.force,
                               include_uncovered=not args.skip_uncovered)

    rows = []
    for ref, verdict, reading in results:
        d = beacon_mod.drift(store, args.serial, ref["id"],
                             days=args.baseline_days)
        rows.append({
            "ref": ref["id"], "freq_hz": ref["freq_hz"], "band": ref["band"],
            "coverage": verdict, "status": reading["status"],
            "signal_db": reading.get("signal_db"),
            "noise_db": reading.get("noise_db"),
            "snr_db": reading.get("snr_db"),
            "drift": d, "error": reading.get("error"),
        })

    # Skipped uncovered bands are reported, never silently omitted. In the
    # default measure-everything mode nothing is skipped, so this is empty
    # and the measured rows above carry the no_reference verdict instead.
    measured_ids = {r["ref"] for r in rows}
    uncovered = [r for r in refs
                 if ref_mod.coverage(r, dev_cfg) == ref_mod.NO_REFERENCE
                 and r["id"] not in measured_ids]

    if args.json:
        print(json.dumps({"receiver": args.serial, "readings": rows,
                          "no_reference": [r["id"] for r in uncovered]},
                         indent=2))
        return 0

    if not rows and not uncovered:
        print("no references to measure", file=sys.stderr)
        return 1
    print(f"{'REFERENCE':<16}{'MHZ':>11} {'BAND':<13}{'STATUS':<13}"
          f"{'SNR':>7}{'SIG':>8}{'DRIFT':>9}")
    for r in rows:
        snr = f"{r['snr_db']:.1f}" if r["snr_db"] is not None else "-"
        sig = f"{r['signal_db']:.1f}" if r["signal_db"] is not None else "-"
        if r["drift"]:
            dr = f"{r['drift']['delta_db']:+.1f}"
        else:
            dr = "n/a"
        status = r["status"]
        if r["coverage"] == ref_mod.UNVERIFIED and status == beacon_mod.OK:
            status = "ok(unverif)"
        elif r["coverage"] == ref_mod.NO_REFERENCE \
                and status == beacon_mod.OK:
            # Heard on an antenna not rated for the band: real reception,
            # unscoreable reading.
            status = "ok(no_ref)"
        print(f"{r['ref']:<16}{r['freq_hz']/1e6:>11.4f} {r['band']:<13}"
              f"{status:<13}{snr:>7}{sig:>8}{dr:>9}")
        if r["error"]:
            print(f"    error: {r['error']}", file=sys.stderr)
    for r in uncovered:
        print(f"{r['id']:<16}{r['freq_hz']/1e6:>11.4f} {r['band']:<13}"
              f"{'no_reference':<13}{'-':>7}{'-':>8}{'-':>9}")
    if uncovered:
        print(f"\n# {len(uncovered)} reference(s) outside this observer's "
              "antenna coverage — those bands are UNCALIBRATED, not healthy.",
              file=sys.stderr)
    if any(r["drift"] is None for r in rows):
        print("# drift n/a: needs >=5 gain-pinned samples over the baseline "
              "window. Baselines are multi-day medians by design (S7).",
              file=sys.stderr)
    return 0


def _ago(ts, now=None):
    """Human 'how long since', or 'never' -- never blank.

    A blank cell reads as a rendering gap; "never" is a finding.  A target
    that has never been heard is exactly the kind of row worth looking at.
    """
    if not ts:
        return "never"
    d = max(0, (now or time.time()) - ts)
    if d < 90:
        return f"{int(d)}s"
    if d < 5400:
        return f"{int(d // 60)}m"
    if d < 172800:
        return f"{int(d // 3600)}h"
    return f"{int(d // 86400)}d"


def _param_summary(params):
    """Compact per-parameter verification state for the table.

    Conflicts are listed first and never abbreviated away: a conflict is
    the single most actionable thing this table can report, because it
    means the catalog and the radio disagree.
    """
    if not params:
        return "-"
    marks = {"verified": "ok", "measured": "1x", "conflict": "CONFLICT",
             "suspect": "suspect", "unverified": "?", "no_claim": "-"}
    order = {"conflict": 0, "suspect": 1, "measured": 2, "unverified": 3,
             "verified": 4, "no_claim": 5}
    items = sorted(params.items(), key=lambda kv: order.get(kv[1]["state"], 9))
    out = []
    for key, v in items:
        label = key.replace("_hz", "").replace("_code", "")
        mark = marks.get(v["state"], v["state"])
        obs = v.get("observed")
        if v["state"] == "conflict" and obs is not None:
            out.append(f"{label}={obs}!{mark}")
        elif v["state"] in ("verified", "measured") and obs is not None:
            out.append(f"{label}={obs}:{mark}")
        else:
            out.append(f"{label}:{mark}")
    return " ".join(out)


def cmd_monitor_status(args, cfg):
    """Milestone 1 answer: per known channel, last heard + params verified.

    Pass --plan to include targets that have never produced a single
    check.  Without it this can only report channels already in the
    database, which would quietly hide a target that has never been
    visited at all -- the exact blind spot monitoring exists to remove.
    """
    store = Store(cfg["db"])
    targets = None
    if args.plan:
        plan = config.load_plan(args.plan)
        targets = (plan.get("monitor") or {}).get("resolved") or []
        if not targets:
            print(f"# {args.plan} has no monitor targets", file=sys.stderr)
    rows = store.monitor_status(receiver=args.receiver, targets=targets)
    if not rows:
        print("no monitor checks recorded yet — run a plan with a "
              "'monitor:' block", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    now = time.time()
    print(f"{'CHANNEL':<26}{'MHZ':>11}  {'LAST HEARD':>10}{'CHECKED':>9}"
          f"{'HEARD':>8}{'RX':>4}  PARAMS")
    for r in rows:
        heard = f"{r['hearings'] or 0}/{r['checks'] or 0}"
        print(f"{r['target'][:26]:<26}{r['freq_hz']/1e6:>11.4f}  "
              f"{_ago(r['last_heard'], now):>10}{_ago(r['last_checked'], now):>9}"
              f"{heard:>8}{r['rx_heard'] or 0:>4}  "
              f"{_param_summary(r['params'])}")

    silent = [r for r in rows if not r["hearings"]]
    conflicts = [r for r in rows
                 if any(v["state"] == "conflict" for v in r["params"].values())]
    unchecked = [r for r in rows if not r["checks"]]
    print()
    if conflicts:
        print(f"# {len(conflicts)} channel(s) CONFLICT with ssrf-lite: "
              f"{', '.join(r['target'] for r in conflicts)}", file=sys.stderr)
    if silent:
        print(f"# {len(silent)} checked but never heard — could be genuinely "
              f"quiet, out of range, or wrong in the catalog.", file=sys.stderr)
    if unchecked:
        print(f"# {len(unchecked)} target(s) never checked yet.",
              file=sys.stderr)
    print("# 'ok' = parameter measured consistently on >=2 hearings. "
          "'1x' = seen once, needs one more. RX = distinct receivers that "
          "heard it (>=2 is independent corroboration).", file=sys.stderr)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="survey",
        description="Multi-SDR RF landscape surveying (receive-only, always)")
    p.add_argument("--config", help="site config YAML")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list SDRs by serial, role, busy state")

    r = sub.add_parser("run", help="run a scan plan on one device")
    r.add_argument("plan", help="scan plan YAML")
    r.add_argument("--serial", required=True, help="device serial (not index)")
    r.add_argument("--passes", type=int, default=None,
                   help="stop after N passes (default: plan's setting; 0=forever)")
    r.add_argument("--force", action="store_true",
                   help="take the device from current holders")

    rel = sub.add_parser("release", help="force-free a wedged device")
    rel.add_argument("--serial", required=True)

    rep = sub.add_parser("report", help="active-channel summary")
    rep.add_argument("--receiver", help="filter by device serial")
    rep.add_argument("--min-snr", type=float, default=6.0)

    c = sub.add_parser("candidates", help="ssrf-lite candidate stubs (YAML)")
    c.add_argument("--min-gated", type=int, default=2)

    si = sub.add_parser("station-init", help="create this station's keypair")
    si.add_argument("--id", required=True, help="stable station id (e.g. bowmanville)")
    si.add_argument("--key", help="private key path (default ~/.config/rf-survey/station.key)")

    sm = sub.add_parser("submit", help="package + publish evidence over MQTT")
    sm.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                    help="keep running, submitting every N seconds")

    bc = sub.add_parser("beacon-check",
                        help="measure reference beacons to calibrate an observer")
    bc.add_argument("--serial", required=True, help="device serial (not index)")
    bc.add_argument("--references", help="reference set YAML (default: bundled)")
    bc.add_argument("--region", default="chicago",
                    help="bundled reference set to use (default: chicago)")
    bc.add_argument("--integration", type=float, default=8,
                    help="seconds per reference (default: 8)")
    bc.add_argument("--baseline-days", type=int, default=7,
                    help="drift baseline window (default: 7)")
    bc.add_argument("--skip-uncovered", action="store_true",
                    help="skip references outside the declared antenna bands "
                         "instead of measuring them (measuring is the "
                         "default; the reading is recorded as no_reference "
                         "and never feeds a score)")
    bc.add_argument("--force", action="store_true",
                    help="take the device from current holders")
    bc.add_argument("--json", action="store_true")

    ms = sub.add_parser("monitor-status",
                        help="per known channel: last heard + params verified")
    ms.add_argument("--plan", help="scan plan YAML, to include targets that "
                                   "have never been checked")
    ms.add_argument("--receiver", help="filter by device serial")
    ms.add_argument("--json", action="store_true")

    args = p.parse_args(argv)
    cfg = config.load_config(args.config)
    return {"devices": cmd_devices, "run": cmd_run, "release": cmd_release,
            "report": cmd_report, "candidates": cmd_candidates,
            "station-init": cmd_station_init, "submit": cmd_submit,
            "beacon-check": cmd_beacon_check,
            "monitor-status": cmd_monitor_status}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
