"""
core/packet.py
==============
Binary packet protocol for BLE Mesh Network.

Wire format (little-endian):
  [ MAGIC(2) | VERSION(1) | TYPE(1) | SRC_ADDR(6) | DST_ADDR(6) |
    GROUP_ID(4) | SEQ_NUM(4) | TTL(1) | HOP_COUNT(1) | FLAGS(1) |
    FRAG_IDX(1) | FRAG_TOTAL(1) | PAYLOAD_LEN(2) | PAYLOAD(var) | TAG(16) ]

Total fixed overhead: 46 bytes  |  Max BLE MTU ~512 bytes
"""

from __future__ import annotations
import struct
import time
import uuid
import hashlib
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag
from typing import Optional, List, Tuple

# ── Constants ─────────────────────────────────────────────────────────────────

MAGIC           = 0x4D42          # "BM" – Bluetooth Mesh
PROTOCOL_VER    = 1
BROADCAST_ADDR  = b"\xff" * 6    # FF:FF:FF:FF:FF:FF
MAX_TTL         = 7
HEADER_FMT      = "<HBB6s6sIIBBBBBH"
HEADER_SIZE     = struct.calcsize(HEADER_FMT)   # 36 bytes
TAG_SIZE        = 16                             # AES-GCM tag / HMAC-128
MAX_PACKET_SIZE = 512
MAX_PAYLOAD     = MAX_PACKET_SIZE - HEADER_SIZE - TAG_SIZE   # ≈460 bytes


# ── Packet Types ──────────────────────────────────────────────────────────────

class PacketType(IntEnum):
    # Control plane
    HEARTBEAT       = 0x01   # Node alive + metadata broadcast
    ROUTE_REQUEST   = 0x02   # Ask for path to destination
    ROUTE_REPLY     = 0x03   # Reply with path info
    ROUTE_ERROR     = 0x04   # Path broken notification
    ACK             = 0x05   # Delivery acknowledgement
    PING            = 0x06   # Round-trip latency probe
    PONG            = 0x07   # RTT probe reply

    # Data plane
    DIRECT_MSG      = 0x10   # Unicast text/binary message
    GROUP_MSG       = 0x11   # Multicast group message
    BROADCAST_MSG   = 0x12   # Network-wide broadcast

    # Location services
    RSSI_BEACON     = 0x20   # Location beacon with RSSI data
    LOC_REQUEST     = 0x21   # Ask peers to respond for triangulation
    LOC_RESPONSE    = 0x22   # RSSI response for locating

    # Feature extension
    FEATURE_MSG     = 0xF0   # Generic feature payload (subtype in payload)
    FRAGMENT        = 0xF1   # Fragment of a larger packet


class PacketFlag(IntFlag):
    NONE        = 0x00
    ENCRYPTED   = 0x01   # Payload is AES-GCM encrypted
    COMPRESSED  = 0x02   # Payload is zlib compressed
    RELIABLE    = 0x04   # Requests ACK from final destination
    FRAGMENTED  = 0x08   # Part of a fragmented sequence
    PRIORITY    = 0x10   # High-priority routing hint


# ── Packet Dataclass ──────────────────────────────────────────────────────────

@dataclass
class Packet:
    ptype:       PacketType
    src_addr:    bytes                        # 6-byte MAC or node ID
    dst_addr:    bytes = field(default_factory=lambda: BROADCAST_ADDR)
    group_id:    int   = 0                   # 0 = no group
    seq_num:     int   = 0
    ttl:         int   = MAX_TTL
    hop_count:   int   = 0
    flags:       PacketFlag = PacketFlag.NONE
    frag_idx:    int   = 0                   # 0-based fragment index
    frag_total:  int   = 1                   # total fragment count
    payload:     bytes = b""
    tag:         bytes = b"\x00" * TAG_SIZE  # auth tag set by crypto layer
    timestamp:   float = field(default_factory=time.monotonic)

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_bytes(self) -> bytes:
        header = struct.pack(
            HEADER_FMT,
            MAGIC,
            PROTOCOL_VER,
            int(self.ptype),
            self.src_addr,
            self.dst_addr,
            self.group_id,
            self.seq_num,
            self.ttl,
            self.hop_count,
            int(self.flags),
            self.frag_idx,
            self.frag_total,
            len(self.payload),
        )
        return header + self.payload + self.tag

    @classmethod
    def from_bytes(cls, data: bytes) -> "Packet":
        if len(data) < HEADER_SIZE + TAG_SIZE:
            raise ValueError(f"Packet too short: {len(data)} bytes")

        (magic, version, ptype, src_addr, dst_addr, group_id,
         seq_num, ttl, hop_count, flags, frag_idx, frag_total,
         payload_len) = struct.unpack(HEADER_FMT, data[:HEADER_SIZE])

        if magic != MAGIC:
            raise ValueError(f"Bad magic: 0x{magic:04X}")
        if version != PROTOCOL_VER:
            raise ValueError(f"Unsupported version: {version}")

        expected_len = HEADER_SIZE + payload_len + TAG_SIZE
        if len(data) < expected_len:
            raise ValueError("Truncated packet")

        payload = data[HEADER_SIZE: HEADER_SIZE + payload_len]
        tag     = data[HEADER_SIZE + payload_len: expected_len]

        return cls(
            ptype      = PacketType(ptype),
            src_addr   = src_addr,
            dst_addr   = dst_addr,
            group_id   = group_id,
            seq_num    = seq_num,
            ttl        = ttl,
            hop_count  = hop_count,
            flags      = PacketFlag(flags),
            frag_idx   = frag_idx,
            frag_total = frag_total,
            payload    = payload,
            tag        = tag,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def is_broadcast(self) -> bool:
        return self.dst_addr == BROADCAST_ADDR

    @property
    def is_encrypted(self) -> bool:
        return PacketFlag.ENCRYPTED in self.flags

    @property
    def is_fragmented(self) -> bool:
        return self.frag_total > 1

    @property
    def cache_key(self) -> Tuple[bytes, int]:
        """Unique key for deduplication: (src, seq_num)."""
        return (self.src_addr, self.seq_num)

    def addr_str(self, addr: bytes) -> str:
        return ":".join(f"{b:02X}" for b in addr)

    def __repr__(self) -> str:
        return (
            f"<Packet {self.ptype.name} "
            f"src={self.addr_str(self.src_addr)} "
            f"dst={self.addr_str(self.dst_addr)} "
            f"seq={self.seq_num} ttl={self.ttl} "
            f"frag={self.frag_idx}/{self.frag_total} "
            f"flags={self.flags!r} len={len(self.payload)}>"
        )


# ── Packet Factory ────────────────────────────────────────────────────────────

class PacketFactory:
    """Creates packets with auto-incrementing sequence numbers."""

    def __init__(self, src_addr: bytes):
        self._src   = src_addr
        self._seq   = int(time.time()) & 0xFFFFFFFF   # start from unix ts

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq

    def build(
        self,
        ptype:    PacketType,
        payload:  bytes        = b"",
        dst_addr: bytes        = BROADCAST_ADDR,
        group_id: int          = 0,
        ttl:      int          = MAX_TTL,
        flags:    PacketFlag   = PacketFlag.NONE,
    ) -> "Packet":
        return Packet(
            ptype    = ptype,
            src_addr = self._src,
            dst_addr = dst_addr,
            group_id = group_id,
            seq_num  = self._next_seq(),
            ttl      = ttl,
            flags    = flags,
            payload  = payload,
        )

    def fragment(self, pkt: Packet) -> List[Packet]:
        """Split an oversized packet into MTU-sized fragments."""
        if len(pkt.payload) <= MAX_PAYLOAD:
            return [pkt]

        chunks = [
            pkt.payload[i: i + MAX_PAYLOAD]
            for i in range(0, len(pkt.payload), MAX_PAYLOAD)
        ]
        frags = []
        for idx, chunk in enumerate(chunks):
            f = Packet(
                ptype      = pkt.ptype,
                src_addr   = pkt.src_addr,
                dst_addr   = pkt.dst_addr,
                group_id   = pkt.group_id,
                seq_num    = pkt.seq_num,
                ttl        = pkt.ttl,
                flags      = pkt.flags | PacketFlag.FRAGMENTED,
                frag_idx   = idx,
                frag_total = len(chunks),
                payload    = chunk,
            )
            frags.append(f)
        return frags


# ── Fragment Reassembler ──────────────────────────────────────────────────────

class FragmentBuffer:
    """Accumulates fragments and reassembles them when complete."""

    def __init__(self, timeout: float = 5.0):
        self._timeout = timeout
        # key → (Packet | None, deadline)
        self._buffers: dict[tuple, dict] = {}

    def _key(self, pkt: Packet) -> tuple:
        return (pkt.src_addr, pkt.seq_num)

    def add(self, pkt: Packet) -> Optional[Packet]:
        """
        Add a fragment. Returns the reassembled Packet when all
        fragments have arrived, otherwise None.
        """
        key = self._key(pkt)
        self._expire()

        if key not in self._buffers:
            self._buffers[key] = {
                "frags":    [None] * pkt.frag_total,
                "total":    pkt.frag_total,
                "received": 0,
                "template": pkt,
                "deadline": time.monotonic() + self._timeout,
            }

        buf = self._buffers[key]
        if buf["frags"][pkt.frag_idx] is None:
            buf["frags"][pkt.frag_idx] = pkt.payload
            buf["received"] += 1

        if buf["received"] == buf["total"] and all(f is not None for f in buf["frags"]):
            full_payload = b"".join(buf["frags"])
            template: Packet = buf["template"]
            del self._buffers[key]
            return Packet(
                ptype      = template.ptype,
                src_addr   = template.src_addr,
                dst_addr   = template.dst_addr,
                group_id   = template.group_id,
                seq_num    = template.seq_num,
                ttl        = template.ttl,
                hop_count  = template.hop_count,
                flags      = template.flags & ~PacketFlag.FRAGMENTED,
                frag_idx   = 0,
                frag_total = 1,
                payload    = full_payload,
                tag        = template.tag,
            )
        return None

    def _expire(self):
        now = time.monotonic()
        stale = [k for k, v in self._buffers.items() if v["deadline"] < now]
        for k in stale:
            del self._buffers[k]