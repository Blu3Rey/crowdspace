"""
features/group_chat.py
======================
Multicast group chat rooms.

Group messages are routed as GROUP_MSG packets with a group_id.
Each node that is a member of a group delivers the message locally.

Wire payload (JSON):
  {
    "v":      1,
    "type":   "msg" | "join" | "leave" | "invite" | "rename" | "meta",
    "gid":    12345,
    "gname":  "Crew",
    "id":     "<uuid>",
    "body":   "...",
    "ts":     1234567890.123,
    "sender": "AA:BB:CC:DD:EE:FF",
    "meta":   {}
  }
"""

from __future__ import annotations
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Awaitable, List, Dict, Optional, Set, Deque
from collections import deque

from .base import BaseFeature
from ..core.packet import Packet, PacketType, PacketFlag, BROADCAST_ADDR
from ..core.node import GroupRegistry, Group


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class GroupMessage:
    id:        str
    group_id:  int
    src_addr:  bytes
    body:      str
    timestamp: float = field(default_factory=time.time)
    msg_type:  str   = "msg"   # "msg"|"join"|"leave"|"system"
    meta:      dict  = field(default_factory=dict)

    @property
    def src_str(self) -> str:
        return ":".join(f"{b:02X}" for b in self.src_addr)

    def __str__(self) -> str:
        return f"[group:{self.group_id}] [{self.src_str}] : {self.body}"


GroupMessageHandler = Callable[[GroupMessage], Awaitable[None]]


# ── Group Chat Feature ────────────────────────────────────────────────────────

class GroupChatFeature(BaseFeature):
    """
    Named chat room management and messaging.

    Usage:
        gid = await group_chat.create_room("HQ Channel")
        await group_chat.join(gid)
        await group_chat.send_message(gid, "Hello everyone!")
        group_chat.on_message(gid, my_handler)
    """

    NAME    = "group_chat"
    HANDLES = {PacketType.GROUP_MSG}

    HISTORY_LIMIT = 200

    def __init__(self, node):
        super().__init__(node)
        self._registry:  GroupRegistry                           = node.group_registry
        self._my_groups: Set[int]                                = set()
        self._history:   Dict[int, Deque[GroupMessage]]         = {}
        self._handlers:  Dict[int, List[GroupMessageHandler]]   = {}
        self._global_handlers: List[GroupMessageHandler]        = []

    # ── Public API ────────────────────────────────────────────────────────────

    async def create_room(self, name: str, group_id: Optional[int] = None) -> int:
        """
        Create a new named chat room.

        Args:
            name:     Human-readable room name.
            group_id: Explicit ID (auto-generated if omitted).

        Returns:
            The numeric group ID.
        """
        gid = group_id if group_id is not None else self._gen_group_id(name)
        self._registry.create(gid, name, self.local_addr)
        await self.join(gid)
        # Announce to network
        await self._broadcast_meta(gid, name)
        return gid

    async def join(self, group_id: int) -> bool:
        """Join an existing group."""
        self._my_groups.add(group_id)
        self._registry.join(group_id, self.local_addr)
        self._history.setdefault(group_id, deque(maxlen=self.HISTORY_LIMIT))

        payload = self._encode(group_id, "join", body="", extra={"gname": self._gname(group_id)})
        pkt     = self.make_packet(
            PacketType.GROUP_MSG,
            payload  = payload,
            dst_addr = BROADCAST_ADDR,
            group_id = group_id,
            flags    = PacketFlag.NONE,
        )
        await self.send(pkt)
        self._log.info("Joined group %d (%s)", group_id, self._gname(group_id))
        return True

    async def leave(self, group_id: int):
        """Leave a group."""
        if group_id not in self._my_groups:
            return
        self._my_groups.discard(group_id)
        self._registry.leave(group_id, self.local_addr)

        payload = self._encode(group_id, "leave", body="")
        pkt     = self.make_packet(
            PacketType.GROUP_MSG,
            payload  = payload,
            dst_addr = BROADCAST_ADDR,
            group_id = group_id,
            flags    = PacketFlag.NONE,
        )
        await self.send(pkt)

    async def send_message(
        self,
        group_id: int,
        text:     str,
        *,
        meta:     dict  = None,
        ttl:      int   = 7,
    ) -> Optional[GroupMessage]:
        """Send a text message to a group."""
        if group_id not in self._my_groups:
            self._log.warning("Cannot send to group %d - not a member", group_id)
            return None

        gm = GroupMessage(
            id       = str(uuid.uuid4()),
            group_id = group_id,
            src_addr = self.local_addr,
            body     = text,
            meta     = meta or {},
        )
        payload = self._encode(group_id, "msg", body=text, msg_id=gm.id,
                               extra={"meta": gm.meta})
        pkt = self.make_packet(
            PacketType.GROUP_MSG,
            payload  = payload,
            dst_addr = BROADCAST_ADDR,
            group_id = group_id,
            flags    = PacketFlag.NONE,
            ttl      = ttl,
        )
        await self.send(pkt)
        self._store(gm)
        return gm

    async def invite(self, group_id: int, peer_addr: bytes):
        """
        Send a unicast invite to a peer.
        The peer's GroupChatFeature will auto-join on receipt.
        """
        from ..core.packet import PacketType as PT
        payload = self._encode(
            group_id, "invite", body="",
            extra={"gname": self._gname(group_id)}
        )
        pkt = self.make_packet(
            PacketType.GROUP_MSG,
            payload  = payload,
            dst_addr = peer_addr,
            group_id = group_id,
            flags    = PacketFlag.NONE,
        )
        await self.send(pkt)

    async def rename(self, group_id: int, new_name: str):
        """Rename a group (admin operation - no enforcement, cooperative)."""
        group = self._registry.get(group_id)
        if group:
            group.name = new_name
        await self._broadcast_meta(group_id, new_name)

    def on_message(
        self,
        handler:  GroupMessageHandler,
        group_id: Optional[int] = None,
    ):
        """
        Register a callback for incoming group messages.

        If group_id is None, the handler is called for ALL groups.
        """
        if group_id is None:
            self._global_handlers.append(handler)
        else:
            self._handlers.setdefault(group_id, []).append(handler)

    def history(self, group_id: int) -> List[GroupMessage]:
        return list(self._history.get(group_id, []))

    def my_groups(self) -> List[dict]:
        return [
            {
                "id":      gid,
                "name":    self._gname(gid),
                "members": [":".join(f"{b:02X}" for b in m)
                            for m in (self._registry.get(gid).members
                                      if self._registry.get(gid) else [])],
            }
            for gid in self._my_groups
        ]

    # ── Incoming packet handler ───────────────────────────────────────────────

    async def on_packet(self, pkt: Packet):
        try:
            data = json.loads(pkt.payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        gid      = data.get("gid", pkt.group_id)
        msg_type = data.get("type", "msg")

        if msg_type == "join":
            self._registry.join(gid, pkt.src_addr)
            self._log.info("%s joined group %d", pkt.src_addr.hex(), gid)
            gname = data.get("gname", "")
            if gid not in self._registry._groups and gname:
                self._registry.create(gid, gname, pkt.src_addr)
            system_msg = GroupMessage(
                id       = str(uuid.uuid4()),
                group_id = gid,
                src_addr = pkt.src_addr,
                body     = f"{self.addr_str(pkt.src_addr)} joined",
                msg_type = "system",
            )
            self._maybe_store_and_dispatch(system_msg, gid)

        elif msg_type == "leave":
            self._registry.leave(gid, pkt.src_addr)
            system_msg = GroupMessage(
                id       = str(uuid.uuid4()),
                group_id = gid,
                src_addr = pkt.src_addr,
                body     = f"{self.addr_str(pkt.src_addr)} left",
                msg_type = "system",
            )
            self._maybe_store_and_dispatch(system_msg, gid)

        elif msg_type == "invite":
            # Auto-join if invited
            if pkt.dst_addr == self.local_addr:
                gname = data.get("gname", f"group-{gid}")
                if gid not in self._registry._groups:
                    self._registry.create(gid, gname, pkt.src_addr)
                if gid not in self._my_groups:
                    await self.join(gid)

        elif msg_type == "meta":
            # Room metadata update
            gname = data.get("gname", "")
            if gname:
                g = self._registry.get(gid)
                if g:
                    g.name = gname

        elif msg_type == "msg":
            # Only deliver if we're a member
            if gid not in self._my_groups:
                return
            gm = GroupMessage(
                id       = data.get("id", str(uuid.uuid4())),
                group_id = gid,
                src_addr = pkt.src_addr,
                body     = data.get("body", ""),
                timestamp= data.get("ts", time.time()),
                meta     = data.get("meta", {}),
            )
            self._store(gm)
            await self._dispatch(gm)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _maybe_store_and_dispatch(self, gm: GroupMessage, gid: int):
        if gid in self._my_groups:
            self._store(gm)
            asyncio.ensure_future(self._dispatch(gm))

    async def _dispatch(self, gm: GroupMessage):
        for h in self._global_handlers + self._handlers.get(gm.group_id, []):
            try:
                await h(gm)
            except Exception as e:
                self._log.error("Handler error: %s", e)

    def _store(self, gm: GroupMessage):
        hist = self._history.setdefault(gm.group_id, deque(maxlen=self.HISTORY_LIMIT))
        hist.append(gm)

    def _gname(self, gid: int) -> str:
        g = self._registry.get(gid)
        return g.name if g else f"group-{gid}"

    def _encode(
        self,
        gid:    int,
        mtype:  str,
        body:   str,
        msg_id: str = "",
        extra:  dict = None,
    ) -> bytes:
        payload = {
            "v": 1, "type": mtype, "gid": gid,
            "id": msg_id or str(uuid.uuid4()),
            "body": body, "ts": time.time(),
            "sender": self.addr_str(self.local_addr),
        }
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def _broadcast_meta(self, gid: int, gname: str):
        payload = self._encode(gid, "meta", body="", extra={"gname": gname})
        pkt     = self.make_packet(
            PacketType.GROUP_MSG,
            payload  = payload,
            dst_addr = BROADCAST_ADDR,
            group_id = gid,
        )
        await self.send(pkt)

    @staticmethod
    def _gen_group_id(name: str) -> int:
        """Deterministic group ID from name + entropy."""
        import hashlib, os
        h = hashlib.sha256(name.encode() + os.urandom(8)).digest()
        return int.from_bytes(h[:4], "little")