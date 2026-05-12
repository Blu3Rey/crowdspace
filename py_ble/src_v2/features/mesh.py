"""
features/mesh.py — Flooding mesh network.

Architecture:
    Every device is simultaneously a peripheral (server) and may connect to
    multiple centrals, forming an ad-hoc graph.  The MeshFeature implements
    controlled flooding:

        • When a MESH_DATA packet arrives destined for us → deliver to app.
        • When a MESH_DATA packet arrives NOT for us → decrement TTL and
          re-broadcast to all other connected peers (excluding the sender).
        • A (src_addr, seq) seen-cache prevents forwarding the same packet twice.

    ROUTE_ANNOUNCE (periodic):
        Each node broadcasts its short address and connected neighbours.
        Receivers build a RoutingTable for unicast routing.

Topology build-up:
    1.  All devices advertise as peripherals (normal behaviour).
    2.  A "coordinator" device (or any device with the mesh feature active)
        scans and connects to nearby peripherals, acting as a star-hub.
    3.  Each additional connection extends the mesh graph.

Unicast vs. flood:
    If RoutingTable has a route: unicast along the known path.
    If not: broadcast with TTL=DEFAULT_TTL (flood until discovered).

Extending:
    Replace the flood with AODV, DSDV, or Babel by overriding
    _forward() and adding ROUTE_REQUEST / ROUTE_REPLY message types.
    The rest of the stack is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import OrderedDict
from typing import Optional, TYPE_CHECKING

from ..addressing import RoutingTable, short_addr_from_mac
from ..constants import (
    BROADCAST_ADDR, DEFAULT_TTL, LOOPBACK_ADDR, MESH_SEEN_CACHE
)
from ..events import EventBus, MESH_FORWARDED, PEER_CONNECTED, PEER_DISCONNECTED
from ..protocol import FeatureID, Message, PacketFlags, build_packets
from . import FeatureBase

if TYPE_CHECKING:
    from ..connection.manager import ConnectionManager
    from ..connection.peer import Peer

log = logging.getLogger(__name__)


class MeshMsg:
    DATA             = 0x01   # application payload routed through mesh
    ROUTE_ANNOUNCE   = 0x02   # "I exist at this address, my neighbours are …"
    ROUTE_REQUEST    = 0x03   # AODV-style route discovery (extensible)
    ROUTE_REPLY      = 0x04


# ── Seen-packet cache ──────────────────────────────────────────────────────────

class SeenCache:
    """LRU cache of (src_addr, seq) pairs to suppress duplicate forwards."""

    def __init__(self, maxsize: int = MESH_SEEN_CACHE):
        self._data: OrderedDict[tuple, float] = OrderedDict()
        self._max  = maxsize

    def seen(self, src: int, seq: int) -> bool:
        key = (src, seq)
        if key in self._data:
            self._data.move_to_end(key)
            return True
        self._data[key] = time.monotonic()
        if len(self._data) > self._max:
            self._data.popitem(last=False)
        return False


# ── Mesh feature ──────────────────────────────────────────────────────────────

class MeshFeature(FeatureBase):
    """
    Flood-mesh with TTL-based loop prevention and optional unicast routing.

    After registering, call send_mesh() to inject an application payload
    into the mesh.  Subscribe to the "mesh.data" EventBus event to receive
    payloads from remote nodes.
    """

    FEATURE_ID = FeatureID.MESH

    def __init__(self, bus: EventBus, conn: "ConnectionManager", my_mac: str):
        super().__init__(bus, conn)
        self._my_mac     = my_mac
        self._my_addr    = short_addr_from_mac(my_mac)
        self._routing    = RoutingTable()
        self._seen       = SeenCache()
        self._seq        = 0   # global outbound sequence counter

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await super().start()
        self.bus.on(PEER_CONNECTED,    self._on_peer_up)
        self.bus.on(PEER_DISCONNECTED, self._on_peer_down)
        asyncio.create_task(self._announce_loop(), name="mesh_announce")

    async def _on_peer_up(self, peer: "Peer", **_) -> None:
        self._routing.add_neighbour(peer.short_addr, peer.mac)

    async def _on_peer_down(self, peer: "Peer", **_) -> None:
        self._routing.remove_neighbour(peer.mac)

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send_mesh(
        self,
        payload:  bytes | str,
        dst_addr: int = BROADCAST_ADDR,
    ) -> None:
        """
        Inject a payload into the mesh network.
        dst_addr=BROADCAST_ADDR → flood to all nodes.
        dst_addr=<short_addr>   → route to that node (flood if no route known).
        """
        self._seq = (self._seq + 1) % 65536
        seq       = self._seq

        # Build mesh envelope: [seq:uint16][dst:uint16][src:uint16][payload]
        envelope = struct.pack(">HHH", seq, dst_addr, self._my_addr) + (
            payload.encode() if isinstance(payload, str) else payload
        )

        routing_flags = PacketFlags.HAS_ROUTING
        packets = build_packets(
            FeatureID.MESH, MeshMsg.DATA, seq & 0xFF, envelope,
            flags    = routing_flags,
            src_addr = self._my_addr,
            dst_addr = dst_addr,
            ttl      = DEFAULT_TTL,
        )

        # Unicast if we have a route; otherwise flood
        next_hop = self._routing.next_hop(dst_addr) if dst_addr != BROADCAST_ADDR else None
        if next_hop:
            await self.conn.send_to(next_hop, packets)
        else:
            await self.conn.broadcast(packets)

    async def _announce_loop(self) -> None:
        """Periodically broadcast our address and neighbours."""
        while self._running:
            await asyncio.sleep(30.0)
            neighbours = self._routing.all_neighbours()
            # Payload: [my_addr:uint16][n_neighbours:uint8][addr0:uint16 ...]
            n = len(neighbours)
            payload  = struct.pack(">HB", self._my_addr, n)
            for mac in neighbours:
                payload += struct.pack(">H", short_addr_from_mac(mac))
            await self._broadcast(MeshMsg.ROUTE_ANNOUNCE, payload,
                                  flags=PacketFlags.HAS_ROUTING,
                                  src_addr=self._my_addr, ttl=1)

    # ── Inbound ───────────────────────────────────────────────────────────────

    async def on_message(self, peer: "Peer", msg: Message) -> None:
        if msg.msg_type == MeshMsg.DATA:
            await self._handle_data(peer, msg)

        elif msg.msg_type == MeshMsg.ROUTE_ANNOUNCE:
            self._handle_announce(peer, msg)

    async def _handle_data(self, peer: "Peer", msg: Message) -> None:
        if len(msg.payload) < 6:
            return

        seq, dst_addr, src_addr = struct.unpack_from(">HHH", msg.payload)
        app_payload = msg.payload[6:]

        # Loop detection
        if self._seen.seen(src_addr, seq):
            return

        if dst_addr == self._my_addr or dst_addr == BROADCAST_ADDR:
            # This packet is for us — deliver to application
            sender_name = peer.name or f"0x{src_addr:04X}"
            self.bus.emit_nowait(
                "mesh.data",
                src_addr = src_addr,
                dst_addr = dst_addr,
                payload  = app_payload,
                sender   = sender_name,
                hops     = DEFAULT_TTL - msg.ttl + 1,
            )

        if dst_addr != self._my_addr:
            # Forward (flood) to all other peers
            await self._forward(peer, msg, src_addr, dst_addr, seq, app_payload)

    async def _forward(
        self, incoming_peer: "Peer", msg: Message,
        src_addr: int, dst_addr: int, seq: int, app_payload: bytes,
    ) -> None:
        ttl = msg.ttl - 1
        if ttl <= 0:
            return

        envelope = struct.pack(">HHH", seq, dst_addr, src_addr) + app_payload
        packets  = build_packets(
            FeatureID.MESH, MeshMsg.DATA, seq & 0xFF, envelope,
            flags    = PacketFlags.HAS_ROUTING,
            src_addr = src_addr,
            dst_addr = dst_addr,
            ttl      = ttl,
        )

        next_hop = self._routing.next_hop(dst_addr) if dst_addr != BROADCAST_ADDR else None
        hops_so_far = DEFAULT_TTL - ttl

        if next_hop and next_hop.upper() != incoming_peer.mac.upper():
            await self.conn.send_to(next_hop, packets)
        else:
            # Flood to all peers except the one we received from
            for p in self.conn.get_peers():
                if p.mac.upper() != incoming_peer.mac.upper():
                    await self.conn.send_to(p.mac, packets)

        self.bus.emit_nowait(MESH_FORWARDED, src=src_addr, dst=dst_addr, hops=hops_so_far)

    def _handle_announce(self, peer: "Peer", msg: Message) -> None:
        if len(msg.payload) < 3:
            return
        announce_addr, n_neighbours = struct.unpack_from(">HB", msg.payload)
        # The announcer is reachable via the peer we received from
        self._routing.update(announce_addr, peer.mac, hops=1)
        # Their neighbours are reachable at hops+1
        offset = 3
        for _ in range(n_neighbours):
            if offset + 2 > len(msg.payload):
                break
            (neighbour_addr,) = struct.unpack_from(">H", msg.payload, offset)
            self._routing.update(neighbour_addr, peer.mac, hops=2)
            offset += 2
        log.debug(f"Mesh route announce from 0x{announce_addr:04X} via {peer.mac}")

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def short_addr(self) -> int:
        return self._my_addr

    def routing_table(self) -> list[dict]:
        return self._routing.dump()