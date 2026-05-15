"""
core/packet.py — Packet serialisation, CRC verification, and fragmentation.

Design notes
------------
* All integers are big-endian (network byte order) on the wire.
* CRC-16/CCITT-FALSE protects the full packet (header bytes 0-41 + payload).
* Compression (zlib level 6) is applied *before* encryption.
* Fragmentation carries a 4-byte fragment header inside the packet payload.
"""

from __future__ import annotations

import struct
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .protocol import (
    BROADCAST_ADDR,
    FRAG_HEADER_SIZE,
    HEADER_FORMAT,
    HEADER_SIZE,
    MsgType,
    Flags,
    PROTOCOL_VERSION,
)


# ── CRC-16/CCITT-FALSE ────────────────────────────────────────────────────────

def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = (crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1
    return crc & 0xFFFF


# ── Packet dataclass ──────────────────────────────────────────────────────────

@dataclass
class Packet:
    """Represents one BLE mesh packet (decrypted, decompressed view).

    Use :meth:`encode` to serialise for the wire.
    Use :meth:`decode` to deserialise from raw bytes received over BLE.

    The fields *rssi* and *received_at* are local metadata; they are **not**
    transmitted on the wire.
    """

    msg_type:  int
    src_id:    bytes          # 16 bytes
    dst_id:    bytes          # 16 bytes (BROADCAST_ADDR → flood)
    seq_num:   int
    payload:   bytes = b""
    ttl:       int   = 7
    flags:     int   = Flags.NONE
    version:   int   = PROTOCOL_VERSION

    # Local metadata — not on the wire
    rssi:        Optional[int]  = field(default=None,                     compare=False, repr=False)
    received_at: float          = field(default_factory=time.monotonic,   compare=False, repr=False)

    # ── Encoding ──────────────────────────────────────────────────────────────

    def encode(self) -> bytes:
        """Serialise to wire bytes.  Compression is applied if COMPRESSED flag set."""
        payload = zlib.compress(self.payload, level=6) if (self.flags & Flags.COMPRESSED) else self.payload
        # Build header without checksum
        pre_crc = struct.pack(
            "!BBBB16s16sIH",
            self.version, self.msg_type, self.ttl, self.flags,
            self.src_id, self.dst_id, self.seq_num, len(payload),
        )
        crc = _crc16(pre_crc + payload)
        return pre_crc + struct.pack("!H", crc) + payload

    # ── Decoding ──────────────────────────────────────────────────────────────

    @classmethod
    def decode(cls, raw: bytes, rssi: Optional[int] = None) -> "Packet":
        """Deserialise from wire bytes.  Raises ``ValueError`` on bad CRC or truncation."""
        if len(raw) < HEADER_SIZE:
            raise ValueError(f"Packet too short: {len(raw)} < {HEADER_SIZE} bytes")

        (version, msg_type, ttl, flags,
         src_id, dst_id, seq_num, payload_len, checksum) = struct.unpack(
            HEADER_FORMAT, raw[:HEADER_SIZE]
        )

        payload = raw[HEADER_SIZE: HEADER_SIZE + payload_len]
        if len(payload) != payload_len:
            raise ValueError(f"Truncated payload: expected {payload_len}B, got {len(payload)}B")

        # Verify CRC over [header sans checksum] + payload
        expected = _crc16(raw[:HEADER_SIZE - 2] + payload)
        if expected != checksum:
            raise ValueError(f"CRC mismatch: received {checksum:#06x}, computed {expected:#06x}")

        if flags & Flags.COMPRESSED:
            payload = zlib.decompress(payload)

        return cls(
            version=version, msg_type=msg_type, ttl=ttl, flags=flags,
            src_id=src_id, dst_id=dst_id, seq_num=seq_num,
            payload=payload, rssi=rssi,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def is_broadcast(self) -> bool:
        return self.dst_id == BROADCAST_ADDR

    @property
    def dedup_key(self) -> Tuple[bytes, int]:
        """Unique key for duplicate detection: (src_id, seq_num)."""
        return (self.src_id, self.seq_num)

    def forwarded(self) -> "Packet":
        """Return a copy with TTL decremented by 1 (for mesh forwarding)."""
        from dataclasses import replace
        return replace(self, ttl=self.ttl - 1, rssi=None)

    def make_ack(self, our_id: bytes) -> "Packet":
        """Create an ACK packet directed back to the sender of *self*."""
        return Packet(
            msg_type=MsgType.ACK,
            src_id=our_id,
            dst_id=self.src_id,
            seq_num=self.seq_num,
        )

    def __repr__(self) -> str:
        dst = "BCAST" if self.is_broadcast else self.dst_id.hex()[:8] + "…"
        return (
            f"<Packet {MsgType.name(self.msg_type)} "
            f"src={self.src_id.hex()[:8]}… dst={dst} "
            f"seq={self.seq_num} ttl={self.ttl} "
            f"payload={len(self.payload)}B>"
        )


# ── Fragmentation ─────────────────────────────────────────────────────────────

# Fragment sub-header (inside Packet.payload for FRAGMENT packets):
#   frag_id    : uint16 — ties all fragments of one message together
#   total_frags: uint8  — total fragment count for this message
#   frag_index : uint8  — zero-based index of this fragment
_FRAG_FMT = "!HBB"   # 4 bytes


def fragment(
    msg_type: int,
    src_id: bytes,
    dst_id: bytes,
    base_seq: int,
    full_payload: bytes,
    max_payload: int,
    base_flags: int = Flags.NONE,
    ttl: int = 7,
) -> List[Packet]:
    """Split *full_payload* into a list of FRAGMENT :class:`Packet` objects.

    Parameters
    ----------
    max_payload:
        Maximum number of bytes per fragment payload (MTU − HEADER_SIZE − FRAG_HEADER_SIZE).
    """
    chunk_size = max_payload - FRAG_HEADER_SIZE
    if chunk_size <= 0:
        raise ValueError(f"MTU too small for fragmentation (chunk_size={chunk_size})")

    chunks = [full_payload[i: i + chunk_size] for i in range(0, len(full_payload), chunk_size)]
    if len(chunks) > 255:
        raise ValueError(f"Payload too large: needs {len(chunks)} fragments (max 255)")

    frag_id = base_seq & 0xFFFF
    return [
        Packet(
            msg_type=MsgType.FRAGMENT,
            src_id=src_id,
            dst_id=dst_id,
            seq_num=base_seq + idx,
            payload=struct.pack(_FRAG_FMT, frag_id, len(chunks), idx) + chunk,
            ttl=ttl,
            flags=base_flags | Flags.FRAGMENTED,
        )
        for idx, chunk in enumerate(chunks)
    ]


class FragmentAssembler:
    """Reassemble FRAGMENT packets back into their original payloads.

    ``feed()`` returns the full reassembled bytes when all fragments have
    arrived, or ``None`` if the message is still incomplete.

    Stale sessions (older than *timeout* seconds) are pruned automatically.
    """

    def __init__(self, timeout: float = 30.0):
        self._timeout = timeout
        # key = (src_id, frag_id) → {index: chunk}
        self._chunks: Dict[tuple, Dict[int, bytes]] = {}
        # key = (src_id, frag_id) → (total_frags, created_at)
        self._meta:   Dict[tuple, Tuple[int, float]] = {}

    def feed(self, packet: Packet) -> Optional[bytes]:
        """Feed one FRAGMENT packet.  Returns reassembled payload or ``None``."""
        if len(packet.payload) < FRAG_HEADER_SIZE:
            return None
        frag_id, total, index = struct.unpack(_FRAG_FMT, packet.payload[:FRAG_HEADER_SIZE])
        chunk = packet.payload[FRAG_HEADER_SIZE:]
        key = (packet.src_id, frag_id)

        if key not in self._chunks:
            self._chunks[key] = {}
            self._meta[key] = (total, time.monotonic())

        self._chunks[key][index] = chunk

        if len(self._chunks[key]) == total:
            full = b"".join(self._chunks[key][i] for i in range(total))
            del self._chunks[key]
            del self._meta[key]
            return full
        return None

    def prune_stale(self) -> int:
        """Remove sessions older than *timeout*.  Returns count removed."""
        now = time.monotonic()
        stale = [k for k, (_, t) in self._meta.items() if now - t > self._timeout]
        for k in stale:
            self._chunks.pop(k, None)
            self._meta.pop(k, None)
        return len(stale)