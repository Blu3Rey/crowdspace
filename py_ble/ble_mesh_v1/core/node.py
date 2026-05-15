"""
core/node.py — :class:`MeshNode`, the central orchestrator of the BLE mesh.

Architecture overview
---------------------
::

    ┌──────────────────────────────────────────────────────────────────────┐
    │                           MeshNode                                   │
    │                                                                      │
    │  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐   │
    │  │ TransportMgr │    │ RoutingTable │    │   Feature registry   │   │
    │  │  peripheral  │    │  DedupCache  │    │  DirectMessaging     │   │
    │  │  central     │    │ NeighborTbl  │    │  GroupChat           │   │
    │  └──────┬───────┘    └──────────────┘    │  DeviceLocator       │   │
    │         │                                │  [custom…]           │   │
    │         │ raw bytes (BLE GATT)           └──────────────────────┘   │
    │         ▼                                                            │
    │   _handle_raw()                                                      │
    │     → Packet.decode()                                                │
    │     → dedup check                                                    │
    │     → update neighbour / routing tables                              │
    │     → _dispatch()  →  feature callbacks                              │
    │     → _forward()   →  re-send on other interfaces (if TTL>0)        │
    └──────────────────────────────────────────────────────────────────────┘

Packet lifecycle
----------------
1. **Inbound** — raw bytes arrive via BLE (write or notification), decoded
   into a :class:`Packet`, deduplication-checked, dispatched to features,
   then forwarded if TTL > 0 and not already for us.
2. **Outbound** — caller calls :meth:`send` → packet assembled → optionally
   encrypted / compressed / fragmented → handed to
   :class:`~ble_mesh.transport.manager.TransportManager`.

Background tasks
----------------
* **heartbeat_loop** — periodically broadcasts DISCOVERY + HEARTBEAT.
* **maintenance_loop** — prunes stale neighbours, expired routes, old
  fragment sessions, and ACK-pending messages.
"""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Awaitable, Callable, Dict, List, Optional, Set, Type

from ..config import MeshConfig
from ..utils.logger import log
from ..utils.crypto import MeshCrypto
from .neighbor import Neighbor, NeighborTable
from .packet import Packet, FragmentAssembler, fragment
from .protocol import (
    BROADCAST_ADDR, Flags, HEADER_SIZE, MsgType,
    MESH_SERVICE_UUID,
)
from .router import DedupCache, RouteEntry, RoutingTable
from ..transport.manager import TransportManager
from ..features.base import Feature


class MeshNode:
    """Top-level BLE mesh node.

    Parameters
    ----------
    config : MeshConfig
        Global configuration.  See :class:`~ble_mesh.config.MeshConfig`.

    Example::

        cfg  = MeshConfig(node_name="Pi-A", power_profile="balanced")
        node = MeshNode(cfg)

        msg  = DirectMessaging()
        node.register_feature(msg)

        async def main():
            await node.start()
            await msg.send("hi", dst_id=peer_id)
            await node.run_forever()

        asyncio.run(main())
    """

    def __init__(self, config: MeshConfig) -> None:
        self.config   = config
        self.node_id  = config.node_id
        self.node_name = config.node_name

        # ── Core tables ───────────────────────────────────────────────────────
        self.neighbors = NeighborTable()
        self.router    = RoutingTable()
        self._dedup    = DedupCache(config.dedup_window)
        self._assembler = FragmentAssembler()

        # ── Sequence counter ─────────────────────────────────────────────────
        self._seq: int = 0

        # ── Crypto ───────────────────────────────────────────────────────────
        self._crypto: Optional[MeshCrypto] = None
        if config.enable_encryption and config.psk:
            try:
                self._crypto = MeshCrypto(config.psk)
                log.info("[Node] AES-256-GCM encryption enabled.")
            except RuntimeError as exc:
                log.warning("[Node] Encryption requested but unavailable: %s", exc)

        # ── Transport ─────────────────────────────────────────────────────────
        self._transport = TransportManager(
            node_name          = config.node_name,
            node_id            = config.node_id,
            neighbors          = self.neighbors,
            scan_duration      = config.scan_duration,
            scan_interval      = config.scan_interval,
            max_connections    = config.max_connections,
            connection_timeout = config.connection_timeout,
        )
        self._transport.set_rx_handler(self._handle_raw)
        self._transport.set_connected_handler(self._on_peer_connected)
        self._transport.set_disconnected_handler(self._on_peer_disconnected)

        # ── Features ──────────────────────────────────────────────────────────
        self._features:      List[Feature]            = []
        self._feature_map:   Dict[int, List[Feature]] = {}  # msg_type → [Feature]

        # ── Background tasks ──────────────────────────────────────────────────
        self._tasks: List[asyncio.Task] = []
        self._running = False

        # ── App-level callbacks ───────────────────────────────────────────────
        self._raw_callbacks: List[Callable[[Packet], Awaitable[None]]] = []

    # ── Feature registration ──────────────────────────────────────────────────

    def register_feature(self, feature: Feature) -> "MeshNode":
        """Attach *feature* to this node.  Returns *self* for chaining."""
        feature.attach(self)
        self._features.append(feature)
        for msg_type in feature.handled_types:
            self._feature_map.setdefault(msg_type, []).append(feature)
        log.info("[Node] Feature registered: %s (handles %s).",
                 feature.name,
                 [MsgType.name(t) for t in feature.handled_types if t != -1])
        return self

    def on_packet(self, fn: Callable[[Packet], Awaitable[None]]) -> Callable:
        """Register a low-level callback fired for every inbound packet."""
        self._raw_callbacks.append(fn)
        return fn

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the node: peripheral, scanner, and background tasks."""
        if self._running:
            return
        self._running = True
        log.info("[Node] Starting '%s' (id=%s…).", self.node_name, self.node_id.hex()[:8])

        await self._transport.start()

        # Notify features
        for f in self._features:
            try:
                await f.on_start()
            except Exception as exc:
                log.warning("[Node] Feature '%s' on_start error: %s", f.name, exc)

        # Background tasks
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="mesh-heartbeat"),
            asyncio.create_task(self._maintenance_loop(), name="mesh-maintenance"),
        ]
        log.info("[Node] Ready. (%d feature(s) active)", len(self._features))

    async def stop(self) -> None:
        """Gracefully stop the node."""
        if not self._running:
            return
        self._running = False
        log.info("[Node] Stopping…")

        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        for f in self._features:
            try:
                await f.on_stop()
            except Exception as exc:
                log.warning("[Node] Feature '%s' on_stop error: %s", f.name, exc)

        await self._transport.stop()
        log.info("[Node] Stopped.")

    async def run_forever(self) -> None:
        """Block until :meth:`stop` is called or Ctrl-C is pressed."""
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    # ── Send ──────────────────────────────────────────────────────────────────

    async def send(
        self,
        msg_type:  int,
        payload:   bytes,
        dst_id:    bytes = BROADCAST_ADDR,
        flags:     int   = Flags.NONE,
        ttl:       int   = -1,
    ) -> bool:
        """Assemble and transmit a mesh packet.

        Handles encryption, compression, fragmentation, and routing
        transparently.

        Parameters
        ----------
        msg_type : int
            One of :class:`~ble_mesh.core.protocol.MsgType`.
        payload : bytes
            Unencrypted, uncompressed application payload.
        dst_id : bytes
            Destination node ID, or :data:`BROADCAST_ADDR` for flooding.
        flags : int
            Bitmask of :class:`~ble_mesh.core.protocol.Flags`.
        ttl : int
            Override the default TTL from config (``-1`` = use config value).

        Returns
        -------
        bool
            True if at least one send call succeeded.
        """
        if ttl < 0:
            ttl = self.config.max_ttl
        seq = self._next_seq()

        # ── Apply compression ──────────────────────────────────────────────────
        wire_payload = payload
        if flags & Flags.COMPRESSED:
            import zlib
            wire_payload = zlib.compress(payload, level=6)

        # ── Apply encryption ───────────────────────────────────────────────────
        if self._crypto and not (flags & Flags.ENCRYPTED):
            # Automatically encrypt if crypto is configured
            wire_payload = self._crypto.encrypt(wire_payload)
            flags |= Flags.ENCRYPTED

        max_p = self.config.max_payload_size

        # ── Fragment if necessary ──────────────────────────────────────────────
        if len(wire_payload) > max_p:
            packets = fragment(
                msg_type=msg_type, src_id=self.node_id, dst_id=dst_id,
                base_seq=seq, full_payload=wire_payload,
                max_payload=max_p, base_flags=flags, ttl=ttl,
            )
            results = await asyncio.gather(
                *[self._transmit(p) for p in packets], return_exceptions=True
            )
            return any(r is True for r in results)

        # ── Single packet ──────────────────────────────────────────────────────
        pkt = Packet(
            msg_type=msg_type, src_id=self.node_id, dst_id=dst_id,
            seq_num=seq, payload=wire_payload, ttl=ttl, flags=flags,
        )
        # Mark our own packet in dedup cache to avoid echoing
        self._dedup.is_duplicate(pkt)
        return await self._transmit(pkt)

    async def _transmit(self, packet: Packet) -> bool:
        """Encode and send *packet* via the transport layer."""
        raw = packet.encode()

        # Unicast: try to send directly to the known next-hop
        if not packet.is_broadcast:
            route = self.router.lookup(packet.dst_id)
            if route:
                ok = await self._transport.send_to_address(route.next_hop_addr, raw)
                if ok:
                    log.debug("[Node] → %s via %s (direct route).",
                              MsgType.name(packet.msg_type), route.next_hop_addr)
                    return True
            # Fallback to broadcast flooding
            log.debug("[Node] No route to %s — flooding.", packet.dst_id.hex()[:8])

        return await self._transport.send(raw, dst_id=packet.dst_id if not packet.is_broadcast else None)

    # ── Inbound pipeline ──────────────────────────────────────────────────────

    async def _handle_raw(self, raw: bytes, peer_addr: str) -> None:
        """Called by the transport layer when bytes arrive over BLE."""
        try:
            packet = Packet.decode(raw)
        except ValueError as exc:
            log.debug("[Node] Malformed packet from %s: %s", peer_addr, exc)
            return

        # Update neighbour table with peer info
        nb = self.neighbors.get_by_address(peer_addr)
        if nb:
            nb.update(packet.rssi or -100)

        # Deduplication — drop if already processed
        if self._dedup.is_duplicate(packet):
            log.debug("[Node] Dedup drop: %s.", packet)
            return

        # Handle fragment assembly
        if packet.msg_type == MsgType.FRAGMENT:
            reassembled = self._assembler.feed(packet)
            if reassembled is None:
                return
            # Replace payload with reassembled data; use original flags sans FRAGMENT
            try:
                # The first fragment's header is the "real" packet type —
                # we embed the original msg_type in the first byte of fragment data.
                # (See: fragment() sets msg_type=FRAGMENT for all frags.)
                # For simplicity, we signal the original type in the top-level packet
                # assembled here.  Features should handle FRAGMENT type too if needed.
                packet = Packet(
                    msg_type=MsgType.DIRECT_MSG,  # best-effort; features re-parse
                    src_id=packet.src_id, dst_id=packet.dst_id,
                    seq_num=packet.seq_num, payload=reassembled,
                    ttl=packet.ttl, flags=packet.flags & ~Flags.FRAGMENTED,
                )
            except Exception as exc:
                log.warning("[Node] Fragment reassembly error: %s", exc)
                return

        # Decrypt if encrypted
        if packet.flags & Flags.ENCRYPTED:
            if self._crypto is None:
                log.warning("[Node] Received encrypted packet but crypto not configured — dropping.")
                return
            try:
                payload = self._crypto.decrypt(packet.payload)
                from dataclasses import replace
                packet = replace(packet, payload=payload, flags=packet.flags & ~Flags.ENCRYPTED)
            except Exception as exc:
                log.warning("[Node] Decryption failed: %s", exc)
                return

        # Update routing table — we know peer_addr is a direct link
        if nb:
            self.router.update(
                dst_id=packet.src_id,
                next_hop_id=nb.node_id,
                next_hop_addr=peer_addr,
                hop_count=1,
                rssi=packet.rssi or -100,
            )

        # Handle control packets locally regardless of destination
        if packet.msg_type in (MsgType.DISCOVERY, MsgType.HEARTBEAT):
            await self._handle_discovery(packet, peer_addr)

        # ── For us? ───────────────────────────────────────────────────────────
        is_for_us = packet.dst_id == self.node_id or packet.is_broadcast
        if is_for_us:
            await self._dispatch(packet)

        # ── Forward? ──────────────────────────────────────────────────────────
        if packet.ttl > 1 and (packet.is_broadcast or not is_for_us):
            await self._forward(packet)

    async def _handle_discovery(self, packet: Packet, peer_addr: str) -> None:
        """Parse DISCOVERY/HEARTBEAT payload and update neighbour table."""
        if len(packet.payload) < 16:
            return
        peer_node_id = packet.payload[:16]
        peer_name    = packet.payload[16:].decode("utf-8", errors="replace").rstrip("\x00")
        self.neighbors.upsert(
            peer_node_id, peer_addr,
            name=peer_name, rssi=packet.rssi or -100,
        )
        self.router.update(
            dst_id=peer_node_id,
            next_hop_id=peer_node_id,
            next_hop_addr=peer_addr,
            hop_count=1,
            rssi=packet.rssi or -100,
        )
        log.info("[Node] Discovered: '%s' (%s…) at %s RSSI=%s.",
                 peer_name, peer_node_id.hex()[:8], peer_addr, packet.rssi)

    async def _dispatch(self, packet: Packet) -> None:
        """Dispatch *packet* to registered feature handlers."""
        for cb in self._raw_callbacks:
            try:
                result = cb(packet)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                log.warning("[Node] Raw callback error: %s", exc)

        handlers = (
            self._feature_map.get(packet.msg_type, [])
            + self._feature_map.get(-1, [])   # wildcard features
        )
        for feature in handlers:
            try:
                await feature.handle(packet)
            except Exception as exc:
                log.warning("[Node] Feature '%s' handle() error: %s", feature.name, exc)

    async def _forward(self, packet: Packet) -> None:
        """Forward *packet* with TTL decremented."""
        fwd = packet.forwarded()
        raw = fwd.encode()
        await self._transport.send(raw)
        log.debug("[Node] Forwarded %s (ttl %d→%d).", packet, packet.ttl, fwd.ttl)

    # ── Peer connect / disconnect ─────────────────────────────────────────────

    async def _on_peer_connected(self, neighbor: Neighbor) -> None:
        log.info("[Node] Peer connected: '%s' (%s…).",
                 neighbor.name, neighbor.node_id.hex()[:8])
        # Send discovery so the peer can register us
        await self.send(
            MsgType.DISCOVERY,
            self.node_id + self.node_name.encode("utf-8"),
            dst_id=neighbor.node_id,
        )

    async def _on_peer_disconnected(self, neighbor: Neighbor) -> None:
        log.info("[Node] Peer disconnected: '%s' (%s…).",
                 neighbor.name, neighbor.node_id.hex()[:8])
        self.router.invalidate(neighbor.node_id)

    # ── Background loops ──────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Periodically broadcast DISCOVERY to keep neighbours aware of us."""
        await asyncio.sleep(1)   # let the peripheral start first
        while self._running:
            payload = self.node_id + self.node_name.encode("utf-8")
            try:
                await self.send(MsgType.HEARTBEAT, payload, dst_id=BROADCAST_ADDR)
                log.debug("[Node] HEARTBEAT broadcast.")
            except Exception as exc:
                log.warning("[Node] Heartbeat error: %s", exc)
            await asyncio.sleep(self.config.heartbeat_interval)

    async def _maintenance_loop(self) -> None:
        """Prune stale state every 60 seconds."""
        while self._running:
            await asyncio.sleep(60)
            stale_nb  = self.neighbors.prune_stale()
            exp_routes = self.router.prune_expired()
            frag_prune = self._assembler.prune_stale()
            log.debug("[Node] Maintenance: pruned %d neighbours, %d routes, %d frag sessions.",
                      len(stale_nb), exp_routes, frag_prune)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq

    # ── Status ───────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return a snapshot of the node's current state (useful for dashboards)."""
        return {
            "node_id":     self.node_id.hex(),
            "node_name":   self.node_name,
            "running":     self._running,
            "connections": self._transport.connection_count,
            "neighbors":   [n.to_dict() for n in self.neighbors.all()],
            "routes":      len(self.router),
            "features":    [f.name for f in self._features],
            "seq":         self._seq,
        }

    def __repr__(self) -> str:
        return (
            f"<MeshNode name='{self.node_name}' "
            f"id={self.node_id.hex()[:8]}… "
            f"running={self._running}>"
        )