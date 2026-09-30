# rf-survey

**🌐 [Project site](https://rfsurvey.chicagooffline.com/)**

An SDR-based RF landscape surveying tool. Plug in one or more SDRs, set your
location (manually or from GPS), and scan. Output is structured evidence good
enough to validate or extend [ssrf-lite](https://github.com/Chicago-Offline/ssrf-lite)
entries.

**Status: active development.** See [PLAN.md](PLAN.md).

## Quick start

**Requirements:** an RTL-SDR dongle, macOS or Raspberry Pi OS / Debian / Ubuntu.

Plug in the dongle, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/Chicago-Offline/rf-survey/main/install.sh | bash
```

That installs the rtl-sdr userland, puts `survey` on your PATH, and starts
setup. Re-running it is safe — every step is idempotent.

Setup asks **two questions**: where the receiver is, and what to listen to.
Everything else is detected. Then:

```bash
survey run        # uses the profile and receiver from your config
survey report     # see what it heard
```

<details>
<summary>What the installer does, and how to steer it</summary>

- **macOS** — `brew install rtl-sdr coreutils pipx` (Homebrew must already be
  installed).
- **Debian / Raspberry Pi OS** — `apt-get install rtl-sdr coreutils procps
  python3-venv python3-pip` (plus `pipx` where packaged), and blacklists the
  kernel DVB-T driver that otherwise grabs the dongle
  (`usb_claim_interface error -6`). The blacklist is appended to its own file
  and skipped if anything already covers it.
- Installs rf-survey with `pipx`, falling back to a venv under
  `~/.local/share/rf-survey` on older releases without pipx.

Environment knobs:

| Variable | Effect |
|----------|--------|
| `RFS_REF=<branch\|tag>` | install a ref other than `main` |
| `RFS_REPO=<git url>` | install from a different repo or fork |
| `RFS_SETUP=0` | install only, do not launch the wizard |
| `RFS_NO_SUDO=1` | never call sudo; print the apt command instead |

</details>

### Manual install

```bash
pipx install git+https://github.com/Chicago-Offline/rf-survey
survey setup
```

> **PEP 668 note:** On Debian 12+ and Ubuntu 22.04+ `pip install` into the
> system Python is blocked. Use `pipx` — it creates an isolated environment
> automatically. Install pipx with `sudo apt install pipx`.

<details>
<summary>Repo checkout / development</summary>

```bash
git clone https://github.com/Chicago-Offline/rf-survey
cd rf-survey
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
survey setup
```

</details>

## Setup

```bash
survey setup              # two questions: location, profile
survey setup --advanced   # observer ID, gpsd, per-receiver antenna details
```

Setup writes `~/.config/rf-survey/config.yml` — commented, and meant to be
edited by hand afterwards. It picks the observer ID from the hostname, and the
tuner role and gain from the detected dongle.

### Antenna details are optional

The quick path does **not** ask about your antenna, and that costs you nothing
up front. An undescribed antenna records coverage as `unverified` — an honest
unknown, not an error. Surveying works normally.

Describing it is worth doing once your install settles, because it is what
lets a beacon check tell **real silence** apart from **a frequency this antenna
was never going to hear**. Run `survey setup --advanced`, or fill in the
commented `antenna:` block in the config.

⚠️ `bands_mhz` is the antenna's **usable receive reach**, not its resonant
band. Overstate it and silence on an unhearable frequency gets scored as a real
negative.

### Bundled survey profiles

| Name | Description |
|------|-------------|
| `uhf-dmr` | UHF 450–470 MHz business/industrial DMR hunt |
| `chicago-ham` | Chicago 2 m + 70 cm amateur repeater discovery (FM) |
| `2m-fm` | 2 m amateur FM activity survey |

Setup stores your choice as `plan:` in the config, so `survey run` needs no
arguments. Both can still be overridden:

```bash
survey plans                       # list bundled profiles
survey run                         # config default profile + receiver
survey run uhf-dmr                 # override the profile
survey run 2m-fm --serial BENCH    # override both
```

`--serial` is only required when more than one receiver is configured —
rf-survey will not guess which antenna a scan belongs to.

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
