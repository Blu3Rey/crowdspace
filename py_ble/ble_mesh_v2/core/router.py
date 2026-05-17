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
    """Maps destination node IDs to their best-known next-hop(s).

    Up to *_MAX_PER_DST* routes are kept per destination, sorted by metric
    (lower = better).  The second entry serves as a hot-standby backup: if
    the primary link disappears, :meth:`lookup` automatically falls through to
    it without waiting for the routing table to rebuild.

    Unlike the previous single-entry design, updates from the **same next-hop**
    always replace the existing entry — even if the new metric is worse.  This
    means link degradation (falling RSSI) is tracked accurately.  Competition
    between *different* next-hops still uses metric comparison.
    """

    _MAX_PER_DST = 2   # primary + one hot-standby backup

    def __init__(self) -> None:
        # dst_id → list of RouteEntry, sorted best-first (lowest metric)
        self._routes: Dict[bytes, List[RouteEntry]] = {}

    def update(
        self,
        dst_id:        bytes,
        next_hop_id:   bytes,
        next_hop_addr: str,
        hop_count:     int = 1,
        rssi:          int = -100,
    ) -> bool:
        """Propose or refresh a route.  Returns True if the table changed."""
        candidate = RouteEntry(dst_id, next_hop_id, next_hop_addr, hop_count, rssi)
        entries   = self._routes.get(dst_id, [])

        # If we already have an entry via this exact next-hop, always update it
        # (captures degradation as well as improvement).
        for i, e in enumerate(entries):
            if e.next_hop_id == next_hop_id:
                entries[i] = candidate
                entries.sort(key=lambda x: x.metric)
                self._routes[dst_id] = entries
                return True

        # New next-hop: insert and keep the best _MAX_PER_DST entries.
        entries.append(candidate)
        entries.sort(key=lambda x: x.metric)
        self._routes[dst_id] = entries[: self._MAX_PER_DST]
        return True

    def lookup(self, dst_id: bytes) -> Optional[RouteEntry]:
        """Return the best non-expired route to *dst_id*, or ``None``.

        Automatically falls through to the backup route if the primary has
        expired, and cleans up fully-expired destinations.
        """
        entries = self._routes.get(dst_id)
        if not entries:
            return None

        live = [e for e in entries if not e.is_expired]
        if len(live) != len(entries):
            # Prune expired entries in place
            if live:
                self._routes[dst_id] = live
            else:
                del self._routes[dst_id]
                return None

        return live[0]   # best (lowest metric) surviving entry

    def invalidate(self, next_hop_id: bytes) -> List[bytes]:
        """Remove all routes whose next-hop is *next_hop_id* (link went down).
        Returns list of invalidated dst_ids."""
        dead_dsts = []
        for dst, entries in list(self._routes.items()):
            remaining = [e for e in entries if e.next_hop_id != next_hop_id]
            if len(remaining) != len(entries):
                if remaining:
                    self._routes[dst] = remaining
                else:
                    del self._routes[dst]
                    dead_dsts.append(dst)
        return dead_dsts

    def remove(self, dst_id: bytes) -> None:
        self._routes.pop(dst_id, None)

    def prune_expired(self) -> int:
        """Evict expired entries.  Returns number of *destinations* fully removed."""
        removed = 0
        for dst in list(self._routes.keys()):
            live = [e for e in self._routes[dst] if not e.is_expired]
            if not live:
                del self._routes[dst]
                removed += 1
            elif len(live) != len(self._routes[dst]):
                self._routes[dst] = live
        return removed

    def all_routes(self) -> List[RouteEntry]:
        """Return the primary (best) route for every known destination."""
        return [entries[0] for entries in self._routes.values() if entries]

    def all_routes_with_backups(self) -> Dict[bytes, List[RouteEntry]]:
        """Return the full route list (primary + backups) for every destination."""
        return {dst: list(entries) for dst, entries in self._routes.items()}

    def __len__(self) -> int:
        return len(self._routes)


# ── Dedup cache ───────────────────────────────────────────────────────────────

class DedupCache:
    """LRU cache of (src_id, seq_num) pairs seen recently, with replay protection.

    Two-layer defence against duplicate and replayed packets:

    1. **LRU window** — the most recent *max_size* ``(src_id, seq_num)`` pairs
       are cached.  Packets seen within this window are dropped immediately.

    2. **Per-source high-water mark** — we track the highest ``seq_num`` ever
       seen from each source.  Once a ``(src, seq)`` pair falls out of the LRU
       window an attacker could replay it; the high-water mark catches this by
       rejecting any packet whose ``seq_num`` is strictly behind the running
       maximum for that source (accounting for 32-bit wrap-around).

    This makes replay attacks require forging a *future* sequence number, which
    is not feasible without breaking the encryption layer.
    """

    def __init__(self, max_size: int = 512) -> None:
        self._seen:    OrderedDict[Tuple[bytes, int], float] = OrderedDict()
        self._max      = max_size
        # Per-source highest seq_num seen (replay guard)
        self._max_seq: Dict[bytes, int] = {}

    def is_duplicate(self, packet: Packet) -> bool:
        """Return True (and record) if this packet has been seen before or is a replay."""
        key = packet.dedup_key          # (src_id, seq_num)
        src_id, seq_num = key

        # Layer 1: LRU window check
        if key in self._seen:
            self._seen.move_to_end(key)
            return True

        # Layer 2: Replay guard.
        # Reject seq_num strictly behind the high-water mark, while correctly
        # recognising genuine 32-bit wrap-around (0xFFFFFFFF → 0 → 1 …).
        # Strategy: if seq_num < max_seen AND (max_seen - seq_num) ≤ 2^31,
        # the packet is genuinely in the past → replay.  If the gap is > 2^31
        # the sequence number has wrapped around and the packet is new.
        max_seen = self._max_seq.get(src_id, -1)
        if max_seen >= 0 and seq_num < max_seen:
            if (max_seen - seq_num) <= 0x7FFFFFFF:
                return True   # genuine replay — gap is small enough to be sure

        # Record as seen and update high-water mark
        self._seen[key] = time.monotonic()
        if len(self._seen) > self._max:
            self._seen.popitem(last=False)

        # Advance high-water mark only forward (accounting for wrap-around)
        if max_seen < 0 or seq_num > max_seen or (max_seen - seq_num) > 0x7FFFFFFF:
            self._max_seq[src_id] = seq_num

        return False

    def clear(self) -> None:
        self._seen.clear()
        self._max_seq.clear()

    def __len__(self) -> int:
        return len(self._seen)
