"""Location providers: static config, gpsd, raw NMEA serial."""
import json
import socket
import time


class Fix:
    __slots__ = ("lat", "lon", "alt_m", "quality", "source")

    def __init__(self, lat=None, lon=None, alt_m=None, quality="none", source="static"):
        self.lat, self.lon, self.alt_m = lat, lon, alt_m
        self.quality, self.source = quality, source

    def as_tuple(self):
        return (self.lat, self.lon, self.alt_m, self.quality)


class StaticLocation:
    def __init__(self, cfg):
        self.fix = Fix(cfg.get("lat"), cfg.get("lon"), cfg.get("alt_m"),
                       "static" if cfg.get("lat") is not None else "none", "static")

    def get(self):
        return self.fix


class GpsdLocation:
    """Minimal gpsd JSON client (no external deps)."""

    def __init__(self, cfg):
        self.host = cfg.get("host", "127.0.0.1")
        self.port = int(cfg.get("port", 2947))
        self._last = Fix(source="gpsd")
        self._sock = None

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=5)
        s.sendall(b'?WATCH={"enable":true,"json":true}\n')
        s.settimeout(3)
        self._sock = s

    def get(self):
        try:
            if self._sock is None:
                self._connect()
            buf = b""
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    self._sock = None
                    break
                buf += chunk
                for line in buf.split(b"\n"):
                    try:
                        msg = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if msg.get("class") == "TPV" and msg.get("mode", 0) >= 2:
                        self._last = Fix(
                            msg.get("lat"), msg.get("lon"), msg.get("alt"),
                            f"{msg['mode']}D", "gpsd")
        except OSError:
            self._sock = None
        return self._last


def make_location(cfg):
    loc = cfg.get("location", {})
    mode = loc.get("mode", "static")
    if mode == "gpsd":
        return GpsdLocation(loc)
    return StaticLocation(loc)
