"""
protocol.py — Fragmentation, reassembly, and deduplication engine.

This module is the protocol brain: it turns arbitrary-length payloads into
BLE-sized frames (Protocol.fragment), and turns incoming frames back into
complete logical messages (Protocol.process_incoming).

Thread-safety: All public methods are safe to call from asyncio coroutines.
The internal state (seq counter, fragment buffers, seen-set) must NOT be
accessed concurrently without the caller's own lock — the Node does this.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from .constants import (
    MAX_PAYLOAD_PER_FRAG, FRAG_TIMEOUT_S, MsgFlags,
)
from .message import Message, MessageHeader, build_frame, parse_frame

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Fragment Reassembly Buffer
# ─────────────────────────────────────────────────────────────
class _FragmentGroup:
    """Holds the pieces of one multi-fragment message until complete."""

    __slots__ = ("total", "pieces", "first_msg", "updated_at")

    def __init__(self, total: int, first_msg: Message):
        self.total      : int                = total
        self.pieces     : Dict[int, bytes]   = {}
        self.first_msg  : Message            = first_msg
        self.updated_at : float              = time.monotonic()

    def add(self, frag_idx: int, payload: bytes) -> bool:
        self.pieces[frag_idx] = payload
        self.updated_at = time.monotonic()
        return len(self.pieces) == self.total

    def reassemble(self) -> bytes:
        return b"".join(self.pieces[i] for i in range(self.total))

    @property
    def is_expired(self) -> bool:
        return (time.monotonic() - self.updated_at) > FRAG_TIMEOUT_S


class FragmentBuffer:
    """
    Reassembles fragmented messages from arbitrary arrival order.

    Key: (src_id: bytes, frag_id: int)
    """

    def __init__(self):
        self._groups: Dict[Tuple[bytes, int], _FragmentGroup] = {}

    def add(self, msg: Message) -> Optional[Tuple[Message, bytes]]:
        """
        Add one fragment.  Returns (first_fragment_msg, complete_payload) when
        the message is fully reassembled, else None.
        """
        h    = msg.header
        key  = (h.src_id, h.frag_id)
        grp  = self._groups.get(key)

        if grp is None:
            grp = _FragmentGroup(h.frag_total, msg)
            self._groups[key] = grp

        complete = grp.add(h.frag_idx, msg.payload)
        if complete:
            payload = grp.reassemble()
            first   = grp.first_msg
            del self._groups[key]
            return (first, payload)
        return None

    def expire(self):
        """Remove stale groups.  Call periodically (e.g., every 30 s)."""
        stale = [k for k, g in self._groups.items() if g.is_expired]
        for k in stale:
            log.debug("Dropping expired fragment group %s / frag_id=%d", k[0].hex(), k[1])
            del self._groups[k]

    def __len__(self) -> int:
        return len(self._groups)


# ─────────────────────────────────────────────────────────────
# Deduplication Window
# ─────────────────────────────────────────────────────────────
class _SeenSet:
    """
    LRU-bounded set of (src_id, seq, frag_id) tuples.

    Prevents the same logical message being dispatched twice even if the peer
    retransmits (e.g. no ACK received before timeout).
    """

    def __init__(self, maxsize: int = 8192):
        self._data: OrderedDict = OrderedDict()
        self._max  = maxsize

    def add(self, key: tuple) -> bool:
        """Returns True if key is *new* (not a duplicate)."""
        if key in self._data:
            self._data.move_to_end(key)
            return False
        self._data[key] = None
        if len(self._data) > self._max:
            self._data.popitem(last=False)
        return True


# ─────────────────────────────────────────────────────────────
# Public Protocol Engine
# ─────────────────────────────────────────────────────────────
class Protocol:
    """
    Stateful protocol engine tied to one local device.

    Usage
    -----
    proto = Protocol(device_id=my_8_byte_id)

    # Outbound: split a payload into BLE-sized frames
    frames: List[bytes] = proto.fragment(
        msg_type=MsgType.FEATURE,
        payload=json_bytes,
        dst_id=peer_device_id,
        flags=MsgFlags.REQUIRES_ACK,
    )

    # Inbound: feed raw BLE frame bytes
    result = proto.process_incoming(raw_bytes)
    if result:
        first_frag, complete_payload = result
        # complete_payload is the full logical message payload
        # first_frag.header has src_id, dst_id, msg_type, flags, seq…
    """

    def __init__(self, device_id: bytes):
        assert len(device_id) == 8, "device_id must be exactly 8 bytes"
        self.device_id = device_id
        self._seq      : int = 0      # rolls over at 65535
        self._frag_id  : int = 0      # rolls over at 65535
        self._frags    : FragmentBuffer = FragmentBuffer()
        self._seen     : _SeenSet       = _SeenSet()

    # ── Outbound ─────────────────────────────────────────────

    def fragment(
        self,
        msg_type : int,
        payload  : bytes,
        dst_id   : bytes,
        flags    : int = 0,
    ) -> List[bytes]:
        """
        Split *payload* into one or more BLE-MTU-sized frames.

        Every frame is a self-contained, CRC-signed binary blob ready to be
        written to a GATT characteristic.  Multi-fragment messages share the
        same frag_id so the remote end can reassemble them.

        Returns a list of byte strings; always at least one element.
        """
        if not payload:
            payload = b""

        seq     = self._next_seq()
        frag_id = self._next_frag_id()
        ts      = int(time.time() * 1000)

        # Chunk the payload
        chunks: List[bytes] = []
        for i in range(0, max(len(payload), 1), MAX_PAYLOAD_PER_FRAG):
            chunks.append(payload[i : i + MAX_PAYLOAD_PER_FRAG])
        if not chunks:
            chunks = [b""]

        total   = len(chunks)
        frames  = []
        for idx, chunk in enumerate(chunks):
            frame = build_frame(
                msg_type=msg_type, payload=chunk,
                src_id=self.device_id, dst_id=dst_id,
                flags=flags, seq=seq,
                frag_id=frag_id, frag_total=total, frag_idx=idx,
                timestamp=ts,
            )
            frames.append(frame)

        log.debug(
            "Fragmented %d bytes → %d frame(s) [seq=%d, frag_id=%d, type=%#x]",
            len(payload), total, seq, frag_id, msg_type,
        )
        return frames

    # ── Inbound ──────────────────────────────────────────────

    def process_incoming(
        self, raw: bytes
    ) -> Optional[Tuple[Message, bytes]]:
        """
        Parse one raw BLE frame and attempt reassembly.

        Returns
        -------
        (first_fragment_msg, complete_payload)
            when a full logical message is available.
        None
            if the frame is malformed, a duplicate, or more fragments are
            still expected.
        """
        msg = parse_frame(raw)
        if msg is None:
            return None

        h   = msg.header
        key = (h.src_id, h.seq, h.frag_id)

        if h.frag_total == 1:
            # Single-frame message — fast path
            if not self._seen.add(key):
                log.debug("Duplicate single-frame message from %s (seq=%d)", h.src_hex, h.seq)
                return None
            log.debug(
                "Received single-frame msg type=%#x from %s (%d B)",
                h.msg_type, h.src_hex, len(msg.payload),
            )
            return (msg, msg.payload)

        # Multi-frame: buffer until all pieces arrive
        result = self._frags.add(msg)
        if result is None:
            log.debug(
                "Buffering fragment %d/%d for frag_id=%d from %s",
                h.frag_idx + 1, h.frag_total, h.frag_id, h.src_hex,
            )
            return None

        first_frag, complete = result
        if not self._seen.add(key):
            log.debug("Duplicate reassembled message from %s (seq=%d)", h.src_hex, h.seq)
            return None

        log.debug(
            "Reassembled %d-fragment message (%d B) from %s",
            h.frag_total, len(complete), h.src_hex,
        )
        return (first_frag, complete)

    def expire_fragments(self):
        """Prune stale fragment groups.  Call from the node's cleanup task."""
        self._frags.expire()

    # ── Counters ─────────────────────────────────────────────

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def _next_frag_id(self) -> int:
        self._frag_id = (self._frag_id + 1) & 0xFFFF
        return self._frag_id