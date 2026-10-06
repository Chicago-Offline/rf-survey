"""USB reset recovery for wedged dongles (meshpi 2026-10-06 incident)."""
import subprocess
import sys

import pytest

from rfsurvey import sweep, usbreset


def _mkdev(root, name, vendor, product, busnum, devnum, serial=None):
    d = root / name
    d.mkdir()
    (d / "idVendor").write_text(vendor + "\n")
    (d / "idProduct").write_text(product + "\n")
    (d / "busnum").write_text(busnum + "\n")
    (d / "devnum").write_text(devnum + "\n")
    if serial is not None:
        (d / "serial").write_text(serial + "\n")
    return d


def test_find_rtl_nodes_selects_only_rtl_and_pads_node_path(tmp_path):
    _mkdev(tmp_path, "3-1", "0bda", "2838", "3", "3", "SONDE")
    _mkdev(tmp_path, "3-2", "0bda", "2832", "3", "4", "BENCH")
    _mkdev(tmp_path, "1-1", "1d6b", "0002", "1", "1", "roothub")

    found = usbreset.find_rtl_nodes(str(tmp_path))

    assert found == [
        ("SONDE", "/dev/bus/usb/003/003"),
        ("BENCH", "/dev/bus/usb/003/004"),
    ]


def test_find_rtl_nodes_tolerates_missing_sysfs_and_serial(tmp_path):
    assert usbreset.find_rtl_nodes(str(tmp_path / "nope")) == []
    _mkdev(tmp_path, "3-1", "0bda", "2838", "3", "3", serial=None)
    assert usbreset.find_rtl_nodes(str(tmp_path)) == [("", "/dev/bus/usb/003/003")]


def test_reset_serial_reports_present_serials_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _mkdev(tmp_path, "3-1", "0bda", "2838", "3", "3", "SONDE")

    with pytest.raises(usbreset.ResetError) as e:
        usbreset.reset_serial("ADSB", str(tmp_path))

    assert "ADSB" in str(e.value)
    assert "SONDE" in str(e.value)


def test_reset_serial_refuses_on_non_linux(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(usbreset.ResetError, match="Linux-only"):
        usbreset.reset_serial("SONDE", str(tmp_path))


def test_recover_wedged_resets_the_serial_behind_the_index(monkeypatch):
    monkeypatch.setattr("rfsurvey.devices.list_devices",
                        lambda: [{"index": 0, "serial": "BENCH"},
                                 {"index": 1, "serial": "SONDE"}])
    seen = {}

    def fake_reset(serial):
        seen["serial"] = serial
        return "/dev/bus/usb/003/003"

    monkeypatch.setattr("rfsurvey.usbreset.reset_serial", fake_reset)

    note = sweep._recover_wedged(1)

    assert seen["serial"] == "SONDE"
    assert "USB-reset" in note and "SONDE" in note


def test_recover_wedged_never_raises_when_enumeration_fails(monkeypatch):
    def boom():
        raise RuntimeError("rtl_test hung")

    monkeypatch.setattr("rfsurvey.devices.list_devices", boom)

    note = sweep._recover_wedged(1)

    assert "could not identify device 1" in note
    assert "rtl_test hung" in note


def test_recover_wedged_reports_a_failed_reset(monkeypatch):
    monkeypatch.setattr("rfsurvey.devices.list_devices",
                        lambda: [{"index": 1, "serial": "SONDE"}])

    def boom(serial):
        raise usbreset.ResetError("permission denied")

    monkeypatch.setattr("rfsurvey.usbreset.reset_serial", boom)

    note = sweep._recover_wedged(1)

    assert "USB reset of 'SONDE' failed" in note
    assert "permission denied" in note


def test_hung_sweep_triggers_recovery_and_says_so(monkeypatch):
    """A hung rtl_power must reset the device, not just report the hang."""
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="rtl_power", timeout=kw["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sweep, "_recover_wedged", lambda i: "; USB-reset 'SONDE'")

    with pytest.raises(sweep.SweepError) as e:
        sweep.run_rtl_power(1, 162.3, 162.8, 2, 8)

    assert "hung past its own -e bound" in str(e.value)
    assert "USB-reset 'SONDE'" in str(e.value)


def test_sweep_timeout_pad_leaves_headroom_over_the_e_bound():
    """Must exceed rtl_power's own -e deadline (integration + 30), or every
    healthy sweep would be killed as hung."""
    assert sweep.TIMEOUT_PAD_S > 30
    # ...but stay far enough under a 300 s beacon budget that a wedged
    # dongle cannot eat the whole pass before recovery kicks in.
    assert sweep.TIMEOUT_PAD_S < 120
