"""
features/__init__.py — Plugin system: FeatureBase and FeatureRegistry.

Adding a new BLE feature requires three steps and zero changes to existing code:

    1.  Create ble_stack/features/my_feature.py
    2.  Subclass FeatureBase and set FEATURE_ID = FeatureID.CUSTOM (or add a value)
    3.  Register it: registry.register(MyFeature(bus, conn))

The FeatureRegistry then routes every Message with matching feature_id to that
handler automatically.

FeatureBase gives every feature:
    - self.bus    — EventBus for pub/sub communication with other layers
    - self.conn   — ConnectionManager for sending packets
    - self._send  — convenience wrapper around build_packets + conn
    - lifecycle hooks: start() / stop()
    - on_message() override point
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

from ..events import EventBus
from ..protocol import FeatureID, Message, build_packets
from ..constants import LOOPBACK_ADDR, BROADCAST_ADDR

if TYPE_CHECKING:
    from ..connection.manager import ConnectionManager
    from ..connection.peer import Peer

log = logging.getLogger(__name__)


class FeatureBase(ABC):
    """
    Base class for all BLE application features.
    Subclasses must set FEATURE_ID and implement on_message().
    """

    FEATURE_ID: FeatureID = NotImplemented

    def __init__(self, bus: EventBus, conn: "ConnectionManager"):
        if self.FEATURE_ID is NotImplemented:
            raise TypeError(f"{type(self).__name__} must define FEATURE_ID")
        self.bus  = bus
        self.conn = conn
        self._running = False

    @abstractmethod
    async def on_message(self, peer: "Peer", msg: Message) -> None:
        ...

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def _send(self, peer, msg_type, payload, *, flags=0, dst_addr=LOOPBACK_ADDR,
                    src_addr=LOOPBACK_ADDR, ttl=0, group_id=0) -> int:
        msg_id  = peer.next_msg_id(int(self.FEATURE_ID))
        packets = build_packets(
            self.FEATURE_ID, msg_type, msg_id, payload,
            flags=flags, src_addr=src_addr, dst_addr=dst_addr, ttl=ttl, group_id=group_id,
        )
        await self.conn.send_to(peer.mac, packets)
        return msg_id

    async def _broadcast(self, msg_type, payload, *, flags=0, src_addr=LOOPBACK_ADDR,
                         ttl=0, group_id=0) -> None:
        packets = build_packets(
            self.FEATURE_ID, msg_type, 0, payload,
            flags=flags, src_addr=src_addr, dst_addr=BROADCAST_ADDR, ttl=ttl, group_id=group_id,
        )
        await self.conn.broadcast(packets)


class FeatureRegistry:
    """Routes incoming Messages to the right FeatureBase handler by feature_id."""

    def __init__(self):
        self._features: dict[FeatureID, FeatureBase] = {}

    def register(self, feature: FeatureBase) -> None:
        fid = feature.FEATURE_ID
        if fid in self._features:
            raise ValueError(f"Feature {fid.name} already registered")
        self._features[fid] = feature
        log.info(f"Registered feature: {type(feature).__name__} ({fid.name})")

    def get(self, feature_id: FeatureID) -> Optional[FeatureBase]:
        return self._features.get(feature_id)

    async def dispatch(self, peer, msg: Message) -> None:
        feature = self._features.get(msg.feature_id)
        if feature is None:
            log.debug(f"No handler for {msg.feature_id!r}")
            return
        try:
            await feature.on_message(peer, msg)
        except Exception:
            log.exception(f"{feature.FEATURE_ID.name} error")

    async def start_all(self) -> None:
        for f in self._features.values(): await f.start()

    async def stop_all(self) -> None:
        for f in self._features.values(): await f.stop()

    def registered(self) -> list[str]:
        return [f"{type(f).__name__}({f.FEATURE_ID.name})" for f in self._features.values()]