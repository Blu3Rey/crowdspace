"""
device.py — Persistent local-device identity.

On first run a random UUID is generated and serialised to ~/.ble_p2p/device.json.
Subsequent runs load the same identity so a device is consistently recognisable
by its peers across restarts.

The 8-byte device_id is the first 8 bytes of the UUID (no dashes), giving
2^64 unique addresses — more than sufficient for a local mesh.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from .constants import CONFIG_DIR, Capability

log = logging.getLogger(__name__)

_CONFIG_FILE = CONFIG_DIR / "device.json"


class LocalDevice:
    """
    Represents this node's persistent identity.

    Attributes
    ----------
    device_id    : 8-byte unique identifier (stable across restarts)
    device_uuid  : full 128-bit UUID string (for display / debugging)
    name         : human-readable name (e.g. "AlicePhone")
    capabilities : Capability bitmask advertising what this node can do
    """

    def __init__(
        self,
        name         : Optional[str] = None,
        capabilities : int            = 0,
    ):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._load_or_create(name, capabilities)

    # ── Initialisation ────────────────────────────────────────

    def _load_or_create(self, name: Optional[str], capabilities: int):
        if _CONFIG_FILE.exists():
            self._load(name, capabilities)
        else:
            self._create(name, capabilities)

    def _load(self, name_override: Optional[str], cap_override: int):
        try:
            with _CONFIG_FILE.open() as fh:
                data = json.load(fh)
            self.device_uuid  = data["device_uuid"]
            self.device_id    = bytes.fromhex(data["device_id"])
            self.name         = name_override or data["name"]
            self.capabilities = cap_override or data.get("capabilities", self._default_caps())
            log.info("Loaded device identity: %s (%s)", self.name, self.id_hex)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            log.warning("Corrupt device.json (%s); regenerating.", exc)
            _CONFIG_FILE.unlink(missing_ok=True)
            self._create(name_override, cap_override)

    def _create(self, name: Optional[str], capabilities: int):
        self.device_uuid  = str(uuid.uuid4())
        # Derive an 8-byte ID from the UUID bytes
        raw               = uuid.UUID(self.device_uuid).bytes
        self.device_id    = raw[:8]
        self.name         = name or f"Node-{self.id_hex[:6].upper()}"
        self.capabilities = capabilities or self._default_caps()
        self._save()
        log.info("Created new device identity: %s (%s)", self.name, self.id_hex)

    def _save(self):
        with _CONFIG_FILE.open("w") as fh:
            json.dump(
                {
                    "device_uuid" : self.device_uuid,
                    "device_id"   : self.device_id.hex(),
                    "name"        : self.name,
                    "capabilities": int(self.capabilities),
                },
                fh,
                indent=2,
            )

    @staticmethod
    def _default_caps() -> int:
        return int(
            Capability.RELAY
            | Capability.STORE_FORWARD
            | Capability.GROUP_CHAT
            | Capability.LOCATOR
        )

    # ── Properties ───────────────────────────────────────────

    @property
    def id_hex(self) -> str:
        """Hex string of the 8-byte device_id (16 hex chars)."""
        return self.device_id.hex()

    @property
    def short_id(self) -> str:
        """First 8 hex chars — handy for display."""
        return self.id_hex[:8]

    def update_name(self, new_name: str):
        self.name = new_name
        self._save()

    # ── Serialisation for handshake ───────────────────────────

    def info_payload(self) -> bytes:
        """
        JSON payload sent in HANDSHAKE messages so peers learn our
        device_id, display name, and capability set.
        """
        return json.dumps(
            {
                "id"  : self.id_hex,
                "name": self.name,
                "caps": int(self.capabilities),
                "ver" : 1,
            },
            separators=(",", ":"),
        ).encode()

    def __repr__(self) -> str:
        return f"<LocalDevice name={self.name!r} id={self.id_hex}>"