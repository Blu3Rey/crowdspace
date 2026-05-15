"""
features/messaging.py
=====================
Direct (unicast) and broadcast text/binary messaging.

Wire payload (JSON inside Packet.payload):
  {
    "v":    1,                    # sub-protocol version
    "type": "text"|"binary"|"receipt",
    "id":   "<uuid>",             # message UUID for receipts
    "body": "<text>" | "<hex>",   # content
    "ts":   1234567890.123,       # unix timestamp
    "meta": {}                    # optional app-specific metadata
  }
"""

from __future__ import annotations
import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Callable, Awaitable, Optional, List, Deque, Dict

from .base import BaseFeature
from ..core.packet import Packet, PacketType, PacketFlag, BROADCAST_ADDR


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class Message:
    id:        str
    src_addr:  bytes
    dst_addr:  bytes
    body:      str
    timestamp: float = field(default_factory=time.time)
    is_read:   bool  = False
    delivered: bool  = False
    msg_type:  str   = "text"    # "text" | "binary"
    meta:      dict  = field(default_factory=dict)

    @property
    def src_str(self) -> str:
        return ":".join(f"{b:02X}" for b in self.src_addr)

    @property
    def dst_str(self) -> str:
        return ":".join(f"{b:02X}" for b in self.dst_addr)

    def __str__(self) -> str:
        direction = "→" if self.dst_addr != BROADCAST_ADDR else "⊕"
        return f"[{self.src_str}] {direction} [{self.dst_str}] : {self.body}"

    def to_dict(self) -> dict:
        return {
            "id":        self.id,
            "src":       self.src_str,
            "dst":       self.dst_str,
            "body":      self.body,
            "timestamp": self.timestamp,
            "is_read":   self.is_read,
            "delivered": self.delivered,
            "type":      self.msg_type,
            "meta":      self.meta,
        }


MessageHandler = Callable[[Message], Awaitable[None]]


# ── Messaging Feature ─────────────────────────────────────────────────────────

class MessagingFeature(BaseFeature):
    """
    Direct + broadcast messaging.

    Usage:
        msg = await messaging.send_text(dst_addr, "Hello!")
        await messaging.send_broadcast("Hello network!")
        messaging.on_receive(my_handler)   # register async callback
    """

    NAME    = "messaging"
    HANDLES = {PacketType.DIRECT_MSG, PacketType.BROADCAST_MSG}

    HISTORY_LIMIT = 500   # messages kept per conversation
    SUB_VERSION   = 1

    def __init__(self, node):
        super().__init__(node)
        # conversation history: peer_addr → deque[Message]
        self._history:  Dict[bytes, Deque[Message]] = {}
        # pending receipts: msg_id → asyncio.Future
        self._receipts: Dict[str, asyncio.Future]   = {}
        # registered receive callbacks
        self._receive_callbacks: List[MessageHandler] = []

    # ── Public API ────────────────────────────────────────────────────────────

    async def send_text(
        self,
        dst_addr: bytes,
        text:     str,
        *,
        reliable: bool  = True,
        meta:     dict  = None,
        await_receipt: bool = False,
        receipt_timeout: float = 5.0,
    ) -> Message:
        """
        Send a UTF-8 text message to a specific peer.

        Args:
            dst_addr:       6-byte destination address.
            text:           Message body.
            reliable:       Request delivery ACK from mesh layer.
            meta:           Optional metadata dict.
            await_receipt:  Wait for an application-level read receipt.
            receipt_timeout: Seconds to wait for receipt before returning.

        Returns:
            The outgoing Message object.
        """
        msg = Message(
            id       = str(uuid.uuid4()),
            src_addr = self.local_addr,
            dst_addr = dst_addr,
            body     = text,
            meta     = meta or {},
        )
        payload = self._encode(msg, "text")
        flags   = PacketFlag.NONE
        if reliable:
            flags |= PacketFlag.RELIABLE

        pkt = self.make_packet(
            PacketType.DIRECT_MSG,
            payload  = payload,
            dst_addr = dst_addr,
            flags    = flags,
            reliable = reliable,
        )
        await self.send(pkt)
        self._store(msg)

        if await_receipt:
            loop          = asyncio.get_event_loop()
            future        = loop.create_future()
            self._receipts[msg.id] = future
            try:
                await asyncio.wait_for(future, timeout=receipt_timeout)
                msg.delivered = True
            except asyncio.TimeoutError:
                self._log.warning("No receipt for msg %s within %.1fs", msg.id, receipt_timeout)
            finally:
                self._receipts.pop(msg.id, None)

        return msg

    async def send_binary(
        self,
        dst_addr: bytes,
        data:     bytes,
        *,
        reliable: bool = True,
        meta:     dict = None,
    ) -> Message:
        """Send raw binary data."""
        msg = Message(
            id       = str(uuid.uuid4()),
            src_addr = self.local_addr,
            dst_addr = dst_addr,
            body     = data.hex(),
            msg_type = "binary",
            meta     = meta or {},
        )
        payload = self._encode(msg, "binary")
        pkt = self.make_packet(
            PacketType.DIRECT_MSG,
            payload  = payload,
            dst_addr = dst_addr,
            flags    = PacketFlag.NONE,
            reliable = reliable,
        )
        await self.send(pkt)
        self._store(msg)
        return msg

    async def send_broadcast(self, text: str, meta: dict = None) -> Message:
        """Send a message to all nodes on the network."""
        msg = Message(
            id       = str(uuid.uuid4()),
            src_addr = self.local_addr,
            dst_addr = BROADCAST_ADDR,
            body     = text,
            meta     = meta or {},
        )
        payload = self._encode(msg, "text")
        pkt = self.make_packet(
            PacketType.BROADCAST_MSG,
            payload  = payload,
            dst_addr = BROADCAST_ADDR,
            flags    = PacketFlag.NONE,
        )
        await self.send(pkt)
        self._store(msg)
        return msg

    def on_receive(self, handler: MessageHandler):
        """Register an async callback invoked on every incoming message."""
        self._receive_callbacks.append(handler)

    def conversation(self, peer_addr: bytes) -> List[Message]:
        """Return chronological message history with a peer."""
        return list(self._history.get(peer_addr, []))

    def all_conversations(self) -> Dict[bytes, List[Message]]:
        return {k: list(v) for k, v in self._history.items()}

    def unread_count(self) -> int:
        return sum(1 for msgs in self._history.values()
                   for m in msgs if not m.is_read and m.src_addr != self.local_addr)

    def mark_read(self, peer_addr: bytes):
        for m in self._history.get(peer_addr, []):
            m.is_read = True

    # ── Incoming packet handler ───────────────────────────────────────────────

    async def on_packet(self, pkt: Packet):
        if pkt.ptype in (PacketType.DIRECT_MSG, PacketType.BROADCAST_MSG):
            await self._handle_message(pkt)

    async def _handle_message(self, pkt: Packet):
        try:
            data = json.loads(pkt.payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._log.warning("Malformed message payload")
            return

        msg_type = data.get("type", "text")

        if msg_type == "receipt":
            # Delivery receipt
            orig_id = data.get("id", "")
            future  = self._receipts.get(orig_id)
            if future and not future.done():
                future.set_result(True)
            return

        msg = Message(
            id       = data.get("id", str(uuid.uuid4())),
            src_addr = pkt.src_addr,
            dst_addr = pkt.dst_addr,
            body     = data.get("body", ""),
            timestamp= data.get("ts", time.time()),
            msg_type = msg_type,
            meta     = data.get("meta", {}),
        )
        self._store(msg)

        # Send read receipt
        if pkt.ptype == PacketType.DIRECT_MSG and pkt.dst_addr == self.local_addr:
            await self._send_receipt(pkt.src_addr, msg.id)

        # Invoke callbacks
        for cb in self._receive_callbacks:
            try:
                await cb(msg)
            except Exception as e:
                self._log.error("Callback error: %s", e)

        await self._dispatch_hooks(pkt)

    async def _send_receipt(self, dst: bytes, msg_id: str):
        payload = json.dumps({"v": 1, "type": "receipt", "id": msg_id}).encode()
        pkt     = self.make_packet(PacketType.DIRECT_MSG, payload=payload, dst_addr=dst)
        await self.send(pkt)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _encode(self, msg: Message, msg_type: str) -> bytes:
        return json.dumps({
            "v":    self.SUB_VERSION,
            "type": msg_type,
            "id":   msg.id,
            "body": msg.body,
            "ts":   msg.timestamp,
            "meta": msg.meta,
        }, ensure_ascii=False).encode("utf-8")

    def _store(self, msg: Message):
        key  = msg.src_addr if msg.src_addr != self.local_addr else msg.dst_addr
        hist = self._history.setdefault(key, deque(maxlen=self.HISTORY_LIMIT))
        hist.append(msg)