"""
features/group_chat.py — Broadcast group / channel messaging.

Wire formats
------------
GROUP_JOIN / GROUP_LEAVE payload::

    [group_id_len: 1B][group_id: UTF-8]

GROUP_MSG payload::

    [group_id_len: 1B][group_id: UTF-8][msg_id: 4B uint32][body: UTF-8]

A "group" is just a named UTF-8 string (channel name).  The owning node
announces membership by broadcasting GROUP_JOIN packets; peers track who
belongs to which group.  When a node sends a GROUP_MSG it floods to
BROADCAST_ADDR — every node with TTL still alive receives it and only those
that have joined the group fire the inbound callbacks.

Usage::

    chat = GroupChat()
    node.register_feature(chat)

    await chat.join("general")

    @chat.on_message("general")
    async def handler(group_id, src_id, text, msg_id):
        print(f"[{group_id}] {src_id.hex()[:8]}: {text}")

    await chat.send("general", "Hello everyone!")
"""

from __future__ import annotations

import asyncio
import struct
from collections import defaultdict
from typing import Awaitable, Callable, Dict, List, Optional, Set

from ..core.packet import Packet
from ..core.protocol import BROADCAST_ADDR, MsgType
from ..utils.logger import log
from .base import Feature

# Callback: (group_id, src_id, text, msg_id) → None/Awaitable
GroupMsgCallback = Callable[[str, bytes, str, int], Awaitable[None]]

_MSG_ID_FMT  = "!I"
_MSG_ID_SIZE = struct.calcsize(_MSG_ID_FMT)


def _encode_group(group_id: str) -> bytes:
    g = group_id.encode("utf-8")[:255]
    return bytes([len(g)]) + g


def _decode_group(payload: bytes) -> tuple[str, bytes]:
    """Returns (group_id, remaining_payload)."""
    if not payload:
        return "", b""
    glen = payload[0]
    return payload[1: 1 + glen].decode("utf-8", errors="replace"), payload[1 + glen:]


class GroupChat(Feature):
    """Named-channel broadcast messaging.

    Groups are ephemeral — membership is tracked in memory and re-announced
    on each HEARTBEAT / node restart.
    """

    handled_types = frozenset({MsgType.GROUP_JOIN, MsgType.GROUP_LEAVE, MsgType.GROUP_MSG})
    name = "group-chat"

    def __init__(self) -> None:
        super().__init__()
        # Groups this node has joined
        self._my_groups: Set[str] = set()
        # Known membership: group_id → set of node_ids
        self._members:  Dict[str, Set[bytes]] = defaultdict(set)
        # Per-group callbacks
        self._callbacks: Dict[str, List[GroupMsgCallback]] = defaultdict(list)
        self._msg_counter = 0

    # ── Public API ────────────────────────────────────────────────────────────

    async def join(self, group_id: str) -> None:
        """Join (or re-announce membership in) a group."""
        self._my_groups.add(group_id)
        self._members[group_id].add(self.node.node_id)
        await self.node.send(
            MsgType.GROUP_JOIN,
            _encode_group(group_id),
            dst_id=BROADCAST_ADDR,
        )
        log.info("[GroupChat] Joined group '%s'.", group_id)

    async def leave(self, group_id: str) -> None:
        """Leave a group and notify peers."""
        self._my_groups.discard(group_id)
        self._members[group_id].discard(self.node.node_id)
        await self.node.send(
            MsgType.GROUP_LEAVE,
            _encode_group(group_id),
            dst_id=BROADCAST_ADDR,
        )
        log.info("[GroupChat] Left group '%s'.", group_id)

    async def send(self, group_id: str, text: str | bytes, encoding: str = "utf-8") -> bool:
        """Send a message to *group_id*.  Returns False if not a member."""
        if group_id not in self._my_groups:
            log.warning("[GroupChat] Cannot send to '%s': not a member.", group_id)
            return False
        body    = text.encode(encoding) if isinstance(text, str) else text
        msg_id  = self._next_msg_id()
        payload = _encode_group(group_id) + struct.pack(_MSG_ID_FMT, msg_id) + body
        return await self.node.send(MsgType.GROUP_MSG, payload, dst_id=BROADCAST_ADDR)

    def on_message(self, group_id: str) -> Callable:
        """Decorator that registers a callback for messages in *group_id*."""
        def decorator(fn: GroupMsgCallback) -> GroupMsgCallback:
            self._callbacks[group_id].append(fn)
            return fn
        return decorator

    def on_any_message(self, fn: GroupMsgCallback) -> GroupMsgCallback:
        """Register a callback fired for messages in *any* joined group."""
        for g in self._my_groups:
            self._callbacks[g].append(fn)
        self._callbacks["*"].append(fn)
        return fn

    # ── Membership queries ────────────────────────────────────────────────────

    @property
    def joined_groups(self) -> List[str]:
        return list(self._my_groups)

    def members_of(self, group_id: str) -> List[bytes]:
        return list(self._members.get(group_id, []))

    # ── Feature interface ─────────────────────────────────────────────────────

    async def handle(self, packet: Packet) -> None:
        if packet.msg_type == MsgType.GROUP_JOIN:
            group_id, _ = _decode_group(packet.payload)
            if group_id:
                self._members[group_id].add(packet.src_id)
                log.debug("[GroupChat] %s joined '%s'.", packet.src_id.hex()[:8], group_id)

        elif packet.msg_type == MsgType.GROUP_LEAVE:
            group_id, _ = _decode_group(packet.payload)
            if group_id:
                self._members[group_id].discard(packet.src_id)
                log.debug("[GroupChat] %s left '%s'.", packet.src_id.hex()[:8], group_id)

        elif packet.msg_type == MsgType.GROUP_MSG:
            await self._handle_group_message(packet)

    async def _handle_group_message(self, packet: Packet) -> None:
        group_id, rest = _decode_group(packet.payload)
        if not group_id or len(rest) < _MSG_ID_SIZE:
            return

        msg_id = struct.unpack(_MSG_ID_FMT, rest[:_MSG_ID_SIZE])[0]
        body   = rest[_MSG_ID_SIZE:]
        text   = body.decode("utf-8", errors="replace")

        log.info("[GroupChat] [%s] %s: %s", group_id, packet.src_id.hex()[:8], text[:60])

        cbs = self._callbacks.get(group_id, []) + self._callbacks.get("*", [])
        for cb in cbs:
            try:
                result = cb(group_id, packet.src_id, text, msg_id)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                log.warning("[GroupChat] Callback error: %s", exc)

    async def on_start(self) -> None:
        # Re-announce membership after (re)start
        for g in list(self._my_groups):
            await self.join(g)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _next_msg_id(self) -> int:
        self._msg_counter = (self._msg_counter + 1) & 0xFFFFFFFF
        return self._msg_counter
