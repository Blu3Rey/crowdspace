"""
features/group_chat.py — Multi-party group chat.

Wire payload (after feature_id byte)
--------------------------------------
JSON:
{
  "op"    : "msg" | "join" | "leave" | "sync",
  "gid"   : "<8-char group id>",
  "gname" : "<display name>",
  "text"  : "<message text>",          # op=msg only
  "members": ["hex1", "hex2", ...],    # op=sync
  "ts"    : <unix_ms_int>
}

Usage
-----
gc = GroupChatFeature(node)
node.register_feature(gc)

gc.on_message(my_handler)
gid = gc.create_group("TeamAlpha", [peer_id_bytes_1, peer_id_bytes_2])
await gc.send(gid, "Hello team!")
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Set

from ..constants import FeatureID, MsgType, MsgFlags, BROADCAST_ID
from .base import Feature

log = logging.getLogger(__name__)

GroupMessageHandler = Callable[[str, str, str, str, str, int], Awaitable[None]]
# (group_id, group_name, from_name, from_id_hex, text, ts_ms)


@dataclass
class Group:
    """In-memory representation of one chat group."""
    group_id : str
    name     : str
    members  : Set[str]              = field(default_factory=set)  # device_id hex strings
    created_at: float                = field(default_factory=time.time)

    @property
    def member_ids(self) -> List[bytes]:
        result = []
        for h in self.members:
            try:
                result.append(bytes.fromhex(h))
            except ValueError:
                pass
        return result


class GroupChatFeature(Feature):
    """
    Manages named groups and fans messages out to all member devices.

    Groups are stored in-memory; for persistence across restarts, call
    export_groups() / import_groups() and store the JSON yourself.
    """

    feature_id = FeatureID.GROUP_CHAT

    def __init__(self, node):
        super().__init__(node)
        self._groups  : Dict[str, Group]            = {}
        self._handlers: List[GroupMessageHandler]   = []

    # ── Public API ────────────────────────────────────────────

    def on_message(self, callback: GroupMessageHandler) -> None:
        """
        Register callback for incoming group messages.

        Signature
        ---------
        async def handler(
            group_id: str, group_name: str,
            from_name: str, from_id_hex: str,
            text: str, timestamp_ms: int
        ) -> None
        """
        self._handlers.append(callback)

    def create_group(
        self,
        name      : str,
        member_ids: List[bytes],   # list of 8-byte device IDs (not including self)
    ) -> str:
        """
        Create a new group locally and return its group_id.

        The group is not announced to peers until you call send() or sync().
        """
        gid = uuid.uuid4().hex[:8]
        members = {m.hex() for m in member_ids}
        members.add(self.node.device.id_hex)   # always include ourselves
        self._groups[gid] = Group(group_id=gid, name=name, members=members)
        log.info("Created group %r (id=%s, %d members)", name, gid, len(members))
        return gid

    def get_group(self, group_id: str) -> Optional[Group]:
        return self._groups.get(group_id)

    def list_groups(self) -> List[Group]:
        return list(self._groups.values())

    def add_member(self, group_id: str, device_id: bytes) -> bool:
        grp = self._groups.get(group_id)
        if grp is None:
            return False
        grp.members.add(device_id.hex())
        return True

    def remove_member(self, group_id: str, device_id: bytes) -> bool:
        grp = self._groups.get(group_id)
        if grp is None:
            return False
        grp.members.discard(device_id.hex())
        return True

    async def send(self, group_id: str, text: str) -> Dict[str, bool]:
        """
        Broadcast *text* to all non-self members of *group_id*.

        Returns a dict mapping member_id_hex → queued_ok.
        """
        grp = self._groups.get(group_id)
        if grp is None:
            log.warning("send: unknown group_id=%s", group_id)
            return {}

        body = json.dumps(
            {
                "op"   : "msg",
                "gid"  : group_id,
                "gname": grp.name,
                "text" : text,
                "ts"   : int(time.time() * 1000),
            },
            separators=(",", ":"),
        ).encode()
        payload = self.encode_payload(body)

        results: Dict[str, bool] = {}
        local_hex = self.node.device.id_hex

        for member_id in grp.member_ids:
            if member_id.hex() == local_hex:
                continue
            ok = await self.node.send_message(
                msg_type = MsgType.FEATURE,
                payload  = payload,
                dst_id   = member_id,
                flags    = int(MsgFlags.REQUIRES_ACK | MsgFlags.RELAY),
            )
            results[member_id.hex()] = ok

        return results

    async def sync_membership(self, group_id: str) -> bool:
        """
        Gossip current group membership to all members.
        Useful after adding/removing members.
        """
        grp = self._groups.get(group_id)
        if grp is None:
            return False

        body = json.dumps(
            {
                "op"     : "sync",
                "gid"    : group_id,
                "gname"  : grp.name,
                "members": list(grp.members),
                "ts"     : int(time.time() * 1000),
            },
            separators=(",", ":"),
        ).encode()
        payload = self.encode_payload(body)
        local_hex = self.node.device.id_hex

        for member_id in grp.member_ids:
            if member_id.hex() == local_hex:
                continue
            await self.node.send_message(
                msg_type = MsgType.FEATURE,
                payload  = payload,
                dst_id   = member_id,
                flags    = 0,
            )
        return True

    # ── Persistence helpers ───────────────────────────────────

    def export_groups(self) -> str:
        """Serialise all groups to JSON string for external storage."""
        data = {
            gid: {"name": g.name, "members": list(g.members)}
            for gid, g in self._groups.items()
        }
        return json.dumps(data)

    def import_groups(self, json_str: str):
        """Restore groups from a previously exported JSON string."""
        data = json.loads(json_str)
        for gid, info in data.items():
            self._groups[gid] = Group(
                group_id=gid,
                name=info["name"],
                members=set(info["members"]),
            )
        log.info("Imported %d group(s)", len(data))

    # ── Feature interface ─────────────────────────────────────

    async def handle_message(self, body: bytes, src_id: bytes, src_name: str) -> None:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.warning("GroupChat: malformed JSON from %s", src_id.hex())
            return

        op       = data.get("op", "msg")
        group_id = data.get("gid", "")
        gname    = data.get("gname", "Unknown")
        ts       = data.get("ts", int(time.time() * 1000))

        if op == "sync":
            # Update local membership table
            remote_members = set(data.get("members", []))
            if group_id not in self._groups:
                self._groups[group_id] = Group(
                    group_id=group_id, name=gname, members=remote_members,
                )
                log.info("Learned new group %r (id=%s) via sync", gname, group_id)
            else:
                self._groups[group_id].members |= remote_members
            return

        if op == "join":
            if group_id in self._groups:
                self._groups[group_id].members.add(src_id.hex())
            return

        if op == "leave":
            if group_id in self._groups:
                self._groups[group_id].members.discard(src_id.hex())
            return

        # op == "msg"
        text = data.get("text", "")

        # Auto-learn group if not known
        if group_id not in self._groups:
            self._groups[group_id] = Group(
                group_id=group_id, name=gname, members={src_id.hex()},
            )

        log.info("Group[%s] from %s: %r", gname, src_name, text[:80])

        for cb in self._handlers:
            try:
                await cb(group_id, gname, src_name, src_id.hex(), text, ts)
            except Exception as exc:
                log.exception("GroupChat handler raised: %s", exc)