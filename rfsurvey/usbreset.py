"""USB port reset for a wedged RTL-SDR.

Field-verified on meshpi 2026-10-06.  Both dongles entered a state where
rtl_power opened the device fine but never delivered samples, so every
sweep burned its full timeout and died with "hung past its own -e bound".

The load-bearing detail: SIGKILL does not fix this.  The wedge lives in
the device/driver, not the process, so killing rtl_power frees the PID and
the very next sweep hangs identically.  That is how a single wedge turned
into hours of failed beacon passes -- every hourly run got through 2 of 12
references before its budget expired, and the other 10 aged past the
aggregator's 3 h staleness window.  A USBDEVFS_RESET cleared it instantly.

Requires write access to the device node.  The usual rtl-sdr udev rules
give the dongles to the `plugdev` group, so this works unprivileged for a
user in that group; otherwise it raises and the caller just logs it.
"""
import fcntl
import os
import sys

# linux/usbdevice_fs.h: _IO('U', 20)
USBDEVFS_RESET = (ord("U") << 8) | 20

SYSFS_USB = "/sys/bus/usb/devices"
RTL_VENDOR = "0bda"
RTL_PRODUCTS = {"2832", "2838"}


class ResetError(Exception):
    pass


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_rtl_nodes(sysfs=SYSFS_USB):
    """[(serial, node_path)] for every attached RTL-SDR.

    Read straight from sysfs rather than shelling out to rtl_test: this
    runs in the recovery path, where the whole premise is that talking to
    the device hangs.
    """
    nodes = []
    if not os.path.isdir(sysfs):
        return nodes
    for name in sorted(os.listdir(sysfs)):
        base = os.path.join(sysfs, name)
        if _read(os.path.join(base, "idVendor")) != RTL_VENDOR:
            continue
        if _read(os.path.join(base, "idProduct")) not in RTL_PRODUCTS:
            continue
        busnum = _read(os.path.join(base, "busnum"))
        devnum = _read(os.path.join(base, "devnum"))
        if not busnum or not devnum:
            continue
        try:
            node = "/dev/bus/usb/%03d/%03d" % (int(busnum), int(devnum))
        except ValueError:
            continue
        nodes.append((_read(os.path.join(base, "serial")) or "", node))
    return nodes


def reset_serial(serial, sysfs=SYSFS_USB):
    """USB port reset the dongle with this serial; returns the node path.

    The device re-enumerates afterwards, so its rtl_* index may change --
    callers must re-resolve by serial rather than reuse an old index.
    """
    if not sys.platform.startswith("linux"):
        raise ResetError("USB reset is Linux-only (USBDEVFS_RESET)")
    for found, node in find_rtl_nodes(sysfs):
        if found != serial:
            continue
        try:
            fd = os.open(node, os.O_WRONLY)
        except OSError as e:
            raise ResetError("cannot open %s: %s" % (node, e))
        try:
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        except OSError as e:
            raise ResetError("ioctl USBDEVFS_RESET on %s failed: %s" % (node, e))
        finally:
            os.close(fd)
        return node
    present = [s for s, _n in find_rtl_nodes(sysfs)]
    raise ResetError("no RTL-SDR with serial %r to reset (present: %s)"
                     % (serial, present))
