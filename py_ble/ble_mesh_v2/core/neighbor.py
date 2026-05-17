"""
core/neighbor.py — In-memory registry of directly-reachable BLE peers.

Each :class:`Neighbor` tracks connection state, RSSI (exponential moving
average), and the bleak ``BleakClient`` handle so the transport layer can
write to the peer's RX characteristic.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Neighbor:
    """A single directly-reachable BLE peer.

    Parameters
    ----------
    node_id : bytes
        The peer's 16-byte mesh node identifier (read from INFO_CHAR).
    address : str
        The BLE MAC address (Linux/Windows) or Core Bluetooth UUID (macOS).
    name : str
        Human-readable name read from the BLE advertisement.
    rssi : int
        Current smoothed RSSI in dBm (higher = stronger).
    """

    node_id:    bytes
    address:    str
    name:       str  = "Unknown"
    rssi:       int  = -100
    last_seen:  float = field(default_factory=time.monotonic)
    is_connected: bool = False
    # bleak BleakClient — None when not connected as central
    client: Optional[Any] = field(default=None, repr=False, compare=False)
    # Arbitrary metadata (e.g. feature-layer annotations)
    metadata: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    _ALPHA: float = 0.3   # EMA smoothing factor for RSSI

    def update(self, rssi: int, name: Optional[str] = None) -> None:
        """Update RSSI (EMA) and bump *last_seen* timestamp."""
        self.rssi = int(self._ALPHA * rssi + (1 - self._ALPHA) * self.rssi)
        self.last_seen = time.monotonic()
        if name:
            self.name = name

    @property
    def age(self) -> float:
        """Seconds since the neighbor was last heard from."""
        return time.monotonic() - self.last_seen

    @property
    def is_stale(self) -> bool:
        return self.age > 120.0

    def to_dict(self) -> dict:
        return {
            "node_id":   self.node_id.hex(),
            "address":   self.address,
            "name":      self.name,
            "rssi":      self.rssi,
            "connected": self.is_connected,
            "age_s":     round(self.age, 1),
        }


class NeighborTable:
    """Thread-safe (within a single asyncio event loop) neighbor registry.

    Indexed by both *node_id* and BLE *address* for O(1) lookup either way.
    """

    def __init__(self) -> None:
        self._by_id:   Dict[bytes, Neighbor] = {}
        self._by_addr: Dict[str, bytes]       = {}

    # ── Mutations ─────────────────────────────────────────────────────────────

    def upsert(self, node_id: bytes, address: str, **kwargs) -> Neighbor:
        """Insert or update a neighbor record.  Returns the (possibly new) entry."""
        if node_id in self._by_id:
            n = self._by_id[node_id]
            rssi = kwargs.pop("rssi", None)
            name = kwargs.pop("name", None)
            n.update(rssi, name) if rssi is not None else None
            for k, v in kwargs.items():
                if hasattr(n, k):
                    setattr(n, k, v)
        else:
            n = Neighbor(node_id=node_id, address=address, **kwargs)
            self._by_id[node_id]    = n
            self._by_addr[address]  = node_id
        return n

    def remove(self, node_id: bytes) -> Optional[Neighbor]:
        n = self._by_id.pop(node_id, None)
        if n:
            self._by_addr.pop(n.address, None)
        return n

    def prune_stale(self) -> List[Neighbor]:
        """Remove neighbors not seen for > 120 s.  Returns removed entries."""
        stale = [n for n in self._by_id.values() if n.is_stale]
        for n in stale:
            self.remove(n.node_id)
        return stale

    # ── Lookups ───────────────────────────────────────────────────────────────

    def get(self, node_id: bytes) -> Optional[Neighbor]:
        return self._by_id.get(node_id)

    def get_by_address(self, address: str) -> Optional[Neighbor]:
        nid = self._by_addr.get(address)
        return self._by_id.get(nid) if nid else None

    def all(self) -> List[Neighbor]:
        return list(self._by_id.values())

    def connected(self) -> List[Neighbor]:
        return [n for n in self._by_id.values() if n.is_connected]

    def best_signal(self) -> Optional[Neighbor]:
        cs = self.connected()
        return max(cs, key=lambda n: n.rssi) if cs else None

    # ── Dunder ────────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._by_id)

    def __contains__(self, node_id: bytes) -> bool:
        return node_id in self._by_id

    def __iter__(self):
        return iter(self._by_id.values())
