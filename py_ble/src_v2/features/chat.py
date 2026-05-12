"""
features/chat.py — 1-to-1 text chat + core protocol (HANDSHAKE / PING / PONG).

Two separate features in one file because they are tightly related:

    CoreFeature  (FEATURE_ID = FeatureID.CORE)
        Manages HANDSHAKE, PING, PONG, GOODBYE.
        Emits EventBus events that the app layer listens to.

    ChatFeature  (FEATURE_ID = FeatureID.CHAT)
        Sends and receives UTF-8 text messages with application-level ACK.
        Emits chat.received and chat.acked events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional, TYPE_CHECKING

from ..events import (
    CHAT_ACKED, CHAT_RECEIVED, EventBus, PEER_CONNECTED, PEER_DISCONNECTED,
)
from ..protocol import CoreMsg, FeatureID, Message
from . import FeatureBase

if TYPE_CHECKING:
    from ..connection.manager import ConnectionManager
    from ..connection.peer import Peer

log = logging.getLogger(__name__)


# ── Chat message types ────────────────────────────────────────────────────────

class ChatMsg:
    TEXT       = 0x01
    ACK        = 0x02
    TYPING_ON  = 0x03
    TYPING_OFF = 0x04


# ── Core feature ──────────────────────────────────────────────────────────────

class CoreFeature(FeatureBase):
    """
    Handles the handshake that names each peer, plus keepalive ping/pong.

    On HANDSHAKE receipt:
        Sets peer.name and peer.state, emits peer.connected.
        Replies with our own HANDSHAKE if we haven't yet.

    On PING:  replies PONG immediately.
    On PONG:  records RTT and emits peer.rssi update.
    On GOODBYE: emits peer.disconnected and triggers cleanup.
    """

    FEATURE_ID = FeatureID.CORE

    def __init__(self, bus: EventBus, conn: "ConnectionManager", my_name: str):
        super().__init__(bus, conn)
        self.my_name        = my_name
        self._ping_sent_at: dict[str, float] = {}   # mac → timestamp
        self._handshook:    set[str]          = set()

    async def start(self) -> None:
        await super().start()
        # When a new peer is registered, send our handshake
        self.bus.on(PEER_CONNECTED, self._on_peer_connected)

    async def _on_peer_connected(self, peer: "Peer", **_) -> None:
        await self.send_handshake(peer)

    async def send_handshake(self, peer: "Peer") -> None:
        await self._send(peer, CoreMsg.HANDSHAKE, self.my_name)

    async def send_ping(self, peer: "Peer") -> None:
        self._ping_sent_at[peer.mac] = time.monotonic()
        await self._send(peer, CoreMsg.PING, b"")

    async def on_message(self, peer: "Peer", msg: Message) -> None:
        t = msg.msg_type

        if t == CoreMsg.HANDSHAKE:
            peer.name = msg.payload.decode("utf-8", errors="replace")
            from ..connection.peer import PeerState
            peer.state = PeerState.CONNECTED
            log.info(f"Handshake from {peer.name} ({peer.mac})")

            # Reply once if we haven't already sent ours
            if peer.mac not in self._handshook:
                self._handshook.add(peer.mac)
                await self.send_handshake(peer)

            self.bus.emit_nowait("peer.named", peer=peer)

        elif t == CoreMsg.PING:
            await self._send(peer, CoreMsg.PONG, b"")

        elif t == CoreMsg.PONG:
            sent_at = self._ping_sent_at.pop(peer.mac, None)
            if sent_at:
                rtt_ms = (time.monotonic() - sent_at) * 1000
                peer.ping_rtt_ms = rtt_ms
                self.bus.emit_nowait("ping.pong", peer=peer, rtt_ms=rtt_ms)

        elif t == CoreMsg.GOODBYE:
            self.bus.emit_nowait(PEER_DISCONNECTED, peer=peer, reason="goodbye")


# ── Chat feature ──────────────────────────────────────────────────────────────

class ChatFeature(FeatureBase):
    """
    UTF-8 text messaging with application-level delivery ACK.

    Emits:
        chat.received  (peer, sender, text, msg_id)
        chat.acked     (peer, msg_id)
    """

    FEATURE_ID = FeatureID.CHAT

    async def on_message(self, peer: "Peer", msg: Message) -> None:
        if msg.msg_type == ChatMsg.TEXT:
            text = msg.payload.decode("utf-8", errors="replace")
            peer.record_rx()
            self.bus.emit_nowait(
                CHAT_RECEIVED,
                peer    = peer,
                sender  = peer.name or peer.mac,
                text    = text,
                msg_id  = msg.msg_id,
            )
            # Send delivery ACK
            await self._send(peer, ChatMsg.ACK, bytes([msg.msg_id]))

        elif msg.msg_type == ChatMsg.ACK:
            if msg.payload:
                self.bus.emit_nowait(CHAT_ACKED, peer=peer, msg_id=msg.payload[0])

        elif msg.msg_type == ChatMsg.TYPING_ON:
            self.bus.emit_nowait("chat.typing", peer=peer, typing=True)

        elif msg.msg_type == ChatMsg.TYPING_OFF:
            self.bus.emit_nowait("chat.typing", peer=peer, typing=False)

    async def send_text(self, peer: "Peer", text: str) -> int:
        """Send a text message to one peer.  Returns the msg_id."""
        return await self._send(peer, ChatMsg.TEXT, text)

    async def send_typing(self, peer: "Peer", typing: bool) -> None:
        t = ChatMsg.TYPING_ON if typing else ChatMsg.TYPING_OFF
        await self._send(peer, t, b"")