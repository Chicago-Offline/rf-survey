#!/usr/bin/env python3
"""Generate rf-survey monitor targets from ssrf-lite records.

This is the ssrf-lite -> rf-survey leg of the feedback loop: the catalog
says what we believe exists, and this turns each belief into something the
monitor will go and physically check.

Usage:
    tools/gen_targets.py --ssrf ~/src/ssrf-lite \
        --include 'ssrf/systems/US/IL/Cook/**/amateur/*.yml' \
        --priority 2 -o plans/targets-ham.yml

Conventions that matter, and why:

* For a repeater assignment we monitor the chain's `rx` leg, because in
  ssrf-lite `rx` is the repeater OUTPUT -- the downlink a receiver can
  actually hear.  Monitoring `tx` (the 467/144 input) would listen for
  handhelds we are not in range of and would report every working
  repeater as silent.

* The expected output tone is `mode.ctcss_rx_hz`, not `ctcss_tx_hz`.
  Those genuinely differ in the wild: the O'Hare .575 GMRS machine in this
  catalog carries 141.3 on the INPUT only and transmits carrier squelch,
  and a monitor expecting 141.3 on the output would mark a perfectly
  healthy repeater as conflicting forever.

* Three-way tone expectation (see rfsurvey/targets.py):
    ctcss_rx_hz present        -> expect that tone
    only ctcss_tx_hz present   -> expect NO output tone (claimed CSQ)
    neither present            -> no expectation; nothing to verify
  The middle case is a positive claim and is emitted as an explicit null.
  Treating it as "unknown" would silently drop a verifiable fact; treating
  absence-of-both as "CSQ" would manufacture conflicts on every record
  whose tone simply was never researched.
"""
import argparse
import glob
import os
import sys

import yaml

# Catalog mode.type -> our decoder.  Anything unmapped is skipped rather
# than guessed: pointing the nfm decoder at a P25/NXDN channel produces
# "heard, no tone" forever, which reads like a verified-quiet channel.
DECODER_BY_MODE = {
    "FM": "nfm", "NFM": "nfm", "FMN": "nfm", "AM": "nfm",
    "DMR": "dmr",
}


def targets_from_doc(doc, priority, want_usage=("repeater",)):
    chains = {c["id"]: c for c in doc.get("rf_chains") or []}
    out, skipped = [], []
    for a in doc.get("assignments") or []:
        chain = chains.get(a.get("rf_chain_id"))
        if not chain:
            skipped.append((a.get("id"), "no rf_chain"))
            continue
        if want_usage and a.get("usage") not in want_usage:
            skipped.append((a.get("id"), f"usage={a.get('usage')}"))
            continue
        # Repeater output. Simplex/base records have no separate output, so
        # fall back to tx -- for those, tx IS what is on the air.
        leg = chain.get("rx") or chain.get("tx") or {}
        freq = leg.get("freq_mhz")
        if not freq:
            skipped.append((a.get("id"), "no freq_mhz"))
            continue
        mode = chain.get("mode") or {}
        mtype = (mode.get("type") or "").upper()
        decoder = DECODER_BY_MODE.get(mtype)
        if not decoder:
            skipped.append((a.get("id"), f"unmapped mode {mtype or '?'}"))
            continue

        expect = {}
        if mtype:
            expect["mode"] = mtype
        if "ctcss_rx_hz" in mode and mode["ctcss_rx_hz"] is not None:
            expect["ctcss_hz"] = float(mode["ctcss_rx_hz"])
        elif mode.get("ctcss_tx_hz") is not None:
            expect["ctcss_hz"] = None          # claimed CSQ on the output
        if "dcs_rx_code" in mode and mode["dcs_rx_code"] is not None:
            expect["dcs_code"] = str(mode["dcs_rx_code"]).zfill(3)
            # Polarity is part of the claim where the catalog states it.
            # Nothing can verify it yet (detect_dcs does not decode the
            # word at all), but recording the expectation now means the
            # grade flips on its own once Golay decode lands, rather than
            # needing every target regenerated.
            if mode.get("dcs_rx_polarity"):
                expect["dcs_polarity"] = str(mode["dcs_rx_polarity"])
        if mode.get("color_code") is not None:
            expect["color_code"] = int(mode["color_code"])

        out.append({
            "name": a.get("channel_name") or a["id"],
            "freq_mhz": round(float(freq), 6),
            "ssrf_id": a["id"],
            "decoder": decoder,
            "priority": priority,
            "expect": expect,
        })
    return out, skipped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssrf", default="~/src/ssrf-lite",
                    help="ssrf-lite checkout root")
    ap.add_argument("--include", action="append", required=True,
                    help="glob (relative to --ssrf), repeatable")
    ap.add_argument("--priority", type=int, default=2)
    ap.add_argument("--usage", default="repeater",
                    help="comma-separated assignment usages, or 'any'")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    args = ap.parse_args(argv)

    root = os.path.expanduser(args.ssrf)
    if not os.path.isdir(root):
        ap.error(f"ssrf-lite checkout not found: {root}")
    usage = None if args.usage == "any" else tuple(
        u.strip() for u in args.usage.split(",") if u.strip())

    targets, skipped, files = [], [], []
    for pat in args.include:
        hits = sorted(glob.glob(os.path.join(root, pat), recursive=True))
        if not hits:
            print(f"warning: no files matched {pat!r}", file=sys.stderr)
        files += hits
    for path in files:
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
        t, s = targets_from_doc(doc, args.priority, usage)
        targets += t
        skipped += [(os.path.relpath(path, root), *row) for row in s]

    # Same frequency + same name twice would double-book a schedule slot.
    seen, dedup = set(), []
    for t in targets:
        key = (t["freq_mhz"], t["name"])
        if key in seen:
            print(f"warning: duplicate target {t['name']} @ {t['freq_mhz']}",
                  file=sys.stderr)
            continue
        seen.add(key)
        dedup.append(t)
    dedup.sort(key=lambda t: t["freq_mhz"])

    doc = yaml.safe_dump({"targets": dedup}, sort_keys=False,
                         default_flow_style=False, allow_unicode=True)
    header = (f"# GENERATED by tools/gen_targets.py from ssrf-lite\n"
              f"# {len(dedup)} target(s) from {len(files)} record file(s).\n"
              f"# Do not hand-edit; re-run the generator.  Pin local\n"
              f"# hypotheses in the plan's inline monitor.targets instead.\n")
    out = header + doc
    if args.out:
        with open(os.path.expanduser(args.out), "w") as fh:
            fh.write(out)
        print(f"wrote {args.out}: {len(dedup)} targets", file=sys.stderr)
    else:
        sys.stdout.write(out)

    if skipped:
        print(f"\nskipped {len(skipped)}:", file=sys.stderr)
        for row in skipped[:40]:
            print("  " + " ".join(str(x) for x in row), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
