# rf-survey

**🌐 [Project site](https://rfsurvey.chicagooffline.com/)**

An SDR-based RF landscape surveying tool. Plug in one or more SDRs, set your
location (manually or from GPS), and scan. Output is structured evidence good
enough to validate or extend [ssrf-lite](https://github.com/Chicago-Offline/ssrf-lite)
entries.

**Status: active development.** See [PLAN.md](PLAN.md).

## Quick start

**Requirements:** Python 3.9+, an RTL-SDR dongle.

```bash
# 1. Install rtl-sdr tools (Debian/Ubuntu)
sudo apt install -y rtl-sdr

# On macOS
brew install rtl-sdr

# 2. Install rf-survey
pipx install git+https://github.com/Chicago-Offline/rf-survey

# 3. Plug in your RTL-SDR, then run the setup wizard
survey setup
```

> **PEP 668 note:** On Debian 12+ and Ubuntu 22.04+ `pip install` into the
> system Python is blocked. Use `pipx` as shown above — it creates an
> isolated environment automatically. Install pipx with `sudo apt install pipx`.

The wizard checks your dependencies, detects the dongle, asks a few
questions (observer ID, location, antenna), writes
`~/.config/rf-survey/config.yml`, and prints the exact `survey run`
command to start scanning.

### Manual install (repo checkout / development)

```bash
git clone https://github.com/Chicago-Offline/rf-survey
cd rf-survey
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
survey setup
```

### Bundled survey plans

| Name | Description |
|------|-------------|
| `uhf-dmr` | UHF 450–470 MHz business/industrial DMR hunt |
| `chicago-ham` | Chicago 2 m + 70 cm amateur repeater discovery (FM) |
| `2m-fm` | 2 m amateur FM activity survey |

```bash
survey plans                       # list bundled plans
survey run uhf-dmr --serial BENCH  # run by name (no path needed)
survey run 2m-fm   --serial BENCH
```

### Other useful commands

```bash
survey doctor          # check all dependencies and hardware
survey devices         # list connected SDRs by serial
survey report          # summary of what was heard
survey candidates      # ssrf-lite candidate stubs (YAML)
```

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
