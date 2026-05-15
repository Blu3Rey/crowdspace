"""
features/base.py
================
Abstract base class for all mesh-network feature plugins.

A Feature is a self-contained module that:
  • Registers for specific PacketTypes it handles
  • Has access to the MeshNode to send packets
  • Exposes a clean API to user-facing code (CLI, GUI, etc.)

Implementing a new feature:

    class MyFeature(BaseFeature):
        NAME     = "my_feature"
        HANDLES  = {PacketType.FEATURE_MSG}

        async def on_packet(self, pkt: Packet):
            ...   # parse and act on incoming packets

        async def my_action(self, ...):
            pkt = self.make_packet(PacketType.FEATURE_MSG, payload=...)
            await self.send(pkt)
"""

from __future__ import annotations
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Set, TYPE_CHECKING, Optional, Callable, Awaitable, Any

from ..core.packet import Packet, PacketType, PacketFlag, BROADCAST_ADDR

if TYPE_CHECKING:
    from ..mesh_node import MeshNode

log = logging.getLogger(__name__)


class BaseFeature(ABC):
    """
    Every feature plugin must subclass this.
    """

    # ── Subclass contract ─────────────────────────────────────────────────────

    NAME: str            = "base"           # unique feature identifier
    HANDLES: Set[PacketType] = set()        # which packet types to receive

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def __init__(self, node: "MeshNode"):
        self._node  = node
        self._log   = logging.getLogger(f"feature.{self.NAME}")
        self._hooks: list[Callable[[Packet], Awaitable[None]]] = []

    async def start(self):
        """Called when the MeshNode starts. Override for setup."""

    async def stop(self):
        """Called on graceful shutdown. Override for cleanup."""

    @abstractmethod
    async def on_packet(self, pkt: Packet):
        """
        Handle an incoming packet that belongs to this feature.
        Called by the MeshNode dispatcher.
        """

    # ── Convenience helpers ───────────────────────────────────────────────────

    async def send(self, pkt: Packet):
        """Send a packet via the node's router."""
        await self._node.router_send(pkt)

    def make_packet(
        self,
        ptype:    PacketType,
        payload:  bytes        = b"",
        dst_addr: bytes        = BROADCAST_ADDR,
        group_id: int          = 0,
        ttl:      int          = 7,
        flags:    PacketFlag   = PacketFlag.NONE,
        reliable: bool         = False,
    ) -> Packet:
        """Create a packet using the node's factory."""
        if reliable:
            flags |= PacketFlag.RELIABLE
        return self._node.factory.build(
            ptype    = ptype,
            payload  = payload,
            dst_addr = dst_addr,
            group_id = group_id,
            ttl      = ttl,
            flags    = flags,
        )

    @property
    def local_addr(self) -> bytes:
        return self._node.local_addr

    @property
    def routing_table(self):
        return self._node.routing_table

    @property
    def group_registry(self):
        return self._node.group_registry

    # ── Event hooks ───────────────────────────────────────────────────────────

    def on_message(self, handler: Callable[[Packet], Awaitable[None]]):
        """Register a callback for every packet this feature receives."""
        self._hooks.append(handler)

    async def _dispatch_hooks(self, pkt: Packet):
        for hook in self._hooks:
            try:
                await hook(pkt)
            except Exception as e:
                self._log.error("Hook error: %s", e)

    # ── Utility ───────────────────────────────────────────────────────────────

    @staticmethod
    def addr_str(addr: bytes) -> str:
        return ":".join(f"{b:02X}" for b in addr)

    def __repr__(self) -> str:
        return f"<Feature:{self.NAME}>"


# ── Feature Registry ──────────────────────────────────────────────────────────

class FeatureRegistry:
    """
    Maps PacketTypes → Feature instances.
    Supports dynamic registration at runtime.
    """

    def __init__(self):
        self._features: dict[str, BaseFeature]       = {}
        self._handlers: dict[PacketType, list[BaseFeature]] = {}

    def register(self, feature: BaseFeature):
        self._features[feature.NAME] = feature
        for ptype in feature.HANDLES:
            self._handlers.setdefault(ptype, []).append(feature)
        log.info("[FeatureRegistry] Registered feature: %s", feature.NAME)

    def unregister(self, name: str):
        feature = self._features.pop(name, None)
        if feature:
            for ptype in feature.HANDLES:
                handlers = self._handlers.get(ptype, [])
                if feature in handlers:
                    handlers.remove(feature)

    def get(self, name: str) -> Optional[BaseFeature]:
        return self._features.get(name)

    async def dispatch(self, pkt: Packet):
        handlers = self._handlers.get(pkt.ptype, [])
        if not handlers:
            log.debug("[FeatureRegistry] No handler for %s", pkt.ptype.name)
            return
        await asyncio.gather(*[h.on_packet(pkt) for h in handlers])

    async def start_all(self):
        await asyncio.gather(*[f.start() for f in self._features.values()])

    async def stop_all(self):
        await asyncio.gather(*[f.stop() for f in self._features.values()])

    @property
    def all_features(self) -> list[str]:
        return list(self._features.keys())