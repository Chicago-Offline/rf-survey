"""Dependency and environment checks for a fresh install.

The first thing a new observer hits is not radio physics, it is a missing
apt package.  "survey doctor" and the first stage of "survey setup" answer
one question: can this machine actually run a survey, and if not, what is
the exact command to fix it.

Every check returns a hint that is a command the user can paste.  A check
that cannot say how to fix itself is not worth printing.
"""

import os
import platform
import shutil
import subprocess

OK = "ok"
MISSING = "missing"
WARN = "warn"

# Binaries the survey actually execs.  See sweep.py, dwell.py, devices.py.
REQUIRED_BINS = [
    ("rtl_test", "rtl-sdr", "enumerate and claim-test RTL-SDR devices"),
    ("rtl_power", "rtl-sdr", "spectrum sweep"),
    ("rtl_fm", "rtl-sdr", "analog NFM dwell and tone detection"),
]

OPTIONAL_BINS = [
    ("dsd-fme", None, "DMR decode (only needed by decoder: dmr plans)"),
    ("rtl_sdr", "rtl-sdr", "raw IQ capture"),
]

# timeout/pgrep come from coreutils/procps, not rtl-sdr, and stock macOS
# ships neither GNU timeout nor pgrep-compatible procps behaviour.
SHELL_BINS = [
    ("timeout", "coreutils", "bounded lifetime for rtl_fm / dsd-fme / rtl_test"),
    ("pgrep", "procps", "find the process holding a wedged device"),
]


def _os_family():
    if platform.system() == "Darwin":
        return "macos"
    if os.path.exists("/etc/debian_version"):
        return "debian"
    if os.path.exists("/etc/fedora-release"):
        return "fedora"
    if os.path.exists("/etc/arch-release"):
        return "arch"
    return "linux"


FAMILY = _os_family()

_INSTALL = {
    "debian": "sudo apt install -y %s",
    "fedora": "sudo dnf install -y %s",
    "arch": "sudo pacman -S --needed %s",
    "macos": "brew install %s",
    "linux": "install the %s package with your package manager",
}

# Same software, different package name per distro.  None = already present.
_PKG_ALIAS = {
    "macos": {"procps": None},
    "fedora": {"procps": "procps-ng"},
    "arch": {"procps": "procps-ng"},
}

DSD_FME_HINT = ("build from source: https://github.com/lwvmobile/dsd-fme"
                "  (only needed for DMR plans)")


def install_hint(package):
    """Paste-able install command for a package on this OS."""
    if package is None:
        return None
    pkg = _PKG_ALIAS.get(FAMILY, {}).get(package, package)
    if pkg is None:
        return None
    return _INSTALL.get(FAMILY, _INSTALL["linux"]) % pkg


def _check_bin(name, package, why, required):
    path = shutil.which(name)
    if path:
        return {"name": name, "status": OK, "detail": path,
                "why": why, "hint": None, "required": required}
    hint = DSD_FME_HINT if name == "dsd-fme" else install_hint(package)
    return {"name": name, "status": MISSING if required else WARN,
            "detail": "not on PATH", "why": why, "hint": hint,
            "required": required}


def _check_python_module(module, why, extra):
    try:
        __import__(module)
    except ImportError:
        return {"name": module, "status": WARN, "detail": "not importable",
                "why": why, "required": False,
                "hint": "pipx inject rf-survey %s   (or reinstall as: "
                        "pipx install 'rf-survey[%s]')" % (module, extra)}
    return {"name": module, "status": OK, "detail": "importable",
            "why": why, "hint": None, "required": False}


def _dvb_conflict():
    """The kernel grabs RTL dongles for DVB-T unless blacklisted.

    This is the most common "it enumerates but will not open" cause on a
    fresh Debian box, and the symptom (usb_open error -3) reads like a
    permissions problem, so it is worth naming explicitly.
    """
    if FAMILY == "macos":
        return None
    try:
        with open("/proc/modules") as fh:
            loaded = fh.read()
    except OSError:
        return None
    for mod in ("dvb_usb_rtl28xxu", "rtl2832", "rtl2830"):
        if mod in loaded:
            return {
                "name": "dvb driver",
                "status": WARN,
                "detail": "%s is loaded and will claim the dongle" % mod,
                "why": "the DVB-T driver blocks librtlsdr from opening the device",
                "required": False,
                "hint": ("echo 'blacklist dvb_usb_rtl28xxu' | sudo tee "
                         "/etc/modprobe.d/blacklist-rtlsdr.conf && "
                         "sudo modprobe -r dvb_usb_rtl28xxu"),
            }
    return None


def _udev_rules():
    """Without udev rules a non-root user gets usb_open error -3."""
    if FAMILY == "macos":
        return None
    for d in ("/lib/udev/rules.d", "/etc/udev/rules.d", "/usr/lib/udev/rules.d"):
        if not os.path.isdir(d):
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        if any("rtl" in e.lower() for e in entries):
            return {"name": "udev rules", "status": OK,
                    "detail": "rtl-sdr udev rules present",
                    "why": "lets a non-root user open the dongle",
                    "hint": None, "required": False}
    return {
        "name": "udev rules", "status": WARN,
        "detail": "no rtl-sdr udev rules found",
        "why": "without them rtl_test fails as usb_open error -3 unless run as root",
        "required": False,
        "hint": ("reinstall the rtl-sdr package, or install rules from "
                 "https://github.com/osmocom/rtl-sdr into /etc/udev/rules.d "
                 "then: sudo udevadm control --reload-rules"),
    }


def check_all(include_optional=True):
    """Run every environment check.  Returns a list of result dicts."""
    results = []
    for name, package, why in REQUIRED_BINS:
        results.append(_check_bin(name, package, why, required=True))
    for name, package, why in SHELL_BINS:
        results.append(_check_bin(name, package, why, required=True))
    if include_optional:
        for name, package, why in OPTIONAL_BINS:
            results.append(_check_bin(name, package, why, required=False))
        results.append(_check_python_module(
            "cryptography", "sign observations for submission", "submit"))
        results.append(_check_python_module(
            "paho", "publish observations over MQTT", "submit"))
    for extra in (_dvb_conflict(), _udev_rules()):
        if extra:
            results.append(extra)
    return results


def missing_required(results):
    return [r for r in results if r["required"] and r["status"] == MISSING]


def aggregate_install_hint(results):
    """One combined package-manager command covering everything missing."""
    packages = []
    for name, package, _why in REQUIRED_BINS + SHELL_BINS:
        if package is None:
            continue
        for r in results:
            if r["name"] == name and r["status"] == MISSING:
                pkg = _PKG_ALIAS.get(FAMILY, {}).get(package, package)
                if pkg and pkg not in packages:
                    packages.append(pkg)
    if not packages:
        return None
    return _INSTALL.get(FAMILY, _INSTALL["linux"]) % " ".join(packages)


_SYMBOL = {OK: "ok  ", MISSING: "FAIL", WARN: "warn"}


def format_results(results):
    """Human-readable check report."""
    lines = []
    for r in results:
        lines.append("  %s %-14s %s" % (_SYMBOL[r["status"]], r["name"],
                                        r["detail"]))
        if r["status"] != OK:
            lines.append("       why: %s" % r["why"])
            if r.get("hint"):
                lines.append("       fix: %s" % r["hint"])
    return "\n".join(lines)


def probe_rtl_test():
    """Run rtl_test and classify the failure, beyond "no devices".

    devices.list_devices() already parses the device list; this turns the
    common failure modes into advice instead of a raw stderr dump.
    """
    if not shutil.which("rtl_test"):
        return {"status": MISSING, "detail": "rtl_test is not installed",
                "hint": install_hint("rtl-sdr")}
    try:
        p = subprocess.run(["rtl_test", "-d", "99"], capture_output=True,
                           text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return {"status": WARN, "detail": "rtl_test hung",
                "hint": "unplug and replug the dongle, then retry"}
    except OSError as exc:
        return {"status": WARN, "detail": str(exc), "hint": None}

    low = ((p.stdout or "") + (p.stderr or "")).lower()
    if "usb_open error -3" in low or "fix the device permissions" in low:
        rules = _udev_rules() or {}
        return {"status": WARN,
                "detail": "device present but cannot be opened",
                "hint": rules.get("hint") or "check device permissions"}
    if "kernel driver is active" in low or "usb_claim_interface error" in low:
        conflict = _dvb_conflict() or {}
        return {"status": WARN,
                "detail": "a kernel DVB driver is holding the device",
                "hint": conflict.get("hint")
                        or "blacklist dvb_usb_rtl28xxu, then unplug and replug"}
    if "no supported devices found" in low:
        return {"status": WARN, "detail": "no RTL-SDR found on USB",
                "hint": "plug in the dongle, or try another port or cable"}
    return {"status": OK, "detail": "rtl_test responded", "hint": None}
