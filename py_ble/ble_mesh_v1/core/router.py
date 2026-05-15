"""
core/router.py — Routing table and duplicate-packet cache.

Routing strategy
----------------
The default strategy is *TTL-bounded flooding* with a routing-table overlay.

* Every packet received is first checked against the :class:`DedupCache`.
  Known (src_id, seq_num) pairs are dropped immediately.
* For *unicast* packets the router first tries to find a known next-hop in
  the :class:`RoutingTable`.  If one exists the packet is forwarded only on
  that interface; otherwise it floods to all neighbours (controlled by TTL).
* *Broadcast* packets always flood.
* :class:`RouteEntry` uses a composite metric (hop count + RSSI penalty) so
  the mesh naturally prefers strong-signal, short paths.

This design keeps the implementation simple while remaining extensible —
a future proactive routing protocol (e.g. OLSR or DSDV) can populate the
routing table independently without touching any other component.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .packet import Packet


# ── Route entry ───────────────────────────────────────────────────────────────

@dataclass
class RouteEntry:
    """One row in the routing table.

    Attributes
    ----------
    dst_id : bytes
        16-byte destination node identifier.
    next_hop_id : bytes
        16-byte identifier of the *neighbour* to forward through.
    next_hop_addr : str
        BLE address of the next-hop neighbour (used by the transport layer).
    hop_count : int
        Number of hops to *dst_id* via this route.
    rssi : int
        RSSI of the link to *next_hop* (dBm, negative).
    last_updated : float
        ``time.monotonic()`` timestamp of the last update.
    """

    dst_id:        bytes
    next_hop_id:   bytes
    next_hop_addr: str
    hop_count:     int   = 1
    rssi:          int   = -100
    last_updated:  float = field(default_factory=time.monotonic)

    _TIMEOUT: float = 120.0  # seconds until this route is considered stale

    @property
    def is_expired(self) -> bool:
        return time.monotonic() - self.last_updated > self._TIMEOUT

    @property
    def metric(self) -> float:
        """Lower is better.  Penalises weak links on top of hop count."""
        rssi_penalty = max(0.0, (-self.rssi - 50) / 10.0)
        return self.hop_count + rssi_penalty

    def touch(self) -> None:
        self.last_updated = time.monotonic()


# ── Routing table ─────────────────────────────────────────────────────────────

class RoutingTable:
    """Maps destination node IDs to their best-known next-hop.

    Only one entry per destination is kept; a new entry replaces the old one
    only when its *metric* is strictly better (lower).
    """

    def __init__(self) -> None:
        self._routes: Dict[bytes, RouteEntry] = {}

    def update(
        self,
        dst_id:        bytes,
        next_hop_id:   bytes,
        next_hop_addr: str,
        hop_count:     int = 1,
        rssi:          int = -100,
    ) -> bool:
        """Propose a route.  Returns True if the table was updated."""
        candidate = RouteEntry(dst_id, next_hop_id, next_hop_addr, hop_count, rssi)
        existing  = self._routes.get(dst_id)
        if existing is None or candidate.metric < existing.metric:
            self._routes[dst_id] = candidate
            return True
        existing.touch()
        return False

    def lookup(self, dst_id: bytes) -> Optional[RouteEntry]:
        """Return the best known route to *dst_id*, or ``None``."""
        entry = self._routes.get(dst_id)
        if entry is None:
            return None
        if entry.is_expired:
            del self._routes[dst_id]
            return None
        return entry

    def invalidate(self, next_hop_id: bytes) -> List[bytes]:
        """Remove all routes whose next-hop is *next_hop_id* (link went down).
        Returns list of invalidated dst_ids."""
        dead = [dst for dst, e in self._routes.items() if e.next_hop_id == next_hop_id]
        for dst in dead:
            del self._routes[dst]
        return dead

    def remove(self, dst_id: bytes) -> None:
        self._routes.pop(dst_id, None)

    def prune_expired(self) -> int:
        expired = [k for k, v in self._routes.items() if v.is_expired]
        for k in expired:
            del self._routes[k]
        return len(expired)

    def all_routes(self) -> List[RouteEntry]:
        return list(self._routes.values())

    def __len__(self) -> int:
        return len(self._routes)


# ── Dedup cache ───────────────────────────────────────────────────────────────

class DedupCache:
    """LRU cache of (src_id, seq_num) pairs seen recently.

    Prevents packets from being processed or re-forwarded more than once,
    which is essential to stop broadcast storms in a flooded mesh.

    The cache is bounded at *max_size* entries.  When full, the oldest entry
    is evicted (LRU policy via :class:`collections.OrderedDict`).
    """

    def __init__(self, max_size: int = 512) -> None:
        self._seen: OrderedDict[Tuple[bytes, int], float] = OrderedDict()
        self._max  = max_size

    def is_duplicate(self, packet: Packet) -> bool:
        """Return True (and mark as seen) if we have seen this packet before."""
        key = packet.dedup_key
        if key in self._seen:
            self._seen.move_to_end(key)   # refresh LRU position
            return True
        self._seen[key] = time.monotonic()
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)
        return False

    def clear(self) -> None:
        self._seen.clear()

    def __len__(self) -> int:
        return len(self._seen)