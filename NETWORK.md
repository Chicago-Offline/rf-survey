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

## 1. Station identity and observers

Two levels, deliberately separated (amended 2026-09-27):

- **Station** — the trust anchor. One rf-survey install, one `station_id`,
  one Ed25519 keypair, one MQTT credential.
- **Observer** — `(station_id, receiver_serial)`. One SDR together with the
  antenna and placement it is actually attached to. This is the unit that
  gets measured, calibrated, scored and displayed.

Per-SDR keypairs were considered and rejected: dongles sharing a host are not
independently trustworthy, and it doubles enrollment for no security gain.
Signing stays at the station.

Station config:

- `station_id` — short stable name (`bowmanville`, `12vpi-mobile`)
- Ed25519 keypair — signs every submission batch; aggregator holds the
  registry of public keys (enrollment is manual, small trusted set)
- site: lat/lon/elev, fixed vs mobile

Observer config — one entry per receiver under `devices:`, keyed by EEPROM
serial. `role`, `gain` and `floor_offset_db` already exist; `antenna` and
`placement` are the addition:

```yaml
devices:
  BENCH:
    role: digital           # R820T2 — sensitivity-critical DMR dwell
    gain: 49.6
    antenna:
      model: "HT dual-band whip (2m/70cm)"
      type: omni            # omni | discone | yagi | vertical
      gain_dbi: 2.15
      bands_mhz: [[144, 148], [430, 450]]   # where this antenna is real
    placement:
      location: "bench, indoor"
      height_m: 1.1
      notes: "no ground plane"
```

`antenna.bands_mhz` is load-bearing, not documentation: the aggregator uses
it to decide which beacon references an observer may legitimately be scored
against (§7). An observer with no antenna coverage at a reference frequency
is `no_reference` for that band — never scored as healthy by omission.

Fixed sites report a constant position; mobile stations (12vpi) attach the
GPS fix per observation (M3).

**Wire format.** The batch gains a `receivers:` block alongside `site:`
carrying each active observer's descriptor. This is additive:
`rfsurvey.obs.v1` consumers ignore it, and the aggregator treats a missing
descriptor as unknown rather than rejecting the batch. Bump to `v2` only
when scoring starts *requiring* the descriptor.

**Rollup rule.** `receiver` is already carried end to end — sweep summaries
are keyed `(receiver, freq_hz)` and every observation row has it. Beacon
baselines, drift alerts and reliability scores are therefore computed **per
observer** and only rolled up to the station for display. Never average
across observers at ingest: that smears exactly the hardware variance §7
exists to detect.

## 2. Transport: MQTT

Reuse the chioff broker infrastructure (same pattern as the MeshCore
observers): WebSockets + TLS + token auth against `wsmqtt.chioff.com`
(dev: `wsmqtt-dev.chicagooffline.com`).

Topics:

- `rfsurvey/obs/<station_id>` — signed observation batches (JSON, gzip),
  QoS 1. Every batch carries `schema: rfsurvey.obs.v1` — versioned from day
  one (the durable part of trunk-recorder's ecosystem was its stable upload
  schema, not the transport).
- **Aggregate before publish**: sweep data goes up as per-bin summaries
  (median/max/hit-count over the batch window), never raw bins — the
  ElectroSense lesson (~100× bandwidth cut via PSD averaging). Dwell/decode
  observations go up as events.
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
- maintains derived tables: per-channel × per-observer duty cycle, last
  heard, decode metadata (CC/TG/NAC/radio IDs), verification tier
- maintains a per-**observer** `(station_id, receiver)` **reliability score**
  (SatNOGS-style): beacon baseline health, heartbeat regularity, evidence
  contradiction rate. Evidence from a degraded observer is down-weighted in
  promotion rules until it recovers; a station's displayed score is a
  rollup of its observers, never the unit of computation (§1).
- keys observer descriptors (antenna, placement) in a `receivers` table on
  `(station_id, receiver)`, updated from the batch `receivers:` block; needs
  an `observations(station_id, receiver)` index alongside the existing
  `obs_station`

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

**Trunked systems (P25 / DMR Cap+/Con+):** don't reinvent — a station may
run trunk-recorder or sdrtrunk as a side decoder and submit its call
metadata (and recordings) as V2/V3 evidence through the same batch path.
rf-survey's own dwell pipeline stays for conventional/analog and discovery
sweeps. This resolves PLAN.md's open question: integrate, don't reimplement.

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

## 7. Beacon calibration (shared reference signals)

Chicago has constant-power licensed transmitters with known ERP and fixed
locations (Willis/Hancock broadcast masts, NWS). Stations measure a shared
reference list on schedule; the aggregator tracks long-term baselines.

What it gives:

- **Observer health + drift detection** (the big win): an observer whose
  median on a reference drops N dB has a failed dongle, wet feedline, or
  moved antenna — flagged automatically, no human noticing required.
- **Coarse cross-station normalization within a band**: beacon-derived
  offsets weight (never equate) receivability claims between stations.
- **Gain sanity** after config changes.

Rules that keep it honest:

1. **Calibration is band-local.** Antenna response and tuner gain curves
   differ across frequency; an FM-broadcast reading says nothing about
   460 MHz. References per band of interest:
   - VHF-high: **NWS 162.55 MHz (KWO39)** — continuous carrier, constant power
   - FM broadcast for low-VHF sanity
   - **ATSC pilot tones** (~470–600 MHz) for the UHF land-mobile band
2. **Baselines are long-term medians** (days) — tropo/weather/multipath move
   spot readings by a few dB.
3. **Overload-aware**: broadcast signals can compress the front end at survey
   gain; beacon-check passes may use their own gain/attenuation, recorded
   with the measurement.
4. Beacons bound **hardware variance, not propagation** — cross-station SNR
   on a surveyed channel is still weighted evidence, not ground truth.
5. **Score per observer, not per station** (§1). An observer is only scored
   against references its `antenna.bands_mhz` actually covers; everything
   else is `no_reference`. A station with a good VHF observer and a deaf UHF
   one must not average out to "healthy".

**N is measured, not guessed.** The flag threshold above has no defensible
value until we know the system's own noise floor. Two co-sited observers
sharing an antenna model but differing in tuner, gain and placement give
that directly: park both on 162.55 MHz with identical integration and log
for several days. The spread between them, plus each one's day-to-day
wander, is the floor — N must sit above it or the score is alarm spam.
The current bench pair (`BENCH` R820T2 @ 49.6, `SONDE` E4000 @ 42, both on
HT dual-band whips) is exactly this experiment and should be run before any
threshold is committed.

Secondary benefit: a known-good delta between two co-sited observers is a
standing self-test. Months of tracking within X dB followed by divergence
means a dongle or a connector, detectable with no external baseline at all.

⚠️ **Current antenna gap**: HT dual-band whips (2m/70cm) cover the NWS
162.55 reference acceptably and FM broadcast poorly-but-usably, but top out
around 450 MHz — so the **ATSC 470–600 MHz reference has no working antenna
on either bench observer**. UHF land-mobile sweeps (450–470) are running on
an off-band antenna with no way to quantify the loss. Fix the antenna or
mark those observers `no_reference` for UHF; do not score them as healthy.

Implementation: shared `references/chicago.yml` in this repo (freq, kind,
expected-strong/weak hints), a `beacon-check` pass type in scan plans,
aggregator-side baselines + deviation alerts feeding the per-observer
reliability score.

## Prior art (what we reused, what we avoided)

- **Reiter, 30C3 (2013), distributed RTL scanner array** — proved COTS
  feasibility; his pitfalls (antenna variance, heterogeneous dongles) are
  why per-channel-median SNR, no-cross-receiver comparison, and beacon
  calibration are load-bearing here.
- **ElectroSense / ElectroSense+** — dumb sensors / smart backend (adopted:
  validation policy lives only in the aggregator), PSD aggregation before
  upload (adopted). Its decay — open enrollment of uncalibrated volunteer
  sensors nobody could trust — is why enrollment here is **manual and
  small, by design. Open public enrollment is a non-goal.**
- **trunk-recorder / OpenMHz / rdio-scanner** — healthiest living
  capture→ingest ecosystem: stable versioned upload schema (adopted),
  per-site keys with revocation (adopted), solved trunked-call recording
  (integrated as side decoder, not reimplemented).
- **SatNOGS** — station registry, observation vetting, reliability scores
  (adopted), retained status topics as a free network dashboard.
- **BigWhoop, rtlsdr-scanner+GPS** — dead; global open-ended ambition kills
  these projects. Regional + purpose-driven (feed ssrf-lite / codeplugs) is
  the survivable niche.

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
