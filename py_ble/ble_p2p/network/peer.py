"""
network/peer.py — Peer model and in-memory registry.

PeerRegistry is the single source of truth for every remote device this node
has ever seen.  It is NOT persisted here — the SQLite store (storage/store.py)
handles long-term persistence, and the node reconciles the two on startup.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# How many seconds before a peer is considered "stale" (not seen recently)
PEER_STALE_TTL_S = 300.0


# ─────────────────────────────────────────────────────────────
# Peer dataclass
# ─────────────────────────────────────────────────────────────
@dataclass
class Peer:
    """
    Represents one remote BLE-P2P node.

    Fields
    ------
    device_id    : 8-byte stable identifier (from CHAR_INFO or HANDSHAKE)
    name         : human-readable display name
    capabilities : Capability bitmask
    ble_address  : platform BLE address (used by BleakClient)
    rssi         : most-recently observed signal strength (dBm)
    last_seen    : monotonic timestamp of last advertisement / connection
    last_connected : monotonic timestamp of last successful GATT session
    pending_connect: True when a connection attempt is in-flight
    connect_failures: consecutive failure count (used for backoff)
    """

    device_id        : bytes
    name             : str
    capabilities     : int
    ble_address      : str
    rssi             : int   = -127
    last_seen        : float = field(default_factory=time.monotonic)
    last_connected   : Optional[float] = None
    pending_connect  : bool  = False
    connect_failures : int   = 0
    _rssi_samples    : list  = field(default_factory=list, repr=False)

    # ── Helpers ──────────────────────────────────────────────

    @property
    def id_hex(self) -> str:
        return self.device_id.hex()

    @property
    def short_id(self) -> str:
        return self.id_hex[:8]

    @property
    def is_fresh(self) -> bool:
        return (time.monotonic() - self.last_seen) < PEER_STALE_TTL_S

    @property
    def is_connected(self) -> bool:
        # "connected" means a session completed in the last 30 s
        if self.last_connected is None:
            return False
        return (time.monotonic() - self.last_connected) < 30.0

    def record_rssi(self, rssi: int):
        """Maintain a rolling window of up to 8 RSSI samples."""
        self._rssi_samples.append(rssi)
        if len(self._rssi_samples) > 8:
            self._rssi_samples.pop(0)
        self.rssi       = rssi
        self.last_seen  = time.monotonic()

    @property
    def avg_rssi(self) -> float:
        if not self._rssi_samples:
            return float(self.rssi)
        return sum(self._rssi_samples) / len(self._rssi_samples)

    def estimate_distance_m(self, tx_power_dbm: int = -59) -> float:
        """
        Log-distance path-loss model — rough estimate only.
        Suitable for "near / medium / far" classification; not surveying.
        """
        r = self.avg_rssi
        if r == 0:
            return -1.0
        ratio = r / tx_power_dbm
        if ratio < 1.0:
            return ratio ** 10
        return 0.89976 * (ratio ** 7.7095) + 0.111

    def mark_connected(self):
        self.last_connected  = time.monotonic()
        self.connect_failures = 0
        self.pending_connect  = False

    def mark_failed(self):
        self.connect_failures += 1
        self.pending_connect   = False

    def __repr__(self) -> str:
        return (
            f"<Peer {self.name!r} id={self.short_id} "
            f"addr={self.ble_address} rssi={self.rssi} dBm>"
        )


# ─────────────────────────────────────────────────────────────
# In-memory Registry
# ─────────────────────────────────────────────────────────────
class PeerRegistry:
    """
    Thread-safe mapping from (device_id bytes) → Peer.

    Secondary index on ble_address allows fast lookup during scanning.
    """

    def __init__(self):
        self._by_id      : Dict[bytes, Peer]  = {}
        self._addr_to_id : Dict[str,   bytes] = {}
        self._lock       : Lock               = Lock()

    # ── Write ops ────────────────────────────────────────────

    def upsert(
        self,
        device_id   : bytes,
        name        : str,
        capabilities: int,
        ble_address : str,
        rssi        : int = -127,
    ) -> Peer:
        """Insert or update a peer.  Returns the (possibly new) Peer object."""
        with self._lock:
            existing = self._by_id.get(device_id)
            if existing:
                existing.name         = name
                existing.capabilities = capabilities
                existing.ble_address  = ble_address
                existing.record_rssi(rssi)
                self._addr_to_id[ble_address.upper()] = device_id
                log.debug("Updated peer %s (%s)", name, device_id.hex())
                return existing
            else:
                peer = Peer(
                    device_id=device_id, name=name,
                    capabilities=capabilities, ble_address=ble_address,
                    rssi=rssi,
                )
                self._by_id[device_id]              = peer
                self._addr_to_id[ble_address.upper()] = device_id
                log.info("Discovered new peer: %r", peer)
                return peer

    def update_rssi(self, ble_address: str, rssi: int):
        """Update RSSI only (called frequently from scanner)."""
        with self._lock:
            dev_id = self._addr_to_id.get(ble_address.upper())
            if dev_id and dev_id in self._by_id:
                self._by_id[dev_id].record_rssi(rssi)

    # ── Read ops ─────────────────────────────────────────────

    def by_id(self, device_id: bytes) -> Optional[Peer]:
        with self._lock:
            return self._by_id.get(device_id)

    def by_address(self, ble_address: str) -> Optional[Peer]:
        with self._lock:
            dev_id = self._addr_to_id.get(ble_address.upper())
            return self._by_id.get(dev_id) if dev_id else None

    def by_id_hex(self, hex_str: str) -> Optional[Peer]:
        try:
            return self.by_id(bytes.fromhex(hex_str))
        except ValueError:
            return None

    def all_peers(self) -> List[Peer]:
        with self._lock:
            return list(self._by_id.values())

    def fresh_peers(self) -> List[Peer]:
        with self._lock:
            return [p for p in self._by_id.values() if p.is_fresh]

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_id)