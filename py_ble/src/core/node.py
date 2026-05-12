"""
core/node.py
============
Peer-node data model and the in-memory routing / neighbour table.
"""

from __future__ import annotations
import time
import math
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Set


# ── Peer Node ─────────────────────────────────────────────────────────────────

@dataclass
class PeerNode:
    """Runtime state for a single known peer node."""

    addr:          bytes               # 6-byte BLE MAC / node ID
    name:          str       = ""      # human-readable name (from heartbeat)
    rssi:          int       = -100    # last observed RSSI (dBm)
    rssi_history:  List[int] = field(default_factory=list)
    public_key:    Optional[bytes] = None   # X25519 public key (32 bytes)
    last_seen:     float     = field(default_factory=time.monotonic)
    last_seq:      int       = 0
    hop_distance:  int       = 255     # ∞ by default; 1 = direct neighbour
    next_hop:      Optional[bytes] = None   # next-hop addr towards this peer
    groups:        Set[int]  = field(default_factory=set)
    features:      Set[str]  = field(default_factory=set)  # advertised capabilities
    rtt_ms:        float     = float("inf")
    tx_count:      int       = 0
    rx_count:      int       = 0
    lost_count:    int       = 0

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def addr_str(self) -> str:
        return ":".join(f"{b:02X}" for b in self.addr)

    @property
    def is_direct_neighbor(self) -> bool:
        return self.hop_distance == 1

    @property
    def is_alive(self, timeout: float = 30.0) -> bool:
        return (time.monotonic() - self.last_seen) < timeout

    @property
    def smoothed_rssi(self) -> float:
        """Exponential moving average of recent RSSI readings."""
        if not self.rssi_history:
            return float(self.rssi)
        alpha = 0.3
        ema   = float(self.rssi_history[0])
        for r in self.rssi_history[1:]:
            ema = alpha * r + (1 - alpha) * ema
        return round(ema, 1)

    @property
    def estimated_distance_m(self) -> Optional[float]:
        """
        Very rough distance estimate from RSSI using free-space path-loss.
        Reference TX power assumed -59 dBm @ 1 m (typical BLE).
        """
        rssi = self.smoothed_rssi
        if rssi == 0 or rssi < -100:
            return None
        TX_POWER = -59
        n        = 2.0   # path loss exponent (free space)
        return round(10 ** ((TX_POWER - rssi) / (10 * n)), 2)

    @property
    def packet_loss_rate(self) -> float:
        total = self.tx_count + self.lost_count
        return 0.0 if total == 0 else self.lost_count / total

    def update_rssi(self, rssi: int, window: int = 10):
        self.rssi = rssi
        self.rssi_history.append(rssi)
        if len(self.rssi_history) > window:
            self.rssi_history.pop(0)

    def touch(self):
        self.last_seen = time.monotonic()

    def to_dict(self) -> dict:
        return {
            "addr":         self.addr_str,
            "name":         self.name,
            "rssi":         self.rssi,
            "smoothed_rssi": self.smoothed_rssi,
            "distance_m":   self.estimated_distance_m,
            "hop_distance": self.hop_distance,
            "rtt_ms":       self.rtt_ms,
            "loss_rate":    self.packet_loss_rate,
            "groups":       list(self.groups),
            "features":     list(self.features),
            "is_alive":     self.is_alive,
        }


# ── Routing Table ─────────────────────────────────────────────────────────────

class RoutingTable:
    """
    Distance-vector routing table with decay-based metric.

    Metric: composite_cost = hop_distance + penalty(rssi) + penalty(loss)
    Routes are refreshed via heartbeats / route replies.
    """

    def __init__(self, stale_timeout: float = 60.0):
        self._routes: Dict[bytes, PeerNode] = {}
        self._stale_timeout = stale_timeout

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def upsert(
        self,
        addr:        bytes,
        rssi:        int         = -100,
        name:        str         = "",
        hop_distance: int        = 255,
        next_hop:    Optional[bytes] = None,
        public_key:  Optional[bytes] = None,
        groups:      Optional[Set[int]] = None,
        features:    Optional[Set[str]] = None,
        rtt_ms:      float       = float("inf"),
    ) -> PeerNode:
        if addr not in self._routes:
            self._routes[addr] = PeerNode(addr=addr)

        node = self._routes[addr]
        node.update_rssi(rssi)
        node.touch()
        if name:
            node.name = name
        if hop_distance < node.hop_distance:
            node.hop_distance = hop_distance
            node.next_hop     = next_hop
        if public_key:
            node.public_key = public_key
        if groups:
            node.groups.update(groups)
        if features:
            node.features.update(features)
        if rtt_ms < node.rtt_ms:
            node.rtt_ms = rtt_ms
        return node

    def get(self, addr: bytes) -> Optional[PeerNode]:
        return self._routes.get(addr)

    def remove(self, addr: bytes):
        self._routes.pop(addr, None)

    def all_nodes(self) -> List[PeerNode]:
        return list(self._routes.values())

    def neighbors(self) -> List[PeerNode]:
        return [n for n in self._routes.values() if n.is_direct_neighbor]

    def best_next_hop(self, dst: bytes) -> Optional[bytes]:
        """Return the best known next-hop MAC toward dst, or None."""
        node = self._routes.get(dst)
        if node is None:
            return None
        if node.is_direct_neighbor:
            return dst
        return node.next_hop

    def evict_stale(self):
        cutoff = time.monotonic() - self._stale_timeout
        stale  = [a for a, n in self._routes.items() if n.last_seen < cutoff]
        for a in stale:
            del self._routes[a]
        return stale

    def best_route_to(self, dst: bytes) -> Tuple[Optional[bytes], int]:
        """Returns (next_hop, cost) for the best path to dst."""
        node = self._routes.get(dst)
        if node is None:
            return None, 9999
        cost = node.hop_distance + self._rssi_penalty(node.smoothed_rssi)
        return (node.next_hop or dst), int(cost)

    @staticmethod
    def _rssi_penalty(rssi: float) -> int:
        """Convert RSSI to an additive routing penalty (0–5)."""
        if rssi >= -60:   return 0
        if rssi >= -70:   return 1
        if rssi >= -80:   return 2
        if rssi >= -90:   return 3
        return 5

    def summary(self) -> str:
        lines = ["Routing Table:"]
        for node in sorted(self._routes.values(), key=lambda n: n.hop_distance):
            nh = ":".join(f"{b:02X}" for b in node.next_hop) if node.next_hop else "direct"
            lines.append(
                f"  {node.addr_str:<20} hops={node.hop_distance} "
                f"rssi={node.rssi:>4}dBm  rtt={node.rtt_ms:>6.1f}ms  via={nh}"
            )
        return "\n".join(lines)


# ── Group Registry ────────────────────────────────────────────────────────────

@dataclass
class Group:
    group_id:  int
    name:      str
    members:   Set[bytes] = field(default_factory=set)
    created_at: float     = field(default_factory=time.monotonic)


class GroupRegistry:
    """Tracks all known multicast groups."""

    def __init__(self):
        self._groups: Dict[int, Group] = {}

    def create(self, group_id: int, name: str, creator: bytes) -> Group:
        g = Group(group_id=group_id, name=name, members={creator})
        self._groups[group_id] = g
        return g

    def join(self, group_id: int, member: bytes):
        if group_id in self._groups:
            self._groups[group_id].members.add(member)

    def leave(self, group_id: int, member: bytes):
        if group_id in self._groups:
            self._groups[group_id].members.discard(member)

    def get(self, group_id: int) -> Optional[Group]:
        return self._groups.get(group_id)

    def all_groups(self) -> List[Group]:
        return list(self._groups.values())

    def groups_for_member(self, member: bytes) -> List[Group]:
        return [g for g in self._groups.values() if member in g.members]