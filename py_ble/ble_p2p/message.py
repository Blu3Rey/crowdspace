"""
message.py — Binary wire format for the BLE-P2P protocol.

Every byte on the wire is packed by this module. The layout is described in
constants.py. Nothing outside this module should call struct.pack/unpack
directly; use MessageHeader.pack() / MessageHeader.unpack() instead.
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Optional

from .constants import (
    PROTOCOL_MAGIC, PROTOCOL_VERSION,
    HEADER_STRUCT_FMT, HEADER_SIZE, CRC_SIZE,
    MsgType, MsgFlags, BROADCAST_ID,
)

# Pre-compile the struct for speed
_HEADER_STRUCT = struct.Struct(HEADER_STRUCT_FMT)
assert _HEADER_STRUCT.size == HEADER_SIZE, (
    f"HEADER_SIZE mismatch: expected {HEADER_SIZE}, got {_HEADER_STRUCT.size}"
)


def _crc16(data: bytes) -> int:
    """CRC-32 truncated to 16 bits — fast, good enough for BLE distances."""
    return zlib.crc32(data) & 0xFFFF


# ─────────────────────────────────────────────────────────────
# Wire Header
# ─────────────────────────────────────────────────────────────
@dataclass
class MessageHeader:
    """
    Represents the 37-byte fixed header that precedes every fragment on the wire.

    Fields
    ------
    msg_type    : MsgType (1 B)
    flags       : MsgFlags bitmask (1 B)
    seq         : per-source sequence number 0–65535 (2 B)
    frag_id     : groups fragments of the same logical message (2 B)
    frag_total  : total fragment count for this message, 1–255 (1 B)
    frag_idx    : zero-based fragment index (1 B)
    src_id      : 8-byte sender device ID
    dst_id      : 8-byte recipient device ID (BROADCAST_ID = all)
    timestamp   : Unix epoch milliseconds (8 B)
    payload_len : byte length of the payload in *this* fragment (2 B)
    """
    msg_type   : int
    flags      : int
    seq        : int
    frag_id    : int
    frag_total : int
    frag_idx   : int
    src_id     : bytes   # exactly 8 bytes
    dst_id     : bytes   # exactly 8 bytes
    timestamp  : int     # milliseconds
    payload_len: int

    # ── Serialisation ────────────────────────────────────────

    def pack(self) -> bytes:
        return _HEADER_STRUCT.pack(
            PROTOCOL_MAGIC,
            PROTOCOL_VERSION,
            int(self.msg_type),
            int(self.flags),
            self.seq,
            self.frag_id,
            self.frag_total,
            self.frag_idx,
            self.src_id[:8].ljust(8, b"\x00"),
            self.dst_id[:8].ljust(8, b"\x00"),
            self.timestamp,
            self.payload_len,
        )

    @classmethod
    def unpack(cls, data: bytes) -> Optional[MessageHeader]:
        """
        Parse the first HEADER_SIZE bytes of *data*.
        Returns None if the data is malformed, too short, or uses the wrong
        magic / version — makes it safe to call on arbitrary BLE payloads.
        """
        if len(data) < HEADER_SIZE:
            return None
        try:
            (magic, version, msg_type, flags, seq,
             frag_id, frag_total, frag_idx,
             src_id, dst_id, timestamp, payload_len) = _HEADER_STRUCT.unpack(
                data[:HEADER_SIZE]
            )
        except struct.error:
            return None

        if magic != PROTOCOL_MAGIC or version != PROTOCOL_VERSION:
            return None
        if frag_total == 0 or frag_idx >= frag_total:
            return None

        return cls(
            msg_type=msg_type, flags=flags, seq=seq,
            frag_id=frag_id, frag_total=frag_total, frag_idx=frag_idx,
            src_id=bytes(src_id), dst_id=bytes(dst_id),
            timestamp=timestamp, payload_len=payload_len,
        )

    # ── Helpers ──────────────────────────────────────────────

    @property
    def src_hex(self) -> str:
        return self.src_id.hex()

    @property
    def dst_hex(self) -> str:
        return self.dst_id.hex()

    @property
    def is_broadcast(self) -> bool:
        return self.dst_id == BROADCAST_ID

    @property
    def requires_ack(self) -> bool:
        return bool(self.flags & MsgFlags.REQUIRES_ACK)


# ─────────────────────────────────────────────────────────────
# Parsed In-Memory Message
# ─────────────────────────────────────────────────────────────
@dataclass
class Message:
    """
    A fully parsed, CRC-verified BLE frame.

    *payload* is the fragment payload, NOT the reassembled message body.
    Use FragmentBuffer in protocol.py to reconstruct multi-fragment messages.
    """
    header  : MessageHeader
    payload : bytes
    crc     : int   # as received on wire

    @property
    def is_valid(self) -> bool:
        expected = _crc16(self.header.pack() + self.payload)
        return expected == self.crc


# ─────────────────────────────────────────────────────────────
# Outbound Message (pre-fragmentation)
# ─────────────────────────────────────────────────────────────
@dataclass
class OutboundMessage:
    """
    Represents one logical message queued for delivery.

    The *node* converts this into one or more raw BLE frames via Protocol.fragment().
    """
    msg_type   : int
    payload    : bytes           # full logical payload (may be multi-fragment)
    dst_id     : bytes           # 8-byte device ID or BROADCAST_ID
    flags      : int = 0
    retries    : int = 0
    created_at : float = field(default_factory=time.monotonic)
    message_id : str  = field(default_factory=lambda: f"{int(time.time()*1e6):x}")

    @property
    def is_priority(self) -> bool:
        return bool(self.flags & MsgFlags.PRIORITY)

    @property
    def is_expired(self) -> bool:
        # Drop messages older than 5 minutes if never delivered
        return (time.monotonic() - self.created_at) > 300.0


# ─────────────────────────────────────────────────────────────
# Wire-frame builder (single fragment → bytes)
# ─────────────────────────────────────────────────────────────
def build_frame(
    msg_type   : int,
    payload    : bytes,
    src_id     : bytes,
    dst_id     : bytes,
    flags      : int,
    seq        : int,
    frag_id    : int,
    frag_total : int,
    frag_idx   : int,
    timestamp  : Optional[int] = None,
) -> bytes:
    """
    Serialise one fragment into a complete BLE frame (header + payload + CRC).
    """
    if timestamp is None:
        timestamp = int(time.time() * 1000)

    header = MessageHeader(
        msg_type=msg_type, flags=flags, seq=seq,
        frag_id=frag_id, frag_total=frag_total, frag_idx=frag_idx,
        src_id=src_id, dst_id=dst_id,
        timestamp=timestamp, payload_len=len(payload),
    )
    raw_header = header.pack()
    crc = _crc16(raw_header + payload)
    return raw_header + payload + struct.pack(">H", crc)


def parse_frame(data: bytes) -> Optional[Message]:
    """
    Parse raw bytes → Message, verifying length and CRC.
    Returns None on any parse / integrity failure.
    """
    if len(data) < HEADER_SIZE + CRC_SIZE:
        return None

    header = MessageHeader.unpack(data)
    if header is None:
        return None

    payload_end = HEADER_SIZE + header.payload_len
    if len(data) < payload_end + CRC_SIZE:
        return None

    payload = data[HEADER_SIZE:payload_end]
    (crc_wire,) = struct.unpack(">H", data[payload_end:payload_end + CRC_SIZE])

    msg = Message(header=header, payload=payload, crc=crc_wire)
    if not msg.is_valid:
        return None   # CRC mismatch — discard silently

    return msg