# rf-survey

An SDR-based RF landscape surveying tool. Plug in one or more SDRs, set your
location (manually or from GPS), and scan. Output is structured evidence good
enough to validate or extend [ssrf-lite](https://github.com/Chicago-Offline/ssrf-lite)
entries.

**Status: planning.** See [PLAN.md](PLAN.md).

## Why

We've been surveying the UHF landscape around Chicagoland with RTL-SDRs on a
Raspberry Pi (sweep → dwell → decode → harvest). It works, but it's a pile of
ad-hoc scripts with hard-won operational lore baked into a skill file instead
of code. This repo turns that into a tool anyone can run.

## Goals

- **Multi-SDR aware** — enumerate dongles by serial, assign roles per tuner
  (e.g. R820T2 for sensitivity-critical digital dwell, E4000 for strong-signal
  FM), run concurrent scan loops without device-claim collisions.
- **Location-tagged** — static lat/lon/elevation or live GPS (gpsd/NMEA), so
  every observation carries where it was heard.
- **Scan plans** — declarative sweep/dwell definitions: bands, step, gain,
  dwell triggers, decoder (FM, DMR via dsd-fme, P25, NXDN...).
- **Evidence, not vibes** — SNR relative to per-channel noise median, duty
  cycle over time, decode metadata (color code, talkgroups, radio IDs), all
  timestamped and receiver-tagged.
- **ssrf-lite output** — candidate system reports that map onto the ssrf-lite
  schema, with confidence levels and cited observations.

## Non-goals

- Transmitting. **Receive-only, always.** Surveyed systems are licensed to
  other people.
- Replacing dsd-fme / rtl-sdr / SoapySDR — we orchestrate, they decode.

## License

TBD (likely MIT).
