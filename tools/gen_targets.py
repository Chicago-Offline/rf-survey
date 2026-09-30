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
  actually hear.  Monitoring `tx` (the 467/144/448 input) would listen
  for handhelds we are not in range of and would report every working
  repeater as silent.

  ssrf-lite states this perspective explicitly ("docs: clarify tx/rx
  perspective is radio-centric"): rx is what YOUR RADIO receives.  It is
  confirmed by WA9ORC's published data -- 2m is 146.760 out / 146.160 in
  and the catalog has tx=146.16 rx=146.76; 70cm is 443.750 out /
  448.750 in and the catalog has tx=448.75 rx=443.75.

  Do NOT "improve" this to min(tx_freq, rx_freq).  The lower leg is the
  output only when the input sits above it, which is true on 70cm (+5
  MHz) and false on 2m and 220 (negative offset), so min() silently
  points every 2m and 220 repeater at its input.

  Simplex records state the frequency once, on rx, and carry a tx block
  holding only emission -- hence the fallback tests for a frequency
  rather than for a non-empty leg dict.

  🔴 Caveat: not every system in ssrf-lite has been migrated to this
  convention.  "fix(cfmc): tx/rx were station-centric, reversed on all 7
  chains" fixed CFMC only; W9DUP/DARC in DuPage still yields rx=144.83
  and rx=223.08, which are inputs.  Sanity-check a regenerated list by
  confirming each channel name matches its frequency before trusting it.

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

* --max-distance-mi fences targets to what an observer could plausibly
  hear.  This is not an optimisation, it is a correctness fix: a Chicago
  dongle pointed at a Green Bay repeater reads heard=0 forever, which is
  indistinguishable from a dead local machine and would publish a false
  "never heard" finding.  Out-of-range records stay in ssrf-lite; they
  just do not become targets.  A location with no coordinates is KEPT and
  reported, never silently dropped -- unknown distance is not evidence of
  being far away, and dropping it would hide a real catalog record.
"""
import argparse
import glob
import math
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


def haversine_mi(lat1, lon1, lat2, lon2):
    """Great-circle miles. Flat-radius fencing only needs this much."""
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def site_coords(doc):
    """station_id -> (lat, lon), via stations[].location_id."""
    locs = {}
    for L in doc.get("locations") or []:
        lat = L.get("lat", L.get("latitude"))
        lon = L.get("lon", L.get("longitude"))
        if lat is not None and lon is not None:
            locs[L["id"]] = (float(lat), float(lon))
    out = {}
    for s in doc.get("stations") or []:
        c = locs.get(s.get("location_id"))
        if c:
            out[s["id"]] = c
    return out


def in_bands(freq, bands):
    return not bands or any(lo <= freq <= hi for lo, hi in bands)


def station_orgs(doc):
    """station_id -> lowercased "orgid orgname" haystack for --org."""
    orgs = {o["id"]: (o.get("name") or "") for o in
            doc.get("organizations") or []}
    out = {}
    for s in doc.get("stations") or []:
        oid = s.get("organization_id") or ""
        out[s["id"]] = f"{oid} {orgs.get(oid, '')}".lower()
    return out


def targets_from_doc(doc, priority, want_usage=("repeater",), origin=None,
                     max_mi=None, bands=(), org=None):
    chains = {c["id"]: c for c in doc.get("rf_chains") or []}
    coords = site_coords(doc)
    orgs = station_orgs(doc)
    out, skipped = [], []
    for a in doc.get("assignments") or []:
        chain = chains.get(a.get("rf_chain_id"))
        if not chain:
            skipped.append((a.get("id"), "no rf_chain"))
            continue
        if want_usage and a.get("usage") not in want_usage:
            skipped.append((a.get("id"), f"usage={a.get('usage')}"))
            continue
        # One ssrf-lite file often holds every agency in a town (Evanston
        # has PD, FD, Public Works, Electric, Water and parking in one
        # doc).  Filtering here keeps a per-agency target list generated
        # rather than hand-trimmed, so it can be regenerated when the
        # catalog changes instead of drifting.
        if org and org not in orgs.get(chain.get("station_id"), ""):
            skipped.append((a.get("id"), f"org!={org}"))
            continue
        tx_leg = chain.get("tx") or {}
        rx_leg = chain.get("rx") or {}
        # Always take the rx leg.  In ssrf-lite, rx is the frequency you
        # RECEIVE to hear the system -- the repeater output / downlink --
        # and tx is what you would transmit on, the input / uplink.  (The
        # RFChain docstring says "transmitter + receiver for a station",
        # which reads backwards; the data is unambiguous and consistent.
        # CFMC 70cm is tx=448.75 rx=443.75 and WA9ORC publishes 443.750
        # output with a +5 MHz input; CFMC 2m is tx=146.16 rx=146.76 and
        # WA9ORC publishes 146.760 output with -600 kHz; Evanston Fire
        # dispatch is tx=159.4275 rx=155.6925 and 155.6925 is the
        # dispatch channel.)
        #
        # This previously picked min(tx, rx) on the theory that the lower
        # leg is the output.  That is offset-sign roulette: right on 70cm
        # (input +5 MHz), WRONG on 2m and 220 (input below output), so
        # every 2m and 220 repeater in a generated list was pointed at
        # its INPUT.  An observer parked on a repeater input hears only
        # nearby users' uplinks, never the repeater itself, so those
        # targets read as dead no matter how busy the machine is.
        #
        # Simplex records carry the frequency once, on rx, with a tx
        # block holding only emission.  rx-first handles them for free;
        # the old "tx_leg or rx_leg" fallback tested dict truthiness,
        # picked that frequency-less tx block, and silently dropped every
        # conventional simplex channel (e.g. Evanston Fire Old 154.160).
        leg = rx_leg if rx_leg.get("freq_mhz") else tx_leg
        freq = leg.get("freq_mhz")
        if not freq:
            skipped.append((a.get("id"), "no freq_mhz"))
            continue
        if not in_bands(float(freq), bands):
            skipped.append((a.get("id"), f"band {freq}"))
            continue
        if origin and max_mi:
            c = coords.get(chain.get("station_id"))
            if c is None:
                # Unknown distance is not evidence of distance. Keep it,
                # but say so -- a silently dropped local repeater is a
                # worse failure than one extra target to check.
                skipped.append((a.get("id"), "NO COORDS (kept)"))
            else:
                d = haversine_mi(origin[0], origin[1], c[0], c[1])
                if d > max_mi:
                    skipped.append(
                        (a.get("id"), f"{d:.0f} mi > {max_mi:.0f}"))
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
    ap.add_argument("--org", metavar="SUBSTR",
                    help="only assignments whose station belongs to an "
                         "organization whose id or name contains SUBSTR "
                         "(case-insensitive), e.g. --org efd")
    ap.add_argument("--usage", default="repeater",
                    help="comma-separated assignment usages, or 'any'")
    ap.add_argument("--origin", metavar="LAT,LON",
                    help="observer position for --max-distance-mi")
    ap.add_argument("--max-distance-mi", type=float,
                    help="drop targets whose site is farther than this")
    ap.add_argument("--band", action="append", metavar="LO-HI",
                    help="MHz range to keep, repeatable (e.g. 144-148)")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    args = ap.parse_args(argv)

    root = os.path.expanduser(args.ssrf)
    if not os.path.isdir(root):
        ap.error(f"ssrf-lite checkout not found: {root}")
    usage = None if args.usage == "any" else tuple(
        u.strip() for u in args.usage.split(",") if u.strip())

    origin = None
    if args.origin:
        try:
            lat, lon = (float(x) for x in args.origin.split(","))
        except ValueError:
            ap.error("--origin must be LAT,LON")
        origin = (lat, lon)
    # Fail loudly rather than silently monitoring the whole midwest: a
    # radius with no origin is a fence that is not actually there.
    if args.max_distance_mi and not origin:
        ap.error("--max-distance-mi requires --origin")

    bands = []
    for b in args.band or []:
        try:
            lo, hi = (float(x) for x in b.split("-"))
        except ValueError:
            ap.error(f"--band must be LO-HI, got {b!r}")
        bands.append((lo, hi))

    targets, skipped, files = [], [], []
    for pat in args.include:
        hits = sorted(glob.glob(os.path.join(root, pat), recursive=True))
        if not hits:
            print(f"warning: no files matched {pat!r}", file=sys.stderr)
        files += hits
    for path in files:
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
        t, s = targets_from_doc(doc, args.priority, usage, origin=origin,
                                org=(args.org or "").lower() or None,
                                max_mi=args.max_distance_mi,
                                bands=tuple(bands))
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
    fence = ""
    if args.max_distance_mi:
        fence = (f"# Fenced to {args.max_distance_mi:.0f} mi of "
                 f"{args.origin}.\n")
    if bands:
        fence += "# Bands: " + ", ".join(
            f"{lo:g}-{hi:g} MHz" for lo, hi in bands) + "\n"
    header = (f"# GENERATED by tools/gen_targets.py from ssrf-lite\n"
              f"# {len(dedup)} target(s) from {len(files)} record file(s).\n"
              + fence +
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
