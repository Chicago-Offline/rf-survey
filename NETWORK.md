# NETWORK.md — distributed survey network → ssrf-lite

Status: **design accepted, MQTT transport chosen** (2026-09-27). Extends
PLAN.md beyond M6: multiple rf-survey stations submit observations to a
central aggregator, which validates and feeds verification data back into
ssrf-lite and codeplugger.

## Vision

A handful of fixed rf-survey stations around Chicagoland (Bowmanville,
Bucktown, Jefferson Park, Lake Saugana IN, ...) plus mobile units,
continuously scanning. Observations flow to a central store, get validated
automatically (received → decoded → voice-confirmed → recorded), and tag
ssrf-lite records with evidence. Codeplugs built from ssrf-lite then carry
real confidence: these channels were actually heard, recently, near you.

```
┌────────────┐ MQTT  ┌────────────┐  PR bot  ┌───────────┐
│ station ×N │──────▶│ aggregator │─────────▶│ ssrf-lite │
│ (rf-survey)│ obs   │ + obs DB   │ verify   │ (curated) │
└────────────┘       └─────┬──────┘ blocks   └─────┬─────┘
                           │ query                 │
                     ┌─────▼─────────────────┐     │
                     │ codeplugger           │◀────┘
                     │ min_verification /    │
                     │ receivable_at filters │
                     └───────────────────────┘
```

## 1. Station identity

Each instance gains a station config on top of the existing receiver/site
config:

- `station_id` — short stable name (`bowmanville`, `12vpi-mobile`)
- Ed25519 keypair — signs every submission batch; aggregator holds the
  registry of public keys (enrollment is manual, small trusted set)
- site: lat/lon/elev, antenna description, fixed vs mobile
- receivers: serials + calibration offsets (already in rf-survey config)

Fixed sites report a constant position; mobile stations (12vpi) attach the
GPS fix per observation (M3).

## 2. Transport: MQTT

Reuse the chioff broker infrastructure (same pattern as the MeshCore
observers): WebSockets + TLS + token auth against `wsmqtt.chioff.com`
(dev: `wsmqtt-dev.chicagooffline.com`).

Topics:

- `rfsurvey/obs/<station_id>` — signed observation batches (JSON, gzip),
  QoS 1
- `rfsurvey/status/<station_id>` — heartbeat: uptime, receivers claimed,
  scan plan hash, last obs ts (retained)
- `rfsurvey/cmd/<station_id>` — reserved for later (plan updates); OFF by
  default, stations never auto-execute remote commands without opt-in

Store-and-forward: the local sqlite store is the source of truth; a
publisher marks rows submitted only on broker ACK. Offline stations catch
up on reconnect. Batches are idempotent (batch UUID) so replays dedupe.

## 3. Aggregator (`ssrf-obs`, separate repo/service)

Small service (dev EC2 first, prod later):

- subscribes to `rfsurvey/obs/#`, verifies signature against station
  registry, rejects unknown/invalid
- dedupes by batch UUID, appends to central observations DB (Postgres, or
  sqlite+litestream to start)
- maintains derived tables: per-channel × per-station duty cycle, last
  heard, decode metadata (CC/TG/NAC/radio IDs), verification tier

The obs DB is **append-only evidence, deliberately outside ssrf-lite** —
the git repo stays curated and lean; hourly observations don't churn it.

## 4. Validation ladder

Per (system, channel, station), automatically assigned:

| Tier | Meaning                                           | Source          |
|------|---------------------------------------------------|-----------------|
| V0   | energy seen (SNR-over-median, activity-gated)     | sweep           |
| V1   | decoded — CC/TG/NAC consistent with ssrf record   | dsd-fme parse   |
| V2   | voice confirmed on dwell audio                    | voice detector  |
| V3   | recorded clip archived                            | dwell capture   |

Promotion / flagging rules (aggregator policy, tunable):

- `verified`: ≥ V1 from ≥ 2 stations across ≥ 3 distinct days
- `flagged`: decode metadata contradicts the ssrf record (wrong color
  code, unexpected talkgroups) → human review queue
- `stale`: no V0+ at any in-range station for N days (default 90)

Hard rules inherited from PLAN.md still apply: energy gate before any
decode is trusted (dsd fabricates sync on noise), SNR relative to
per-channel median, no cross-receiver comparison without calibration.

## 5. ssrf-lite writeback

A bot opens PRs against ssrf-lite that touch **only** a `verification:`
block on existing records:

```yaml
verification:
  level: V2
  status: verified        # verified | flagged | stale | unverified
  last_heard: 2026-09-25
  stations: 3
  observations: ssrf-obs:sys/example#a1b2   # ref into the obs DB
```

New systems discovered by survey remain **human-reviewed candidates**
(`survey candidates`, M5) — one dwell ≠ one system. The bot never creates
or deletes system records.

## 6. Codeplugger integration

Two new profile filters, backed by aggregator queries (or a periodically
exported snapshot for offline builds):

- `min_verification: V1` — only program channels with recent decode
  evidence
- `receivable_at: [lat, lon]` — include only channels heard above an SNR
  threshold at station(s) within X km; per-station SNR history gives a
  coarse receivability map from fixed sites, densified over time by
  mobile tracks

## Milestones

- **M7 — submit**: station identity + keypair, MQTT publisher with
  store-and-forward, batch schema, heartbeats
- **M8 — aggregate**: ssrf-obs service, signature verification, obs DB,
  validation-ladder derivation
- **M9 — writeback**: verification-block PR bot against ssrf-lite
- **M10 — consume**: codeplugger `min_verification` / `receivable_at`

Prerequisites: M2 (gated dwell + decode) and M5 (candidate reports) —
stations must produce trustworthy evidence before networking them.

## Open questions

- Obs DB engine: sqlite+litestream is enough for <10 stations; Postgres if
  we ever open enrollment wider
- Recording retention/consent policy for V3 clips (storage + what we keep)
- Snapshot format for offline codeplugger builds (single sqlite file?)
- Broker load: batch interval default (5 min?) vs near-real-time
