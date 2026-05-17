"""
network/router.py — Routes fully-reassembled messages to the correct handler.

The Router knows nothing about BLE; it operates on complete logical messages
produced by Protocol.process_incoming().  Feature modules register themselves
here.  The Node feeds incoming messages in via Router.route().
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Dict, Optional

from ..constants import MsgType, MsgFlags, FeatureID, BROADCAST_ID
from ..message import Message

log = logging.getLogger(__name__)

# Type aliases
MsgHandler = Callable[[bytes, bytes, str], Awaitable[None]]  # (payload, src_id, src_name)


class Router:
    """
    Central dispatch table for the node.

    Registration
    ------------
    router.register_feature(feature_instance)
        — for FeatureID-namespaced FEATURE messages

    router.register_handler(MsgType.DATA, my_async_fn)
        — for lower-level message types (DATA, ROUTE, …)

    Dispatch
    --------
    await router.route(msg, complete_payload, peer_address)
    """

    def __init__(self, local_device_id: bytes, local_name: str):
        self._local_id   : bytes                   = local_device_id
        self._local_name : str                     = local_name
        self._features   : Dict[int, object]       = {}   # FeatureID → Feature
        self._handlers   : Dict[int, MsgHandler]   = {}   # MsgType   → coro fn
        self._ack_waiters: Dict[str, asyncio.Future] = {}

        # Hooks the Node can register for cross-cutting concerns
        self.on_handshake   : Optional[Callable] = None  # (data_dict, src_id, addr)
        self.on_ack_needed  : Optional[Callable] = None  # (src_id, seq, frag_id)
        self.on_send_pong   : Optional[Callable] = None  # (src_id)

    # ── Registration ─────────────────────────────────────────

    def register_feature(self, feature) -> None:
        """Register a Feature subclass instance.  Its feature_id must be unique."""
        fid = feature.feature_id
        if fid in self._features:
            raise ValueError(f"Feature ID {fid:#x} already registered")
        self._features[fid] = feature
        log.info("Registered feature %s (ID=%#x)", type(feature).__name__, fid)

    def register_handler(self, msg_type: int, handler: MsgHandler) -> None:
        self._handlers[msg_type] = handler

    def get_feature(self, feature_id: int) -> Optional[object]:
        return self._features.get(feature_id)

    def get_all_features(self) -> Dict[int, object]:
        return dict(self._features)

    # ── ACK Waiter management ─────────────────────────────────

    def create_ack_waiter(self, message_id: str, loop: asyncio.AbstractEventLoop) -> asyncio.Future:
        fut = loop.create_future()
        self._ack_waiters[message_id] = fut
        return fut

    def resolve_ack(self, message_id: str):
        fut = self._ack_waiters.pop(message_id, None)
        if fut and not fut.done():
            fut.set_result(True)

    # ── Core dispatch ─────────────────────────────────────────

    async def route(
        self,
        msg              : Message,
        complete_payload : bytes,
        source_address   : str,
        peer_name_lookup : Callable[[bytes], str],
    ) -> None:
        """
        Dispatch a fully-reassembled message to the appropriate handler.

        Parameters
        ----------
        msg              : the first-fragment Message (carries the header)
        complete_payload : the reassembled logical payload (may be multi-frag)
        source_address   : BLE MAC address of the sender
        peer_name_lookup : callable(device_id bytes) → display name str
        """
        h = msg.header

        # ── Destination check ────────────────────────────────
        is_broadcast = (h.dst_id == BROADCAST_ID) or bool(h.flags & MsgFlags.BROADCAST)
        is_for_us    = (h.dst_id == self._local_id) or is_broadcast

        if not is_for_us:
            # Relay if the sender asked for it
            if h.flags & MsgFlags.RELAY:
                log.debug(
                    "Relay requested: src=%s dst=%s — queuing for relay",
                    h.src_hex, h.dst_hex,
                )
                # TODO: enqueue in Node's outbound queue with RELAY flag cleared
            return

        src_name = peer_name_lookup(h.src_id)

        # ── ACK if required ──────────────────────────────────
        if h.requires_ack and self.on_ack_needed:
            await self.on_ack_needed(h.src_id, h.seq, h.frag_id)

        # ── Dispatch by message type ──────────────────────────
        mt = h.msg_type

        if mt == MsgType.HANDSHAKE:
            await self._handle_handshake(complete_payload, h.src_id, source_address)

        elif mt == MsgType.ACK:
            self._handle_ack(complete_payload)

        elif mt == MsgType.PING:
            log.debug("PING from %s", src_name)
            if self.on_send_pong:
                await self.on_send_pong(h.src_id)

        elif mt == MsgType.PONG:
            log.debug("PONG from %s", src_name)

        elif mt == MsgType.FEATURE:
            await self._dispatch_feature(complete_payload, h.src_id, src_name)

        elif mt == MsgType.DATA:
            handler = self._handlers.get(MsgType.DATA)
            if handler:
                await handler(complete_payload, h.src_id, src_name)
            else:
                log.debug("No handler for DATA from %s", src_name)

        elif mt == MsgType.ROUTE:
            handler = self._handlers.get(MsgType.ROUTE)
            if handler:
                await handler(complete_payload, h.src_id, src_name)

        elif mt == MsgType.ERROR:
            log.warning("ERROR message from %s: %r", src_name, complete_payload[:64])

        else:
            log.debug("Unknown msg_type=%#x from %s", mt, src_name)

    # ── Internal handlers ─────────────────────────────────────

    async def _handle_handshake(self, payload: bytes, src_id: bytes, address: str):
        try:
            data = json.loads(payload)
            if self.on_handshake:
                await self.on_handshake(data, src_id, address)
        except (json.JSONDecodeError, Exception) as exc:
            log.warning("Malformed HANDSHAKE from %s: %s", src_id.hex(), exc)

    def _handle_ack(self, payload: bytes):
        try:
            data     = json.loads(payload)
            msg_id   = data.get("id", "")
            self.resolve_ack(msg_id)
            log.debug("ACK received for msg_id=%s", msg_id)
        except Exception:
            pass

    async def _dispatch_feature(self, payload: bytes, src_id: bytes, src_name: str):
        if not payload:
            return
        feature_id = payload[0]
        body       = payload[1:]

        feature = self._features.get(feature_id)
        if feature is None:
            log.debug("No feature handler for feature_id=%#x", feature_id)
            return

        log.debug(
            "Dispatching FEATURE id=%#x from %s (%d B payload)",
            feature_id, src_name, len(body),
        )
        try:
            await feature.handle_message(body, src_id, src_name)
        except Exception as exc:
            log.exception("Feature %#x raised: %s", feature_id, exc)