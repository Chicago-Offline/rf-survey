"""Interactive first-run setup: survey setup.

Goal: plug in an SDR, run one command, answer a few questions, start
contributing observations.  Everything this wizard asks for is something
the survey genuinely cannot infer -- where the antenna is, what it can
hear, who is reporting.  Everything else it detects.

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
TOTAL_STEPS = 6


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


# --------------------------------------------------------------------------
# steps
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
        try:
            devs = devices_mod.list_devices()
        except devices_mod.DeviceError as exc:
            devs = []
            _out("  %s" % exc)

        if devs:
            for d in devs:
                tuner = _tuner_of(d) or "unknown tuner"
                _out("  found  index %s  SN=%s  %s %s  (%s)"
                     % (d["index"], d["serial"] or "(blank)", d["vendor"],
                        d["product"], tuner))
            blank = [d for d in devs if not d["serial"]]
            if blank:
                _out()
                _out("  Note: %d device(s) report a blank serial, and the"
                     % len(blank))
                _out("  survey addresses devices by serial. Name each one:")
                _out("      rtl_eeprom -d %s -s BENCH" % blank[0]["index"])
                _out("  then unplug and replug it.")
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
        location = {
            "mode": "static",
            "lat": ask_float("  Latitude (decimal degrees)", lo=-90, hi=90),
            "lon": ask_float("  Longitude (decimal degrees)",
                             lo=-180, hi=180),
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
                         default=49.6, lo=0, hi=60)

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
    _heading(5, "Pick a survey plan")
    catalog = plans_mod.bundled_catalog()
    if not catalog:
        _out("  No bundled plans found, which should not happen in a normal")
        _out("  install. Reinstall, or pass a plan file path to survey run.")
        return None
    labels = ["%-13s %s" % (name, desc or "") for name, _p, desc in catalog]
    index = ask_choice("  Plans shipped with rf-survey:", labels,
                       default_index=0)
    return catalog[index][0]


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


def render_config(station_id, location, device_cfg, db_path, key_path):
    """Emit a commented config.yml.  The comments are the point: this is
    the file the operator edits next, and the traps in it are not obvious.
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
    add("location:")
    if location["mode"] == "static":
        add("  mode: static")
        add("  lat: %s" % _num(location["lat"]))
        add("  lon: %s" % _num(location["lon"]))
        add("  alt_m: %s" % _num(location["alt_m"]))
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
    add("    server: wsmqtt-dev.chicagooffline.com")
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
        add("#   BENCH:")
        add("#     role: digital")
        add("#     gain: 49.6")
        add("#     antenna:")
        add("#       model: discone")
        add("#       type: discone")
        add("#       gain_dbi: 2.0")
        add("#       bands_mhz: [[25, 1300]]")
        add("#     placement:")
        add("#       location: attic")
        add("#       height_m: 6")
        return "\n".join(out) + "\n"

    add("devices:")
    for serial, dev in device_cfg.items():
        ant = dev["antenna"]
        place = dev["placement"]
        add("  %s:" % serial)
        add("    role: %s" % dev["role"])
        add("    gain: %s" % _num(dev["gain"]))
        add("    antenna:")
        add("      model: %s" % ant["model"])
        add("      type: %s" % ant["type"])
        add("      gain_dbi: %s" % _num(ant["gain_dbi"]))
        add("      # USABLE RECEIVE REACH, not the resonant band. Coverage")
        add("      # checks use this to tell real silence apart from an")
        add("      # antenna that was never going to hear that frequency.")
        add("      bands_mhz: %s" % _bands(ant["bands_mhz"]))
        add("    placement:")
        add("      location: %s" % place["location"])
        add("      height_m: %s" % _num(place["height_m"]))
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


def step_write(config_path, text):
    _heading(6, "Writing configuration")
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


def run(config_path=None, skip_checks=False):
    """Drive the whole wizard.  Returns a process exit code."""
    config_path = config_path or config_mod.DEFAULT_CONFIG

    _out()
    _out("rf-survey setup")
    _out("Answer a few questions and this machine starts observing.")

    if not step_dependencies(skip=skip_checks):
        return 1

    devs = step_hardware()
    station_id, location = step_station()
    device_cfg = step_devices(devs)
    plan_name = step_plan()

    text = render_config(station_id, location, device_cfg,
                         DEFAULT_DB, DEFAULT_KEY)
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
        _out("      survey run %s --serial %s" % (plan_name, serial))
    elif plan_name:
        _out("      survey run %s --serial YOUR_SERIAL" % plan_name)
    else:
        _out("      survey run <plan> --serial YOUR_SERIAL")
    _out()
    _out("  See what it heard:")
    _out("      survey report")
    _out()
    _out("  Other useful commands:")
    _out("      survey doctor     re-check dependencies and hardware")
    _out("      survey devices    list receivers")
    _out("      survey plans      list bundled survey plans")
    _out()
    return 0
