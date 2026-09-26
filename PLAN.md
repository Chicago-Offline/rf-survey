# PLAN.md — rf-survey design & roadmap

## Problem

Current state: `voicewatch2.py` + `harvest_ids.py` + shell wrappers on meshpi.
Known pain points (all field-verified):

- Device claims collide; releasing a dongle takes a 3-step kill + poll dance.
- Scanner loops never exit on their own; supervision is ad-hoc `timeout`s.
- Dongles are not interchangeable (E4000 vs R820T2, ~9.6 dB floor offset);
  cross-receiver comparisons are invalid unless done as per-channel SNR.
- No location tagging, no GPS, no portable config — everything is meshpi-shaped.
- Evidence → ssrf-lite is a manual transcription step.

## Architecture

```
┌─────────────┐   ┌──────────────┐   ┌──────────────┐
│ Device Mgr  │──▶│ Scan Engine  │──▶│ Observation  │
│ (serial→role│   │ (sweep/dwell │   │ Store        │
│  claim/free)│   │  per device) │   │ (sqlite)     │
└─────────────┘   └──────┬───────┘   └──────┬───────┘
                         │                  │
┌─────────────┐   ┌──────▼───────┐   ┌──────▼───────┐
│ Location    │──▶│ Decoders     │   │ Reporters    │
│ (static/gpsd│   │ (rtl_power,  │   │ (ssrf-lite   │
│  NMEA)      │   │  rtl_fm,     │   │  candidates, │
└─────────────┘   │  dsd-fme)    │   │  CSV, JSON)  │
                  └──────────────┘   └──────────────┘
```

### 1. Device manager
- Enumerate SDRs by **serial number**, not index (indexes reshuffle).
- Per-device profile: tuner type, max gain, role, noise-floor calibration.
- Exclusive claim with lease + verified release (poll `rtl_test`-style claim
  until free; never assume a kill worked).
- Scoped teardown: kill by device-matched command line (`rtl:<dev>:` for
  dsd-fme, `-d <dev>` for rtl_* tools), never bare `pkill <decoder>`.
- Start with rtl-sdr; abstract enough that SoapySDR (Airspy, HackRX, SDRplay)
  slots in later.

### 2. Location provider
- Static: lat/lon/elev in config.
- GPS: gpsd first (handles most USB pucks), raw NMEA serial as fallback.
- Every observation row carries a position + fix quality; mobile surveys
  (truck Pi) get a track, fixed sites get a constant.

### 3. Scan engine
- Declarative **scan plan** (YAML/TOML):
  - bands (start/stop/step), integration time, gain
  - dwell rules: SNR-over-median threshold, min hits, dwell duration
  - decoder per dwell (nfm, dmr, p25, ...)
  - schedule (continuous, cron-like windows, N passes)
- Sweep via `rtl_power` (or Soapy equivalent) → per-bin median tracking →
  SNR = peak − that bin's own median (never raw dB).
- Dwell hands the frequency to a decoder subprocess with bounded lifetime
  (`timeout` built in, supervisor reaps orphans at startup).
- Concurrent loops: one engine per claimed device; releases scoped per device.

### 4. Decoders (wrapped, not reimplemented)
- `rtl_power` — spectrum sweep
- `rtl_fm` + squelch — analog FM dwell / audio logging
- `dsd-fme` — DMR/NXDN/P25 decode; parse color code, TGs, radio IDs, LCNs
- Parser layer normalizes each decoder's log format into observation records.
- ⚠️ Activity gating before trusting digital decoders — dsd-family tools can
  fabricate sync on pure noise. A decode only counts if the energy gate
  agreed the channel was active.

### 5. Observation store
- SQLite, append-only observations:
  `(ts, receiver_serial, freq, snr_db, duration, decoder, meta_json, lat, lon, fix)`
- Derived tables: per-channel duty cycle, activity histograms, harvested IDs.
- All comparisons within one receiver unless calibration data says otherwise.

### 6. Reporters
- `survey report` — human summary: active channels, duty cycles, decode facts.
- `survey candidates` — ssrf-lite-shaped YAML stubs with evidence citations
  (observation IDs, dwell counts, date range) and a confidence grade.
  Deliberately **not** auto-PR: a human reviews before anything lands in
  ssrf-lite (one dwell ≠ one system).
- CSV/JSON export for mapping (fold in location → coverage heatmaps later).

## Hard rules (inherited from field experience)

1. **Receive-only.** No TX path in this tool, ever.
2. **SNR, not raw dB** — per-channel median as the floor.
3. **Never compare across receivers** without explicit calibration data.
4. **Equal-length captures** when comparing duty cycles (max-hold grows with
   capture length).
5. **Verified device release** — poll until claimable, don't trust the kill.
6. **Energy gate before decode trust** — decoders lie on noise.

## Milestones

- **M0 — skeleton**: repo layout, config schema, device enumeration by serial,
  static location. `survey devices` lists dongles with tuner + role.
- **M1 — sweep**: `rtl_power` wrapper, per-bin median/SNR, sqlite store,
  `survey sweep <plan>`. Replaces the sweep half of voicewatch2.
- **M2 — dwell + decode**: FM and DMR dwell with activity gating, bounded
  decoder lifetimes, scoped teardown. Replaces voicewatch2 entirely.
- **M3 — location**: gpsd/NMEA provider, position on every observation,
  mobile-survey track support (12vpi use case).
- **M4 — multi-SDR**: concurrent engines, per-device roles, calibration
  offsets, `dev_compare`-style cross-check built in.
- **M5 — reporting**: ssrf-lite candidate generator with evidence citations;
  CSV/JSON export.
- **M6 — portability**: SoapySDR backend, packaging (pipx), docs for
  non-meshpi installs.

## Implementation notes

- Python 3.11+, minimal deps (pyyaml, sqlite3 stdlib, gpsd-py3 optional).
- Existing prior art to mine: `~/uhf_survey/voicewatch2.py`, `harvest_ids.py`,
  `hunt/dev_compare.py`, `hunt/survey_dev.sh` (meshpi + workspace copies).
- Config lives in one file; a scan plan is shareable so others can reproduce
  a survey.

## Open questions

- Name the CLI `survey` or `rfsurvey`?
- Trunked-system following (P25/DMR Cap+) in scope, or defer to sdrtrunk?
- Web UI / live view — later, or never (keep it headless + reports)?
