"""
features/group.py — Multi-device group chat.

Groups are identified by a uint16 group_id.  Any connected peer can create
or join a group.  Messages sent to a group are broadcast to all peers that
the local node knows are members.

Protocol:
    GROUP_JOIN    → "I am joining group G"
    GROUP_LEAVE   → "I am leaving group G"
    GROUP_MESSAGE → text message to group G

Membership model:
    The local node tracks which of its directly-connected peers are in which
    groups.  This is NOT a network-wide membership list — for that, combine
    GroupFeature with MeshFeature (broadcast GROUP_JOIN through the mesh).

Extension points:
    • Persist membership via a storage backend
    • Add GROUP_LIST (query all groups a peer belongs to)
    • Combine with MeshFeature for network-wide group broadcast
    • Add encryption per group_id (DH key exchange in GROUP_JOIN payload)
"""

from __future__ import annotations

import logging
import struct
from collections import defaultdict
from typing import TYPE_CHECKING

from ..constants import BROADCAST_ADDR
from ..events import EventBus, GROUP_MESSAGE
from ..protocol import FeatureID, Message, PacketFlags
from . import FeatureBase

if TYPE_CHECKING:
    from ..connection.manager import ConnectionManager
    from ..connection.peer import Peer

log = logging.getLogger(__name__)


class GroupMsg:
    JOIN    = 0x01
    LEAVE   = 0x02
    MESSAGE = 0x03
    LIST    = 0x04   # request peer to send its group memberships


class GroupFeature(FeatureBase):
    """
    Group chat that works across multiple simultaneously connected peers.

    Local groups:    groups this node has joined
    Peer groups:     which peers are in which groups (learned from JOIN messages)

    Sending to a group:
        Fan-out to all peers that are known members of that group.
        If no peers have joined yet, the message is still broadcast to all peers
        (they will ignore it if not in the group — or join first).
    """

    FEATURE_ID = FeatureID.GROUP

    def __init__(self, bus: EventBus, conn: "ConnectionManager", my_name: str):
        super().__init__(bus, conn)
        self._my_name   = my_name
        self._my_groups: set[int]                      = set()
        self._peer_groups: dict[str, set[int]]         = defaultdict(set)  # mac → group_ids
        self._group_members: dict[int, set[str]]       = defaultdict(set)  # group_id → macs

    # ── Public API ────────────────────────────────────────────────────────────

    async def join(self, group_id: int) -> None:
        """Join a group and notify all connected peers."""
        self._my_groups.add(group_id)
        payload = struct.pack(">H", group_id) + self._my_name.encode()
        await self._broadcast(GroupMsg.JOIN, payload,
                              flags=PacketFlags.HAS_GROUP, group_id=group_id)
        log.info(f"Joined group 0x{group_id:04X}")

    async def leave(self, group_id: int) -> None:
        """Leave a group and notify all connected peers."""
        self._my_groups.discard(group_id)
        payload = struct.pack(">H", group_id)
        await self._broadcast(GroupMsg.LEAVE, payload,
                              flags=PacketFlags.HAS_GROUP, group_id=group_id)
        log.info(f"Left group 0x{group_id:04X}")

    async def send_message(self, group_id: int, text: str) -> None:
        """
        Send a text message to a group.  Only peers known to be in the group
        receive it (falls back to all peers if membership is unknown).
        """
        if group_id not in self._my_groups:
            raise ValueError(f"Not a member of group 0x{group_id:04X} — call join() first")

        members = self._group_members.get(group_id, set())
        payload = struct.pack(">H", group_id) + text.encode("utf-8")

        if members:
            for mac in members:
                peer = self.conn.get_peer(mac)
                if peer:
                    await self._send(peer, GroupMsg.MESSAGE, payload,
                                     flags=PacketFlags.HAS_GROUP, group_id=group_id)
        else:
            # No known members — broadcast (they'll ignore if not in group)
            await self._broadcast(GroupMsg.MESSAGE, payload,
                                  flags=PacketFlags.HAS_GROUP, group_id=group_id)

        # Emit locally too so the UI shows it
        self.bus.emit_nowait(
            GROUP_MESSAGE,
            group_id = group_id,
            sender   = self._my_name,
            text     = text,
        )

    # ── Inbound ───────────────────────────────────────────────────────────────

    async def on_message(self, peer: "Peer", msg: Message) -> None:
        group_id = msg.group_id

        if msg.msg_type == GroupMsg.JOIN:
            if len(msg.payload) >= 2:
                (gid,) = struct.unpack_from(">H", msg.payload)
                name   = msg.payload[2:].decode("utf-8", errors="replace") or peer.name or peer.mac
                self._peer_groups[peer.mac].add(gid)
                self._group_members[gid].add(peer.mac)
                log.info(f"{name} joined group 0x{gid:04X}")
                self.bus.emit_nowait("group.join", peer=peer, group_id=gid, name=name)

        elif msg.msg_type == GroupMsg.LEAVE:
            if len(msg.payload) >= 2:
                (gid,) = struct.unpack_from(">H", msg.payload)
                self._peer_groups[peer.mac].discard(gid)
                self._group_members[gid].discard(peer.mac)
                log.info(f"{peer.name or peer.mac} left group 0x{gid:04X}")
                self.bus.emit_nowait("group.leave", peer=peer, group_id=gid)

        elif msg.msg_type == GroupMsg.MESSAGE:
            if len(msg.payload) >= 2:
                (gid,) = struct.unpack_from(">H", msg.payload)
                if gid not in self._my_groups:
                    return  # not a member, ignore
                text   = msg.payload[2:].decode("utf-8", errors="replace")
                sender = peer.name or peer.mac
                log.info(f"[Group 0x{gid:04X}] {sender}: {text}")
                self.bus.emit_nowait(
                    GROUP_MESSAGE,
                    group_id = gid,
                    sender   = sender,
                    text     = text,
                )

    # ── Accessors ─────────────────────────────────────────────────────────────

    def my_groups(self) -> list[int]:
        return sorted(self._my_groups)

    def group_members(self, group_id: int) -> list[str]:
        return sorted(self._group_members.get(group_id, set()))