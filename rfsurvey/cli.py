"""survey CLI."""
import argparse
import logging
import sys

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

    args = p.parse_args(argv)
    cfg = config.load_config(args.config)
    return {"devices": cmd_devices, "run": cmd_run, "release": cmd_release,
            "report": cmd_report, "candidates": cmd_candidates}[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
