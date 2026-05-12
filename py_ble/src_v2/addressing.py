"""
addressing.py — BLE device addressing.
 
Short address (uint16):
    Derived from the last 2 bytes of the Bluetooth MAC address.
    Collision probability with ≤20 devices: ~0.02%.  Good enough for
    small mesh networks; replace with a coordinator-assigned scheme
    for larger deployments.
 
    Example: "AA:BB:CC:DD:EE:FF" → 0xEEFF
 
RoutingTable:
    Used by the MeshFeature.  Maps destination short-addresses to the
    BT MAC address of the next-hop peer (a directly connected neighbour).
    Updated whenever a mesh ROUTE_REPLY is received or a direct connection
    is established.
"""

import struct
import time

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from .constants import BROADCAST_ADDR


# --- Short address ----------------------------------------------------------

def short_addr_from_mac(mac: str) -> int:
    """
    Derive a uint16 short address from a BT MAC.

        "AA:BB:CC:DD:EE:FF"  →  0xEEFF
        "BC:03:58:30:D8:3C"  →  0xD83C
    """
    parts = mac.replace("-", ":").split(":")
    if len(parts) < 2:
        # MAC unavailable (can happen on some platforms) - use random
        import random
        return random.randint(0x0001, 0xFFFE)
    return (int(parts[-2], 16) << 8) | int(parts[-1], 16)

def mac_to_bytes(mac: str) -> bytes:
    """Convert MAC string to 6 raw bytes."""
    return bytes(int(b, 16) for b in mac.replace("-", ":").split(":"))

# --- Routing table entry -----------------------------------------------------

@dataclass
class RouteEntry:
    dst_short:      int     # destination short address
    next_hop_mac:   str     # BT MAC of the directly-connected next hop
    hops:           int     # distance to destination in hops
    updated_at:     float   = field(default_factory=time.monotonic)

    @property
    def age(self) -> float:
        return time.monotonic() - self.updated_at
    
    def is_stale(self, max_age: float = 120.0) -> bool:
        return self.age > max_age

# --- Routing table -----------------------------------------------------------

class RoutingTable:
    """
    Next-hop routing table for the mesh feature.

    Direct neighbours always have hops=1.
    Multi-hop routes are learned via ROUTE_REPLY messages.
    """

    def __init__(self):
        self._routes: dict[int, RouteEntry] = {}
    
    def add_neighbour(self, short_addr: int, mac: str) -> None:
        """Register a directly connected peer."""
        self.update(short_addr, mac, hops=1)
    
    def remove_neighbour(self, mac: str) -> None:
        """Remove all routes through a peer that has disconnected."""
        self._routes = {
            k: v for k, v in self._routes.items()
            if v.next_hop_mac.lower() != mac.lower()
        }
    
    def update(self, dst: int, next_hop_mac: str, hops: int) -> None:
        existing = self._routes.get(dst)
        if existing is None or hops < existing.hops or existing.is_stale():
            self._routes[dst] = RouteEntry(dst, next_hop_mac, hops)
    
    def next_hop(self, dst: int) -> Optional[str]:
        """
        Return the MAC of the next-hop peer for `dst`, or None if unknown.
        Broadcast is a special case - caller should fan-out to all neighbours.
        """
        if dst == BROADCAST_ADDR:
            return None     # caller handles broadcast
        entry = self._routes.get(dst)
        if entry and not entry.is_stale():
            return entry.next_hop_mac
        return None
    
    def all_neighbours(self) -> list[str]:
        """Return MACs of all directly connected peers (hops==1, not stale)."""
        return [
            r.next_hop_mac
            for r in self._routes.values()
            if r.hops == 1 and not r.is_stale()
        ]
    
    def dump(self) -> list[dict]:
        return [
            {
                "dst": f"0x{r.dst_short:04X}",
                "next_hop": r.next_hop_mac,
                "hops": r.hops,
                "age_s": f"{r.age:.1f}"
            }
            for r in sorted(self._routes.values(), ley=lambda x: x.hops)
        ]