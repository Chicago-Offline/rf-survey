"""SDR device management: enumerate by serial, claim, verified release.

Hard rules (field-verified on meshpi):
- Address devices by SERIAL, never index (indexes reshuffle on replug).
- A kill is not a release: poll until the device is actually claimable.
- Teardown must be scoped to the device: dsd-fme names it 'rtl:<idx>:',
  rtl_* tools use '-d <idx>'. Never bare-pkill a decoder name.
"""
import os
import re
import signal
import subprocess
import time

LIST_RE = re.compile(r"^\s*(\d+):\s+(.*?),\s+(.*?),\s+SN:\s*(\S*)\s*$")

# Known tuner sensitivity roles.  E4000 = strong-signal/analog work,
# R820T/R820T2 = sensitivity-critical digital dwell.
TUNER_ROLES = {
    "E4000": "analog",
    "R820T": "digital",
    "R820T2": "digital",
}


class DeviceError(Exception):
    pass


def list_devices(timeout=15):
    """Enumerate RTL-SDRs WITHOUT claiming any.

    rtl_test prints the device table before trying to open the requested
    index; asking for an out-of-range index (-d 99) lists then fails,
    leaving every real device untouched.
    """
    try:
        p = subprocess.run(
            ["rtl_test", "-d", "99"],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise DeviceError("rtl_test not found — install rtl-sdr")
    except subprocess.TimeoutExpired:
        raise DeviceError("rtl_test hung — a device may be wedged")
    devices = []
    for line in (p.stdout + p.stderr).splitlines():
        m = LIST_RE.match(line)
        if m:
            devices.append({
                "index": int(m.group(1)),
                "vendor": m.group(2).strip(),
                "product": m.group(3).strip(),
                "serial": m.group(4).strip(),
            })
    return devices


def resolve(serial, devices=None):
    """serial -> current index. Raises if absent or ambiguous."""
    devices = devices if devices is not None else list_devices()
    hits = [d for d in devices if d["serial"] == serial]
    if not hits:
        raise DeviceError(f"no device with serial {serial!r} "
                          f"(present: {[d['serial'] for d in devices]})")
    if len(hits) > 1:
        raise DeviceError(f"multiple devices share serial {serial!r} — reflash EEPROM serials")
    return hits[0]["index"]


def is_claimable(index, timeout=15):
    """True if the device can actually be opened right now."""
    try:
        p = subprocess.run(
            ["timeout", str(timeout), "rtl_test", "-d", str(index), "-t"],
            capture_output=True, text=True, timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        return False
    blob = p.stdout + p.stderr
    return "usb_claim_interface error" not in blob


def _pids_matching(pattern):
    p = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    me = os.getpid()
    return [int(x) for x in p.stdout.split() if x.strip() and int(x) != me]


def holders(index):
    """PIDs holding this device, matched on device-scoped command lines."""
    pids = set()
    for pat in (f"rtl:{index}:",          # dsd-fme
                f"rtl_fm -d {index}",
                f"rtl_power -d {index}",
                f"rtl_test -d {index}",
                f"rtl_sdr -d {index}"):
        pids.update(_pids_matching(pat))
    return sorted(pids)


def release(index, wait_s=90):
    """Kill device-scoped holders, then poll until genuinely claimable.

    Kill children (orphaned decoders reparent to init and keep the USB
    claim) as well as supervisors. SIGTERM first, SIGKILL for stragglers.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = holders(index)
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        time.sleep(2)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if is_claimable(index):
            return True
        time.sleep(3)
    return False


class Claim:
    """Context manager: resolve serial -> index, ensure claimable, release on exit."""

    def __init__(self, serial, force=False):
        self.serial = serial
        self.force = force
        self.index = None

    def __enter__(self):
        self.index = resolve(self.serial)
        if not is_claimable(self.index):
            if not self.force:
                raise DeviceError(
                    f"device {self.serial} (index {self.index}) is busy; "
                    f"holders: {holders(self.index)} (use --force to take it)")
            if not release(self.index):
                raise DeviceError(f"could not free device {self.serial}")
        return self

    def __exit__(self, *exc):
        release(self.index)
        return False
