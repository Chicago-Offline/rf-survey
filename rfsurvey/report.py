"""Reports: human summary and ssrf-lite candidate stubs (never auto-PR)."""
import json
import time


def summary(store, receiver=None, min_snr=6.0):
    lines = ["freq_mhz    hits  gated  best_snr  last_heard           receiver"]
    for f, hits, snr, last, rx, gated in store.active_channels(receiver, min_snr):
        lines.append("%-11.4f %-5d %-6d %-9.1f %-20s %s" % (
            f / 1e6, hits, gated or 0, snr or 0,
            time.strftime("%Y-%m-%d %H:%M", time.localtime(last)), rx))
    if len(lines) == 1:
        lines.append("(no observations yet)")
    return "\n".join(lines)


def candidates(store, min_gated=2):
    """ssrf-lite-shaped YAML stubs for channels with repeated GATED activity.

    Evidence-graded; a human reviews before anything lands in ssrf-lite.
    """
    out = []
    for f, hits, snr, last, rx, gated in store.active_channels():
        if (gated or 0) < min_gated:
            continue
        obs = store.observations_for(f)
        ccs, tgs, rids, decoders = set(), set(), set(), set()
        first_ts = obs[0][0] if obs else last
        for ts, orx, osnr, dur, dec, g, meta in obs:
            decoders.add(dec)
            try:
                m = json.loads(meta or "{}")
            except ValueError:
                m = {}
            ccs.update(m.get("color_codes", []))
            tgs.update(m.get("talkgroups", []))
            rids.update(m.get("radio_ids", []))
        conf = "high" if (gated >= 5 and (ccs or tgs)) else \
               "medium" if gated >= 3 else "low"
        stub = {
            "frequency_mhz": round(f / 1e6, 5),
            "mode": "dmr" if ccs or "dmr" in decoders and (tgs or rids) else "unknown",
            "confidence": conf,
            "evidence": {
                "receiver": rx,
                "gated_observations": gated,
                "total_hits": hits,
                "best_snr_db": round(snr or 0, 1),
                "first_heard": time.strftime("%Y-%m-%d", time.localtime(first_ts)),
                "last_heard": time.strftime("%Y-%m-%d", time.localtime(last)),
            },
        }
        if ccs:
            stub["color_codes"] = sorted(ccs)
        if tgs:
            stub["talkgroups"] = sorted(tgs)
        if rids:
            stub["radio_ids"] = sorted(rids)
        out.append(stub)
    return out
