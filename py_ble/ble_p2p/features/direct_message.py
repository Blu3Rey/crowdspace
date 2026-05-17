"""
features/direct_message.py — Point-to-point direct messaging.

Wire payload (after feature_id byte)
--------------------------------------
JSON: {"text": "...", "ts": <unix_ms_int>, "mid": "<msg_id_str>"}

Usage
-----
dm = DirectMessageFeature(node)
node.register_feature(dm)

dm.on_message(my_async_handler)   # handler(from_name, from_hex, text, ts_ms)
await dm.send(peer_device_id_bytes, "Hello!")
"""

from __future__ import annotations

import json
import logging
import time
from typing import Awaitable, Callable, List, Optional

from ..constants import FeatureID, MsgType, MsgFlags
from .base import Feature

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, str, str, int], Awaitable[None]]


class DirectMessageFeature(Feature):
    """
    Reliable point-to-point text messaging.

    Features
    --------
    • Delivers text to a specific device_id.
    • Sets REQUIRES_ACK so the node retries on no-ACK.
    • Calls registered handlers on receipt.
    • Maintains an in-memory conversation history per peer.
    """

    feature_id = FeatureID.DIRECT_MESSAGE

    def __init__(self, node):
        super().__init__(node)
        self._handlers : List[MessageHandler]         = []
        self._history  : dict                         = {}   # peer_hex → [(ts, dir, text)]

    # ── Public API ────────────────────────────────────────────

    def on_message(self, callback: MessageHandler) -> None:
        """
        Register a callback invoked when a direct message arrives.

        Signature
        ---------
        async def handler(from_name: str, from_id_hex: str,
                          text: str, timestamp_ms: int) -> None
        """
        self._handlers.append(callback)

    async def send(self, dst_id: bytes, text: str) -> bool:
        """
        Send *text* to the device identified by *dst_id*.

        Returns True if the message was accepted into the outbound queue.
        Actual delivery depends on the peer being reachable.
        """
        body = json.dumps(
            {
                "text": text,
                "ts"  : int(time.time() * 1000),
                "mid" : f"{int(time.time()*1e6):x}",
            },
            separators=(",", ":"),
        ).encode()

        payload = self.encode_payload(body)

        ok = await self.node.send_message(
            msg_type = MsgType.FEATURE,
            payload  = payload,
            dst_id   = dst_id,
            flags    = int(MsgFlags.REQUIRES_ACK),
        )
        if ok:
            self._record_history(dst_id.hex(), "out", text, int(time.time() * 1000))
        return ok

    async def broadcast(self, text: str) -> bool:
        """Send *text* to all reachable peers."""
        from ..constants import BROADCAST_ID
        body = json.dumps(
            {"text": text, "ts": int(time.time() * 1000), "mid": f"{int(time.time()*1e6):x}"},
            separators=(",", ":"),
        ).encode()
        payload = self.encode_payload(body)
        return await self.node.send_message(
            msg_type = MsgType.FEATURE,
            payload  = payload,
            dst_id   = BROADCAST_ID,
            flags    = int(MsgFlags.BROADCAST),
        )

    def get_history(
        self, peer_id_hex: str, limit: int = 50
    ) -> List[dict]:
        """
        Return conversation history with a peer.
        Each entry: {"ts": int, "dir": "in"|"out", "text": str}
        """
        return self._history.get(peer_id_hex, [])[-limit:]

    def clear_history(self, peer_id_hex: Optional[str] = None):
        if peer_id_hex:
            self._history.pop(peer_id_hex, None)
        else:
            self._history.clear()

    # ── Feature interface ─────────────────────────────────────

    async def handle_message(self, body: bytes, src_id: bytes, src_name: str) -> None:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.warning("DM: malformed JSON from %s", src_id.hex())
            return

        text  = data.get("text", "")
        ts    = data.get("ts",   int(time.time() * 1000))
        src_hex = src_id.hex()

        self._record_history(src_hex, "in", text, ts)
        log.info("DM from %s: %r", src_name, text[:80])

        for cb in self._handlers:
            try:
                await cb(src_name, src_hex, text, ts)
            except Exception as exc:
                log.exception("DM handler raised: %s", exc)

    # ── Internal ──────────────────────────────────────────────

    def _record_history(self, peer_hex: str, direction: str, text: str, ts: int):
        if peer_hex not in self._history:
            self._history[peer_hex] = []
        self._history[peer_hex].append({"ts": ts, "dir": direction, "text": text})
        # Cap history at 1000 messages per peer
        if len(self._history[peer_hex]) > 1000:
            self._history[peer_hex] = self._history[peer_hex][-1000:]