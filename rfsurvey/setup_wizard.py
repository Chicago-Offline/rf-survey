"""Interactive first-run setup: survey setup.

The quick path asks two questions, because two things are genuinely
unknowable from this machine: where the receiver is, and what you want it
to listen to.  Dependencies, hardware, tuner role and gain are detected.

Antenna description is deliberately NOT asked for here.  It matters -- it
is what lets a beacon check tell real silence apart from a frequency the
antenna was never going to hear -- but it is an advanced refinement, not a
precondition for contributing.  Leaving it out records the honest
"unverified" coverage state (references.UNVERIFIED) rather than a
flattering guess, so the quick path costs accuracy nothing.

    survey setup              two questions, sane defaults
    survey setup --advanced   observer ID, gpsd, per-device antenna details

The config it writes is commented, because the user will edit it later and
a bare YAML dump teaches them nothing.
"""

import os
import re
import shutil
import socket
import time

from . import config as config_mod
from . import deps
from . import devices as devices_mod
from . import plans as plans_mod


class SetupError(Exception):
    pass


# bands_mhz is the USABLE RECEIVE REACH of the antenna, not its resonant
# band.  Beacon coverage checks use it to decide whether a silent channel
# is real silence or an antenna that was never going to hear it, so being
# honest here matters more than being flattering.
ANTENNA_PRESETS = [
    {"key": "discone",
     "label": "Discone (wideband scanner antenna)",
     "model": "discone", "type": "discone", "gain_dbi": 2.0,
     "bands_mhz": [[25, 1300]]},
    {"key": "dualband",
     "label": "Dual-band 2 m / 70 cm vertical",
     "model": "dual-band vertical", "type": "vertical", "gain_dbi": 3.0,
     "bands_mhz": [[136, 174], [400, 470]]},
    {"key": "whip",
     "label": "Telescopic whip from the RTL-SDR kit",
     "model": "telescopic whip", "type": "omni", "gain_dbi": 0.0,
     "bands_mhz": [[100, 600]]},
    {"key": "uhf",
     "label": "UHF-only vertical (400-470 MHz)",
     "model": "UHF vertical", "type": "vertical", "gain_dbi": 3.0,
     "bands_mhz": [[400, 470]]},
    {"key": "vhf",
     "label": "VHF-only vertical (136-174 MHz)",
     "model": "VHF vertical", "type": "vertical", "gain_dbi": 3.0,
     "bands_mhz": [[136, 174]]},
    {"key": "custom",
     "label": "Something else (enter the details)",
     "model": None, "type": "omni", "gain_dbi": 0.0,
     "bands_mhz": [[100, 600]]},
]

RULE = "-" * 68

QUICK_STEPS = 4
ADVANCED_STEPS = 6
TOTAL_STEPS = QUICK_STEPS       # rebound by run() per mode

# R820T2 maximum.  A silent, sane default: too much gain shows up as
# obvious noise, too little just loses weak signals.
DEFAULT_GAIN = 49.6


def _out(text=""):
    print(text)


def _heading(step, title):
    _out()
    _out(RULE)
    _out("  Step %d/%d  %s" % (step, TOTAL_STEPS, title))
    _out(RULE)


def _input(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise SetupError("input closed; run survey setup in a terminal")
    except KeyboardInterrupt:
        raise SetupError("cancelled")


def ask(question, default=None, validate=None, allow_blank=False):
    """Free-text question with a default and an optional validator."""
    suffix = " [%s]: " % default if default is not None else ": "
    while True:
        raw = _input(question + suffix).strip()
        if not raw:
            if default is not None:
                raw = str(default)
            elif allow_blank:
                return ""
            else:
                _out("  (a value is required)")
                continue
        if validate:
            problem = validate(raw)
            if problem:
                _out("  %s" % problem)
                continue
        return raw


def ask_float(question, default=None, lo=None, hi=None):
    def validate(raw):
        try:
            value = float(raw)
        except ValueError:
            return "enter a number"
        if lo is not None and value < lo:
            return "must be >= %s" % lo
        if hi is not None and value > hi:
            return "must be <= %s" % hi
        return None

    return float(ask(question, default=default, validate=validate))


def ask_yes_no(question, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _input("%s [%s]: " % (question, hint)).strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        _out("  answer y or n")


def ask_choice(question, labels, default_index=0):
    """Numbered menu.  Returns the chosen index."""
    _out(question)
    for i, label in enumerate(labels, 1):
        _out("   %d) %s" % (i, label))

    def validate(raw):
        if not raw.isdigit() or not (1 <= int(raw) <= len(labels)):
            return "enter a number from 1 to %d" % len(labels)
        return None

    raw = ask("  choice", default=str(default_index + 1), validate=validate)
    return int(raw) - 1


def parse_latlon(raw):
    """'41.88, -87.63' or '41.88 -87.63' -> (lat, lon).  Raises ValueError."""
    parts = [p for p in re.split(r"[,\s]+", raw.strip()) if p]
    if len(parts) != 2:
        raise ValueError("enter two numbers separated by a comma, "
                         "e.g. 41.8781, -87.6298")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        raise ValueError("latitude and longitude must be numbers")
    if not -90.0 <= lat <= 90.0:
        raise ValueError("latitude must be between -90 and 90")
    if not -180.0 <= lon <= 180.0:
        raise ValueError("longitude must be between -180 and 180")
    return lat, lon


def ask_latlon(question):
    def validate(raw):
        try:
            parse_latlon(raw)
        except ValueError as exc:
            return str(exc)
        return None

    return parse_latlon(ask(question, validate=validate))


def _default_station_id():
    host = socket.gethostname().split(".")[0]
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", host).strip("-")
    return slug[:32] or "observer-1"


def _validate_station_id(raw):
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$", raw):
        return ("use 2-32 characters: letters, digits, dash, underscore, "
                "starting with a letter or digit")
    return None


def _tuner_of(device):
    """Best-effort tuner name from the rtl_test product string."""
    blob = "%s %s" % (device.get("vendor", ""), device.get("product", ""))
    for tuner in devices_mod.TUNER_ROLES:
        if tuner.lower() in blob.lower():
            return tuner
    return None


def _scan_devices(quiet=False):
    try:
        return devices_mod.list_devices()
    except devices_mod.DeviceError as exc:
        if not quiet:
            _out("  %s" % exc)
        return []


def _describe_devices(devs):
    for d in devs:
        tuner = _tuner_of(d) or "unknown tuner"
        _out("  found  index %s  SN=%s  %s %s  (%s)"
             % (d["index"], d["serial"] or "(blank)", d["vendor"],
                d["product"], tuner))
    blank = [d for d in devs if not d["serial"]]
    if blank:
        _out()
        _out("  Note: %d device(s) report a blank serial, and the survey"
             % len(blank))
        _out("  addresses devices by serial. Name each one:")
        _out("      rtl_eeprom -d %s -s BENCH" % blank[0]["index"])
        _out("  then unplug and replug it.")


def load_existing(config_path):
    """Whatever is already in the config, or {} if there is nothing usable.

    Setup is not only a first-run tool: people re-run it after changing
    antennas or moving a receiver.  Re-running must never quietly downgrade
    a config that already holds more than the quick path asks for.
    """
    path = os.path.expanduser(config_path)
    if not os.path.exists(path):
        return {}
    try:
        import yaml
        with open(path) as fh:
            data = yaml.safe_load(fh)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def existing_station_id(existing):
    value = (existing.get("station") or {}).get("id")
    return value if isinstance(value, str) and value else None


def existing_location(existing):
    loc = existing.get("location")
    if not isinstance(loc, dict):
        return None
    if loc.get("mode") == "static" and loc.get("lat") is not None:
        return loc
    if loc.get("mode") == "gpsd":
        return loc
    return None


def describe_location(loc):
    if loc.get("mode") == "gpsd":
        return "gpsd at %s:%s" % (loc.get("host"), loc.get("port"))
    text = "%s, %s" % (_num(loc["lat"]), _num(loc["lon"]))
    if loc.get("alt_m") is not None:
        text += " at %s m" % _num(loc["alt_m"])
    return text


def auto_devices(devs, existing_devices=None):
    """Receiver config with no questions asked.

    Role comes from the tuner (devices_mod.TUNER_ROLES), gain from a sane
    default.  No antenna key: coverage stays honestly 'unverified' until
    someone runs survey setup --advanced or edits the config.

    A receiver already described in the config is carried over untouched.
    Re-running the quick path must not silently strip an antenna someone
    took the trouble to measure -- that would turn verified coverage back
    into 'unverified' and quietly devalue their existing observations.
    """
    existing_devices = existing_devices or {}
    configured = {}
    for d in devs:
        serial = d["serial"]
        if not serial:
            continue
        prior = existing_devices.get(serial)
        if isinstance(prior, dict) and prior.get("role"):
            configured[serial] = prior
            continue
        tuner = _tuner_of(d)
        configured[serial] = {
            "role": devices_mod.TUNER_ROLES.get(tuner, "digital"),
            "gain": DEFAULT_GAIN,
        }
    return configured


# --------------------------------------------------------------------------
# quick path
# --------------------------------------------------------------------------

def step_system(skip=False):
    """Dependencies + hardware in one silent-on-success step.

    Returns (ok, devices).  Only prints the full dependency table when
    something is actually wrong.
    """
    _heading(1, "Checking this machine")
    results = deps.check_all()
    missing = deps.missing_required(results)

    if missing:
        _out(deps.format_results(results))
        hint = deps.aggregate_install_hint(results)
        _out()
        _out("  Missing required tools: %s"
             % ", ".join(r["name"] for r in missing))
        if hint:
            _out("  Install them with:")
            _out("      %s" % hint)
        if not skip:
            _out("  Then run:  survey setup")
            return False, []
        _out()
        _out("  --skip-checks given; continuing anyway.")
    else:
        _out("  Dependencies: ok (rtl-sdr userland present)")

    devs = _scan_devices()
    while True:
        if devs:
            _describe_devices(devs)
            return True, devs
        probe = deps.probe_rtl_test()
        _out("  No RTL-SDR detected: %s" % probe["detail"])
        if probe.get("hint"):
            _out("  fix: %s" % probe["hint"])
        _out()
        if not ask_yes_no("  Plugged it in? Scan again", default=True):
            _out("  Continuing with no device; add one to the config later.")
            return True, []
        time.sleep(1)
        devs = _scan_devices()


def step_location(step=2, prior=None):
    """The one fact that turns a hit into evidence: where the receiver is."""
    _heading(step, "Where is this receiver?")
    if prior:
        _out("  Currently recorded: %s" % describe_location(prior))
        if ask_yes_no("  Still accurate? Keep it", default=True):
            return prior
        _out()
    _out("  Without a location an observation is just a frequency. Street")
    _out("  address precision is not needed -- the block is plenty.")
    _out()
    lat, lon = ask_latlon("  Latitude, longitude")
    return {"mode": "static", "lat": lat, "lon": lon}


def step_profile(step=3, prior=None):
    """Pick a bundled survey profile (plan)."""
    _heading(step, "What should it listen to?")
    catalog = plans_mod.bundled_catalog()
    if not catalog:
        _out("  No bundled plans found, which should not happen in a normal")
        _out("  install. Reinstall, or pass a plan file path to survey run.")
        return None
    _out("  A profile is a ready-made scan plan: bands, dwell times and")
    _out("  decoders. You can switch profiles any time; nothing here locks in.")
    _out()
    names = [name for name, _p, _d in catalog]
    default_index = names.index(prior) if prior in names else 0
    labels = ["%-13s %s" % (name, desc or "") for name, _p, desc in catalog]
    index = ask_choice("  Profiles shipped with rf-survey:", labels,
                       default_index=default_index)
    return catalog[index][0]


# --------------------------------------------------------------------------
# advanced path
# --------------------------------------------------------------------------

def step_dependencies(skip=False):
    _heading(1, "Checking dependencies")
    results = deps.check_all()
    _out(deps.format_results(results))
    missing = deps.missing_required(results)
    if not missing:
        _out()
        _out("  All required tools are present.")
        return True

    hint = deps.aggregate_install_hint(results)
    _out()
    _out("  Missing required tools: %s"
         % ", ".join(r["name"] for r in missing))
    if hint:
        _out("  Install them with:")
        _out("      %s" % hint)
    _out("  Then run:  survey setup")
    if skip:
        _out()
        _out("  --skip-checks given; continuing anyway.")
        return True
    return False


def step_hardware():
    _heading(2, "Looking for an RTL-SDR")
    while True:
        devs = _scan_devices()
        if devs:
            _describe_devices(devs)
            return devs

        probe = deps.probe_rtl_test()
        _out()
        _out("  No RTL-SDR detected: %s" % probe["detail"])
        if probe.get("hint"):
            _out("  fix: %s" % probe["hint"])
        _out()
        if not ask_yes_no("  Plugged it in? Scan again", default=True):
            _out("  Continuing with no device; add one to the config later.")
            return []
        time.sleep(1)


def step_station():
    _heading(3, "Who is reporting")
    _out("  The observer ID labels every observation you contribute.")
    _out()
    station_id = ask("  Observer ID", default=_default_station_id(),
                     validate=_validate_station_id)

    _out()
    _out("  Receiver location. This is what makes an observation useful to")
    _out("  anyone else: without it, a hit is just a frequency.")
    mode_index = ask_choice("  How should location be determined?",
                            ["Fixed location I enter now",
                             "gpsd (a GPS receiver on this machine)"],
                            default_index=0)
    if mode_index == 0:
        lat, lon = ask_latlon("  Latitude, longitude")
        location = {
            "mode": "static",
            "lat": lat,
            "lon": lon,
            "alt_m": ask_float("  Antenna altitude above sea level, metres",
                               default=180, lo=-500, hi=9000),
        }
    else:
        location = {
            "mode": "gpsd",
            "host": ask("  gpsd host", default="127.0.0.1"),
            "port": int(ask_float("  gpsd port", default=2947, lo=1,
                                  hi=65535)),
        }
    return station_id, location


def step_devices(devs):
    _heading(4, "Antenna and receiver details")
    if not devs:
        _out("  No device detected, so there is nothing to describe here.")
        return {}

    configured = {}
    roles = ["digital", "analog"]
    for d in devs:
        serial = d["serial"]
        if not serial:
            _out("  Skipping index %s: no serial to key the config on."
                 % d["index"])
            continue

        _out()
        _out("  Device SN=%s  (%s %s)" % (serial, d["vendor"], d["product"]))
        tuner = _tuner_of(d)
        role_default = devices_mod.TUNER_ROLES.get(tuner, "digital")
        role = roles[ask_choice(
            "  Role for this receiver:",
            ["digital (R820T/R820T2 - narrowband digital work)",
             "analog (E4000 - wide analog coverage)"],
            default_index=(roles.index(role_default)
                           if role_default in roles else 0))]
        gain = ask_float("  Tuner gain in dB (49.6 is max for an R820T2)",
                         default=DEFAULT_GAIN, lo=0, hi=60)

        _out()
        _out("  What is connected to this receiver?")
        preset = ANTENNA_PRESETS[ask_choice(
            "  Antenna:", [p["label"] for p in ANTENNA_PRESETS],
            default_index=0)]

        if preset["key"] == "custom":
            model = ask("  Antenna model or description")
            ant_type = ask("  Antenna type (omni/discone/yagi/vertical)",
                           default="omni")
            gain_dbi = ask_float("  Antenna gain in dBi", default=0.0,
                                 lo=-10, hi=30)
            _out()
            _out("  Usable receive range. NOT the resonant band -- the range")
            _out("  where this antenna can actually hear something. Overstate")
            _out("  it and silence gets scored as a real negative.")
            lo_mhz = ask_float("  Lowest usable frequency, MHz", lo=0.1,
                               hi=6000)
            hi_mhz = ask_float("  Highest usable frequency, MHz", lo=lo_mhz,
                               hi=6000)
            bands = [[lo_mhz, hi_mhz]]
        else:
            model = preset["model"]
            ant_type = preset["type"]
            gain_dbi = preset["gain_dbi"]
            bands = preset["bands_mhz"]
            pretty = ", ".join("%g-%g MHz" % (b[0], b[1]) for b in bands)
            _out("  Usable receive range recorded as: %s" % pretty)
            _out("  (That is a coverage claim. Narrow it in the config if")
            _out("   your install does not really hear that whole range.)")

        _out()
        placement = ask("  Where is the antenna? (attic, roof, window, ...)",
                        default="indoor")
        height = ask_float("  Height above ground, metres", default=5,
                           lo=0, hi=500)

        configured[serial] = {
            "role": role,
            "gain": gain,
            "antenna": {"model": model, "type": ant_type,
                        "gain_dbi": gain_dbi, "bands_mhz": bands},
            "placement": {"location": placement, "height_m": height},
        }
    return configured


def step_plan():
    """Advanced-path plan picker (kept for the 6-step numbering)."""
    return step_profile(step=5)


# --------------------------------------------------------------------------
# config emission
# --------------------------------------------------------------------------

def _num(value):
    """Render a number without a trailing .0 when it is really an int."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "%g" % value if isinstance(value, float) else str(value)


def _bands(bands):
    inner = ", ".join("[%s, %s]" % (_num(b[0]), _num(b[1])) for b in bands)
    return "[%s]" % inner


def _device_template(prefix, serial="BENCH"):
    """Commented device block used for both 'no device' and 'no antenna'."""
    return [
        '%s"%s":' % (prefix, serial),
        "%s  role: digital" % prefix,
        "%s  gain: 49.6" % prefix,
        "%s  antenna:" % prefix,
        "%s    model: discone" % prefix,
        "%s    type: discone" % prefix,
        "%s    gain_dbi: 2.0" % prefix,
        "%s    bands_mhz: [[25, 1300]]" % prefix,
        "%s  placement:" % prefix,
        "%s    location: attic" % prefix,
        "%s    height_m: 6" % prefix,
    ]


def render_config(station_id, location, device_cfg, db_path, key_path,
                  plan_name=None):
    """Emit a commented config.yml.  The comments are the point: this is
    the file the operator edits next, and the traps in it are not obvious.

    Tolerates a partially described setup: a device may carry only role and
    gain, and a static location may omit alt_m.  Those gaps are recorded as
    gaps, never papered over with an invented value.
    """
    out = []
    add = out.append
    add("# rf-survey configuration")
    add("# Written by: survey setup")
    add("# Re-run that command any time, or edit this file by hand.")
    add("")
    add("# Observation database. Created on first run.")
    add("db: %s" % db_path)
    add("")
    add("# Default survey profile, used when survey run is given no plan.")
    add("# survey plans lists the alternatives; survey run <name> overrides.")
    if plan_name:
        add("plan: %s" % plan_name)
    else:
        add("# plan: 2m-fm")
    add("")
    add("location:")
    if location["mode"] == "static":
        add("  mode: static")
        add("  lat: %s" % _num(location["lat"]))
        add("  lon: %s" % _num(location["lon"]))
        if location.get("alt_m") is not None:
            add("  alt_m: %s" % _num(location["alt_m"]))
        else:
            add("  # alt_m: 180   # metres above sea level, optional")
    else:
        add("  mode: gpsd")
        add("  host: %s" % location["host"])
        add("  port: %s" % location["port"])
    add("")
    add("station:")
    add("  id: %s" % station_id)
    add("  key: %s" % key_path)
    add("")
    add("  # Upstream submission. Observations are stored locally whether or")
    add("  # not this works; submission is a separate, optional step.")
    add("  # You need a token before survey submit will authenticate:")
    add("  # send the public key printed by survey station-init to the")
    add("  # operator, then save the token they issue to token_file.")
    add("  mqtt:")
    add("    server: wsmqtt.chioff.com")
    add("    port: 443")
    add("    transport: websockets")
    add("    tls: true")
    add("    token_file: ~/.config/rf-survey/mqtt.token")
    add("    topic_prefix: rfsurvey")
    add("")
    add("# Receivers, keyed by USB serial. Run survey devices to list them.")
    if not device_cfg:
        add("devices: {}")
        add("")
        add("# No device was detected during setup. Add one like this:")
        add("#")
        add("# devices:")
        for line in _device_template("#   "):
            add(line)
        return "\n".join(out) + "\n"

    add("devices:")
    for serial, dev in device_cfg.items():
        ant = dev.get("antenna")
        place = dev.get("placement")
        # Quoted: an all-digit serial (81388637) otherwise loads as an int
        # and never matches the string passed to --serial.
        add('  "%s":' % serial)
        add("    role: %s" % dev["role"])
        add("    gain: %s" % _num(dev["gain"]))
        if ant:
            add("    antenna:")
            add("      model: %s" % ant["model"])
            add("      type: %s" % ant["type"])
            add("      gain_dbi: %s" % _num(ant["gain_dbi"]))
            add("      # USABLE RECEIVE REACH, not the resonant band. Coverage")
            add("      # checks use this to tell real silence apart from an")
            add("      # antenna that was never going to hear that frequency.")
            add("      bands_mhz: %s" % _bands(ant["bands_mhz"]))
        else:
            add("    # Antenna not described, so coverage checks report")
            add("    # 'unverified' for this receiver. That is an honest")
            add("    # unknown, not an error -- surveying works fine without")
            add("    # it. Describing the antenna is what lets a beacon check")
            add("    # tell real silence from a frequency this antenna was")
            add("    # never going to hear. Fill this in by hand, or run:")
            add("    #     survey setup --advanced")
            add("    #antenna:")
            add("    #  model: discone")
            add("    #  type: discone")
            add("    #  gain_dbi: 2.0")
            add("    #  bands_mhz: [[25, 1300]]")
        if place:
            add("    placement:")
            add("      location: %s" % place["location"])
            add("      height_m: %s" % _num(place["height_m"]))
        elif not ant:
            add("    #placement:")
            add("    #  location: attic")
            add("    #  height_m: 6")
    return "\n".join(out) + "\n"


def write_config(path, text):
    """Write the config, backing up anything already there."""
    path = os.path.expanduser(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    backup = None
    if os.path.exists(path):
        backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    with open(path, "w") as fh:
        fh.write(text)
    return path, backup


def step_write(config_path, text, step=None):
    _heading(step or TOTAL_STEPS, "Writing configuration")
    path = os.path.expanduser(config_path)
    if os.path.exists(path):
        _out("  %s already exists." % path)
        if not ask_yes_no("  Replace it? (the old one is backed up)",
                          default=True):
            _out()
            _out("  Left the existing config alone. Proposed config:")
            _out()
            for line in text.splitlines():
                _out("    %s" % line)
            return None, None
    written, backup = write_config(path, text)
    _out("  wrote %s" % written)
    if backup:
        _out("  previous config saved as %s" % backup)
    return written, backup


def ensure_station_key(station_id, key_path):
    """Create the signing key if it is missing.  Never overwrites."""
    from . import station as station_mod

    expanded = os.path.expanduser(key_path)
    if os.path.exists(expanded):
        return None, "key already exists at %s" % expanded
    try:
        st = station_mod.Station.create(station_id, key_path)
    except station_mod.StationError as exc:
        return None, str(exc)
    except Exception as exc:  # cryptography missing shows up here too
        return None, str(exc)
    return st.public_key_b64, None


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

DEFAULT_DB = "~/.local/share/rf-survey/observations.db"
DEFAULT_KEY = "~/.config/rf-survey/station.key"


def run(config_path=None, skip_checks=False, advanced=False):
    """Drive the wizard.  Returns a process exit code."""
    global TOTAL_STEPS
    TOTAL_STEPS = ADVANCED_STEPS if advanced else QUICK_STEPS

    config_path = config_path or config_mod.DEFAULT_CONFIG
    existing = load_existing(config_path)
    prior_id = existing_station_id(existing)
    prior_loc = existing_location(existing)
    prior_devices = existing.get("devices") if isinstance(
        existing.get("devices"), dict) else {}
    prior_plan = existing.get("plan") if isinstance(
        existing.get("plan"), str) else None

    _out()
    _out("rf-survey setup")
    if existing:
        _out("Found an existing config at %s."
             % os.path.expanduser(config_path))
        _out("Anything already set is kept unless you change it here.")
    if advanced:
        _out("Advanced mode: observer ID, location source, and the antenna")
        _out("details for every receiver.")
    else:
        _out("Two questions -- where this receiver is, and what to listen to.")
        _out("Everything else is detected. Need the rest? survey setup --advanced")

    if advanced:
        if not step_dependencies(skip=skip_checks):
            return 1
        devs = step_hardware()
        station_id, location = step_station()
        device_cfg = step_devices(devs)
        plan_name = step_plan()
    else:
        ok, devs = step_system(skip=skip_checks)
        if not ok:
            return 1
        station_id = prior_id or _default_station_id()
        location = step_location(step=2, prior=prior_loc)
        plan_name = step_profile(step=3, prior=prior_plan)
        device_cfg = auto_devices(devs, prior_devices)

    text = render_config(station_id, location, device_cfg,
                         DEFAULT_DB, DEFAULT_KEY, plan_name=plan_name)
    written, _backup = step_write(config_path, text)

    _out()
    _out(RULE)
    _out("  Setup complete")
    _out(RULE)

    pubkey, problem = ensure_station_key(station_id, DEFAULT_KEY)
    if pubkey:
        _out("  signing key: %s" % os.path.expanduser(DEFAULT_KEY))
        _out("  public key:  %s" % pubkey)
        _out("  Send that public key to the operator to get a submit token.")
    elif problem:
        _out("  signing key: not created (%s)" % problem)
        _out("  Observations still record locally. To submit later:")
        _out("      pipx install --force 'rf-survey[submit]'")
        _out("      survey station-init --id %s" % station_id)
    _out()

    if written is None:
        _out("  Config was NOT written, so nothing below will run yet.")
        _out("  Re-run survey setup, or save the config shown above.")
        return 1

    serial = next(iter(device_cfg), None)
    _out("  Start surveying:")
    if plan_name and serial:
        _out("      survey run")
        _out("  (that is profile %s on receiver %s, both from the config;"
             % (plan_name, serial))
        _out("   override either with: survey run <profile> --serial <sn>)")
    elif plan_name:
        _out("      survey run --serial YOUR_SERIAL")
        _out("  (profile %s comes from the config)" % plan_name)
    else:
        _out("      survey run <profile> --serial YOUR_SERIAL")
    _out()
    _out("  See what it heard:")
    _out("      survey report")
    _out()

    if not advanced:
        _out("  Settings you did not pick (all editable in %s):"
             % os.path.expanduser(config_path))
        _out("      observer ID  %s%s"
             % (station_id, " (kept)" if prior_id else " (from hostname)"))
        if serial:
            dev = device_cfg[serial]
            _out("      receiver     %s, role %s, gain %s"
                 % (serial, dev["role"], _num(dev["gain"])))
            if dev.get("antenna"):
                _out("      antenna      %s (kept from your config)"
                     % dev["antenna"].get("model", "described"))
            else:
                _out("      antenna      not described -> coverage "
                     "'unverified'")
        if not any(d.get("antenna") for d in device_cfg.values()):
            _out()
            _out("  Optional, and worth it once you settle on an install:")
            _out("      survey setup --advanced    describe the antenna, so a")
            _out("                                 silent channel can be told")
            _out("                                 apart from one you cannot "
                 "hear")
        _out()

    _out("  Other useful commands:")
    _out("      survey doctor     re-check dependencies and hardware")
    _out("      survey devices    list receivers")
    _out("      survey plans      list bundled survey profiles")
    _out()
    return 0
