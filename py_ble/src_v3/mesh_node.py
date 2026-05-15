"""
mesh_node.py
============
Central orchestrator for the BLE Mesh Network node.

MeshNode owns:
  • BLEManager       – hardware I/O
  • KeyManager       – cryptography
  • PacketFactory    – packet creation
  • MeshRouter       – routing engine
  • RoutingTable     – known peers
  • GroupRegistry    – chat rooms
  • FeatureRegistry  – pluggable features

Typical startup:

    node = MeshNode(name="Alice")
    node.load_default_features()
    await node.start()

    # Use built-in features
    await node.messaging.send_text(peer_addr, "Hi!")
    await node.group_chat.create_room("ops")
    estimate = await node.locator.locate(peer_addr)

    await node.stop()
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import struct
import time
from typing import Optional, List, Dict, Any

from .core.packet import (
    Packet, PacketType, PacketFlag, PacketFactory,
    BROADCAST_ADDR, HEADER_SIZE, TAG_SIZE
)
from .core.crypto  import KeyManager, generate_network_key
from .core.node    import RoutingTable, GroupRegistry
from .core.mesh_router import MeshRouter
from .core.ble_manager import BLEManager
from .features.base    import BaseFeature, FeatureRegistry

log = logging.getLogger("mesh_node")


# ── MeshNode ──────────────────────────────────────────────────────────────────

class MeshNode:
    """
    A fully-featured BLE mesh network node.

    Args:
        name:         Human-readable node name (≤28 chars).
        addr:         6-byte node address; auto-generated if omitted.
        network_key:  32-byte PSK; auto-generated if omitted.
        log_level:    Python logging level string.
    """

    HEARTBEAT_INTERVAL = 10.0   # seconds between heartbeat broadcasts
    PING_INTERVAL      = 30.0   # seconds between RTT probes

    def __init__(
        self,
        name:         str            = "MeshNode",
        addr:         Optional[bytes] = None,
        network_key:  Optional[bytes] = None,
        log_level:    str             = "INFO",
    ):
        logging.basicConfig(
            level  = getattr(logging, log_level.upper(), logging.INFO),
            format = "%(asctime)s [%(name)s] %(levelname)s %(message)s",
        )

        self._name        = name[:28]
        self._addr        = addr or os.urandom(6)
        self._started     = False

        # Core subsystems
        self._crypto      = KeyManager(network_key)
        self._factory     = PacketFactory(self._addr)
        self._rt          = RoutingTable()
        self._groups      = GroupRegistry()
        self._features    = FeatureRegistry()

        self._router      = MeshRouter(
            local_addr    = self._addr,
            factory       = self._factory,
            routing_table = self._rt,
            crypto        = self._crypto,
            on_send       = self._ble_send,
            on_deliver    = self._deliver_packet,
        )

        self._ble         = BLEManager(
            local_addr        = self._addr,
            node_name         = self._name,
            on_packet         = self._on_ble_packet,
            on_peer_detected  = self._on_peer_detected,
        )

        self._tasks:      List[asyncio.Task] = []
        self._event_hooks: Dict[str, list]   = {}

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def local_addr(self) -> bytes:
        return self._addr

    @property
    def name(self) -> str:
        return self._name

    @property
    def factory(self) -> PacketFactory:
        return self._factory

    @property
    def routing_table(self) -> RoutingTable:
        return self._rt

    @property
    def group_registry(self) -> GroupRegistry:
        return self._groups

    @property
    def addr_str(self) -> str:
        return ":".join(f"{b:02X}" for b in self._addr)

    # Feature shortcuts (populated by load_default_features)
    @property
    def messaging(self):
        return self._features.get("messaging")

    @property
    def group_chat(self):
        return self._features.get("group_chat")

    @property
    def locator(self):
        return self._features.get("locator")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        """Start all subsystems."""
        if self._started:
            return
        self._started = True
        log.info("[MeshNode] Starting node '%s' (%s)", self._name, self.addr_str)

        await self._router.start()
        await self._ble.start()
        await self._features.start_all()

        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="node-heartbeat"))
        self._tasks.append(asyncio.create_task(self._ping_loop(),      name="node-ping"))
        log.info("[MeshNode] Node '%s' is online", self._name)

    async def stop(self):
        """Graceful shutdown."""
        if not self._started:
            return
        log.info("[MeshNode] Shutting down '%s'", self._name)
        for t in self._tasks:
            t.cancel()
        await self._features.stop_all()
        await self._router.stop()
        await self._ble.stop()
        self._started = False

    # ── Feature Management ────────────────────────────────────────────────────

    def load_default_features(self):
        """Register the built-in messaging, group chat, and locator features."""
        from .features.messaging  import MessagingFeature
        from .features.group_chat import GroupChatFeature
        from .features.locator    import LocatorFeature

        self.register_feature(MessagingFeature(self))
        self.register_feature(GroupChatFeature(self))
        self.register_feature(LocatorFeature(self))

    def register_feature(self, feature: BaseFeature):
        """Dynamically add a feature plugin."""
        self._features.register(feature)
        log.info("[MeshNode] Feature registered: %s", feature.NAME)

    def unregister_feature(self, name: str):
        self._features.unregister(name)

    # ── Sending ───────────────────────────────────────────────────────────────

    async def router_send(self, pkt: Packet):
        """Public entry point for features to send packets."""
        await self._router.send(pkt)

    async def send_raw(
        self,
        ptype:    PacketType,
        payload:  bytes,
        dst_addr: bytes       = BROADCAST_ADDR,
        group_id: int         = 0,
        flags:    PacketFlag  = PacketFlag.NONE,
        reliable: bool        = False,
        ttl:      int         = 7,
    ) -> Packet:
        """Low-level: build and send an arbitrary packet."""
        if reliable:
            flags |= PacketFlag.RELIABLE
        pkt = self._factory.build(ptype, payload, dst_addr, group_id, ttl, flags)
        await self._router.send(pkt)
        return pkt

    # ── Network Utilities ─────────────────────────────────────────────────────

    async def ping(self, peer_addr: bytes) -> Optional[float]:
        """
        Send a PING and return the round-trip time in milliseconds,
        or None on timeout.
        """
        payload = struct.pack("<d", time.monotonic())   # 8-byte timestamp
        pkt     = self._factory.build(
            PacketType.PING,
            payload  = payload,
            dst_addr = peer_addr,
            ttl      = 5,
        )
        # RTT is recorded by the router's _handle_pong
        await self._router.send(pkt)
        await asyncio.sleep(2.0)
        node = self._rt.get(peer_addr)
        return node.rtt_ms if node and node.rtt_ms < float("inf") else None

    def peers(self) -> List[dict]:
        return [n.to_dict() for n in self._rt.all_nodes()]

    def neighbors(self) -> List[dict]:
        return [n.to_dict() for n in self._rt.neighbors()]

    def status(self) -> dict:
        return {
            "name":      self._name,
            "addr":      self.addr_str,
            "peers":     len(self._rt.all_nodes()),
            "neighbors": len(self._rt.neighbors()),
            "groups":    len(self._groups.all_groups()),
            "features":  self._features.all_features,
            "uptime":    time.monotonic(),
        }

    # ── Event System ──────────────────────────────────────────────────────────

    def on_event(self, event: str, handler):
        """
        Register a callback for named events:
          "peer_joined", "peer_left", "packet_received"
        """
        self._event_hooks.setdefault(event, []).append(handler)

    async def _emit(self, event: str, *args, **kwargs):
        for h in self._event_hooks.get(event, []):
            try:
                result = h(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.debug("Event hook error: %s", e)

    # ── Internal Callbacks ────────────────────────────────────────────────────

    async def _on_ble_packet(self, data: bytes, src_addr: bytes, rssi: int):
        """Invoked by BLEManager when raw bytes arrive."""
        try:
            pkt = Packet.from_bytes(data)
        except (ValueError, struct.error) as e:
            log.debug("[MeshNode] Malformed packet: %s", e)
            return
        await self._router.receive(pkt, rssi)

    async def _on_peer_detected(self, addr: bytes, name: str, rssi: int):
        """Invoked by BLEManager when a new BLE advertisement is seen."""
        is_new = self._rt.get(addr) is None
        self._rt.upsert(addr, rssi=rssi, name=name, hop_distance=1, next_hop=addr)
        if is_new:
            log.info("[MeshNode] Peer discovered: %s (%s) RSSI=%d", name, addr.hex(), rssi)
            await self._emit("peer_joined", addr, name, rssi)

    async def _deliver_packet(self, pkt: Packet):
        """Called by router when a packet is addressed to this node."""
        log.debug("[MeshNode] Delivering: %s", pkt)
        await self._emit("packet_received", pkt)
        await self._features.dispatch(pkt)

    async def _ble_send(self, peer_addr: bytes, data: bytes) -> bool:
        """Adapter: router → BLEManager."""
        return await self._ble.send(peer_addr, data)

    # ── Background Tasks ──────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Broadcast node metadata so peers can update their routing tables."""
        while True:
            meta = json.dumps({
                "name":     self._name,
                "pk":       self._crypto.public_key_bytes.hex(),
                "groups":   [g.group_id for g in self._groups.groups_for_member(self._addr)],
                "features": self._features.all_features,
                "ts":       time.time(),
            }, ensure_ascii=False).encode("utf-8")

            pkt = self._factory.build(
                PacketType.HEARTBEAT,
                payload  = meta,
                dst_addr = BROADCAST_ADDR,
                ttl      = 3,
            )
            await self._router.send(pkt)
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    async def _ping_loop(self):
        """Periodically ping all direct neighbors to maintain RTT estimates."""
        await asyncio.sleep(15)   # grace period on startup
        while True:
            for node in self._rt.neighbors():
                payload = struct.pack("<d", time.monotonic())
                pkt     = self._factory.build(
                    PacketType.PING,
                    payload  = payload,
                    dst_addr = node.addr,
                    ttl      = 2,
                )
                await self._router.send(pkt)
                await asyncio.sleep(0.2)   # stagger pings
            await asyncio.sleep(self.PING_INTERVAL)


# ── Module-level convenience ──────────────────────────────────────────────────

async def create_node(
    name:        str            = "MeshNode",
    addr:        Optional[bytes] = None,
    network_key: Optional[bytes] = None,
    features:    bool            = True,
    log_level:   str             = "INFO",
) -> MeshNode:
    """
    Factory function: create, configure, and start a MeshNode.

    Example::

        node = await create_node(name="Alice")
        await node.messaging.send_text(bob_addr, "Hello, Bob!")
    """
    node = MeshNode(name=name, addr=addr, network_key=network_key, log_level=log_level)
    if features:
        node.load_default_features()
    await node.start()
    return node