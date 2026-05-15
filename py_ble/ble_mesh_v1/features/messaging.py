"""
features/messaging.py — Reliable unicast direct messaging.

Wire format for DIRECT_MSG payload
-----------------------------------
  [msg_id: 4 bytes uint32][body: UTF-8 encoded text or raw bytes]

The *msg_id* echoed in ACK payloads allows the sender to match ACKs to
outstanding sends and cancel the retry timer.

Usage::

    messaging = DirectMessaging(node)
    node.register_feature(messaging)

    # Register a callback for inbound messages
    @messaging.on_message
    async def handler(src_id: bytes, text: str, msg_id: int):
        print(f"[{src_id.hex()[:8]}] {text}")

    # Send a message
    await messaging.send("Hello!", dst_id=target_node_id, reliable=True)
"""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Awaitable, Callable, Dict, List, Optional

from ..core.packet import Packet
from ..core.protocol import Flags, MsgType
from ..utils.logger import log
from .base import Feature

# Message callback signature: (src_id, text_or_bytes, msg_id) → None/Awaitable
MsgCallback = Callable[[bytes, str, int], Awaitable[None]]

_PAYLOAD_FMT = "!I"   # msg_id prefix (4 bytes)
_PREFIX_SIZE = struct.calcsize(_PAYLOAD_FMT)


class DirectMessaging(Feature):
    """Reliable (optional ACK + retry) unicast text / binary messaging.

    Parameters
    ----------
    ack_timeout : float
        Seconds to wait for an ACK before retransmitting.
    max_retries : int
        Number of retransmission attempts before giving up.
    """

    handled_types = frozenset({MsgType.DIRECT_MSG, MsgType.ACK})
    name = "direct-messaging"

    def __init__(self, ack_timeout: float = 5.0, max_retries: int = 3) -> None:
        super().__init__()
        self._ack_timeout  = ack_timeout
        self._max_retries  = max_retries
        self._callbacks:   List[MsgCallback] = []
        # msg_id → asyncio.Event (set when ACK arrives)
        self._pending_acks: Dict[int, asyncio.Event] = {}
        # msg_id counter (per-node, not per-session — good enough for demos)
        self._msg_counter  = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def on_message(self, fn: MsgCallback) -> MsgCallback:
        """Decorator / direct call to register an inbound-message callback."""
        self._callbacks.append(fn)
        return fn

    async def send(
        self,
        text: str | bytes,
        dst_id: bytes,
        reliable: bool = True,
        encoding: str = "utf-8",
    ) -> bool:
        """Send a direct message to *dst_id*.

        Parameters
        ----------
        text : str | bytes
            The message body.  Strings are UTF-8 encoded automatically.
        dst_id : bytes
            16-byte destination node ID.
        reliable : bool
            If True, waits for an ACK and retransmits on timeout.
        """
        body = text.encode(encoding) if isinstance(text, str) else text
        msg_id = self._next_msg_id()
        payload = struct.pack(_PAYLOAD_FMT, msg_id) + body
        flags = Flags.ACK_REQ if reliable else Flags.NONE

        if not reliable:
            return await self.node.send(MsgType.DIRECT_MSG, payload, dst_id=dst_id, flags=flags)

        # ── Reliable send with retry ──────────────────────────────────────────
        ack_event = asyncio.Event()
        self._pending_acks[msg_id] = ack_event

        try:
            for attempt in range(1, self._max_retries + 1):
                log.debug("[Messaging] Sending msg_id=%d to %s (attempt %d/%d)",
                          msg_id, dst_id.hex()[:8], attempt, self._max_retries)
                ok = await self.node.send(
                    MsgType.DIRECT_MSG, payload, dst_id=dst_id, flags=flags
                )
                if not ok:
                    log.warning("[Messaging] Send failed on attempt %d.", attempt)
                    await asyncio.sleep(0.5)
                    continue
                try:
                    await asyncio.wait_for(ack_event.wait(), timeout=self._ack_timeout)
                    log.debug("[Messaging] ACK received for msg_id=%d.", msg_id)
                    return True
                except asyncio.TimeoutError:
                    log.debug("[Messaging] ACK timeout for msg_id=%d (attempt %d).", msg_id, attempt)
                    ack_event.clear()
        finally:
            self._pending_acks.pop(msg_id, None)

        log.warning("[Messaging] Message msg_id=%d delivery failed after %d attempts.",
                    msg_id, self._max_retries)
        return False

    # ── Feature interface ─────────────────────────────────────────────────────

    async def handle(self, packet: Packet) -> None:
        if packet.msg_type == MsgType.ACK:
            await self._handle_ack(packet)
        elif packet.msg_type == MsgType.DIRECT_MSG:
            await self._handle_message(packet)

    async def _handle_message(self, packet: Packet) -> None:
        if len(packet.payload) < _PREFIX_SIZE:
            return
        msg_id = struct.unpack(_PAYLOAD_FMT, packet.payload[:_PREFIX_SIZE])[0]
        body   = packet.payload[_PREFIX_SIZE:]
        text   = body.decode("utf-8", errors="replace")

        log.info("[Messaging] ← %s from %s (msg_id=%d)",
                 repr(text[:60]), packet.src_id.hex()[:8], msg_id)

        # Send ACK if requested
        if packet.flags & Flags.ACK_REQ:
            ack = packet.make_ack(self.node.node_id)
            ack_payload = struct.pack(_PAYLOAD_FMT, msg_id)
            await self.node.send(MsgType.ACK, ack_payload, dst_id=packet.src_id)

        for cb in self._callbacks:
            try:
                result = cb(packet.src_id, text, msg_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                log.warning("[Messaging] Callback error: %s", exc)

    async def _handle_ack(self, packet: Packet) -> None:
        if len(packet.payload) < _PREFIX_SIZE:
            return
        msg_id = struct.unpack(_PAYLOAD_FMT, packet.payload[:_PREFIX_SIZE])[0]
        event  = self._pending_acks.get(msg_id)
        if event:
            event.set()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _next_msg_id(self) -> int:
        self._msg_counter = (self._msg_counter + 1) & 0xFFFFFFFF
        return self._msg_counter

    def message_history(self) -> List[dict]:
        """Return metadata for messages currently awaiting ACK."""
        return [{"msg_id": mid} for mid in self._pending_acks]