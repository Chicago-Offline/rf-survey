"""Station identity: Ed25519 keypair + batch signing (NETWORK.md M7).

A station is one rf-survey install with a stable station_id and a keypair.
The aggregator holds a registry of station public keys (manual enrollment,
small trusted set). Requires the 'submit' extra; the rest of rf-survey
works without it.
"""
import base64
import json
import os

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey, Ed25519PublicKey)
    from cryptography.hazmat.primitives import serialization
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

DEFAULT_KEY = "~/.config/rf-survey/station.key"


class StationError(Exception):
    pass


def _require_crypto():
    if not HAVE_CRYPTO:
        raise StationError(
            "cryptography not installed — "
            "pipx inject rf-survey cryptography  "
            "(or reinstall: pipx install 'rf-survey[submit]')")


def canonical(obj):
    """Canonical JSON bytes for signing: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


class Station:
    def __init__(self, station_id, private_key):
        self.station_id = station_id
        self._key = private_key

    @classmethod
    def load(cls, cfg):
        """From the site config's station: section. Key must already exist."""
        _require_crypto()
        st = cfg.get("station") or {}
        sid = st.get("id")
        if not sid:
            raise StationError("config missing station.id")
        key_path = os.path.expanduser(st.get("key", DEFAULT_KEY))
        if not os.path.exists(key_path):
            raise StationError(f"no key at {key_path} — run: survey station-init")
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise StationError(f"{key_path} is not an Ed25519 private key")
        return cls(sid, key)

    @classmethod
    def create(cls, station_id, key_path=DEFAULT_KEY):
        """Generate a new keypair. Refuses to overwrite an existing key."""
        _require_crypto()
        key_path = os.path.expanduser(key_path)
        if os.path.exists(key_path):
            raise StationError(f"key already exists: {key_path}")
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption())
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        return cls(station_id, key)

    @property
    def public_key_b64(self):
        raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def sign(self, batch):
        """Wrap a batch dict in a signed envelope."""
        sig = self._key.sign(canonical(batch))
        return {"batch": batch,
                "sig": base64.b64encode(sig).decode(),
                "station_id": self.station_id}


def verify(envelope, pubkey_b64):
    """Aggregator-side check: True iff sig matches the batch content."""
    _require_crypto()
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
    try:
        pub.verify(base64.b64decode(envelope["sig"]),
                   canonical(envelope["batch"]))
        return True
    except Exception:
        return False
