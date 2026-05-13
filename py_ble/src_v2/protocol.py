
"""
protocol.py — Wire protocol: packet encoding, reassembly, message types.
 
Enhanced header vs. the original 4-byte format:
 
    Base header (6 bytes, always):
        [0]  (version:4 | flags:4)
                 flag bit 0  HAS_ROUTING  — 5-byte routing extension follows
                 flag bit 1  HAS_GROUP    — 2-byte group_id follows
                 flag bit 2  NEEDS_ACK    — sender requests application-level ACK
                 flag bit 3  reserved
        [1]  feature_id    FeatureID enum — routes to the right FeatureBase handler
        [2]  msg_type      uint8, meaning defined by each feature
        [3]  msg_id        uint8, 0-255, wraps; scoped per (feature, peer)
        [4]  chunk_idx     uint8
        [5]  n_chunks      uint8
 
    Routing extension (5 bytes, when HAS_ROUTING set):
        [6-7]  src_addr   uint16 BE
        [8-9]  dst_addr   uint16 BE — 0xFFFF = broadcast
        [10]   ttl        uint8
 
    Group extension (2 bytes, when HAS_GROUP set):
        [N-N+1]  group_id  uint16 BE
 
    Payload: remaining bytes (up to CHUNK_SIZE)
 
Why not TLV / protobuf?
    BLE packets are tiny (typ. 20-512 bytes).  A fixed-position optional-block
    scheme costs 0 bytes when unused and avoids a parser loop on the critical
    path.  Adding new extension blocks (HAS_TIMESTAMP, HAS_SIGNATURE …) is a
    one-bit flag away.
"""


import struct
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional

from .constants import (
    BROADCAST_ADDR, CHUNK_SIZE, LOOPBACK_ADDR, MAX_MSG_ID, PROTOCOL_VERSION
)

# --- Feeature identifiers ------------------------------------------------

class FeatureID(IntEnum):
    CORE    = 0x00  # HANDSHAKE, PING, PONG, GOODBYE - managed by the stack
    CHAT    = 0x01  # 1-to-1 text messages + ACK
    RANGING = 0x02  # RSSI ping/report pairs for distance estimation
    MESH    = 0x03  # flood-mesh routing (neighbour discovery + forwarding)
    GROUP   = 0x04  # broadcast group chat
    CUSTOM  = 0xFF  # user-defined features start here

# --- Core message types (FeatureID.CORE) --------------------------------

class CoreMsg(IntEnum):
    HANDSHAKE   = 0x00  # payload: device_name (UTF-8)
    PING        = 0x01  # payload: empty
    PONG        = 0x02  # payload: empty
    GOODBYE     = 0x03  # payload: empty (or optional reason string)

# --- Packet header flags -------------------------------------------------

class PacketFlags(IntEnum):
    HAS_ROUTING = 0b0001
    HAS_GROUP   = 0b0010
    NEEDS_ACK   = 0b0100

# --- Packet dataclass ----------------------------------------------------

@dataclass
class Packet:
    """
    One BLE-layer transmission unit. Encodes / decodes the full wire format
    including optional routing and group extensions.
    """
    feature_id: FeatureID
    msg_type:   int
    msg_id:     int
    chunk_idx:  int
    n_chunks:   int
    payload:    bytes
    flags:      int     = 0
    src_addr:   int     = LOOPBACK_ADDR
    dst_addr:   int     = LOOPBACK_ADDR
    ttl:        int     = 0
    group_id:   int     = 0
    version:    int     = PROTOCOL_VERSION

    # --- Flag helpers -----------------------------------------------------

    @property
    def has_routing(self) -> bool:
        return bool(self.flags & PacketFlags.HAS_ROUTING)

    @property
    def has_group(self) -> bool:
        return bool(self.flags & PacketFlags.HAS_GROUP)

    @property
    def needs_ack(self) -> bool:
        return bool(self.flags & PacketFlags.NEEDS_ACK)

    @property
    def is_broadcast(self) -> bool:
        return self.dst_addr == BROADCAST_ADDR

    # --- Encoding -------------------------------------------------------------

    def encode(self) -> bytes:
        out = bytearray()

        # Base header (6 bytes)
        out.append((self.version << 4) | (self.flags & 0x0F))
        out.append(int(self.feature_id))
        out.append(self.msg_type & 0xFF)
        out.append(self.msg_id & 0xFF)
        out.append(self.chunk_idx)
        out.append(self.n_chunks)

        # Optional routing extension (5 bytes)
        if self.has_routing:
            out += struct.pack(">HHB", self.src_addr, self.dst_addr, self.ttl)
        
        # Optional group extension (2 bytes)
        if self.has_group:
            out += struct.pack(">H", self.group_id)
        
        out += self.payload
        return bytes(out)
    
    # --- Decoding ------------------------------------------------------------

    @staticmethod
    def decode(raw: bytes | bytearray) -> "Packet":
        raw = bytes(raw)
        if len(raw) < 6:
            raise ValueError(f"Packet too short: {len(raw)} bytes")
        
        b0          = raw[0]
        version     = (b0 >> 4) & 0x0F
        flags       = b0 & 0x0F
        feature_id  = FeatureID(raw[1])
        msg_type    = raw[2]
        msg_id      = raw[3]
        chunk_idx   = raw[4]
        n_chunks    = raw[5]

        cursor = 6
        src_addr = dst_addr = ttl = 0
        group_id = 0

        if flags & PacketFlags.HAS_ROUTING:
            if len(raw) < cursor + 5:
                raise ValueError("Truncated routing extension")
            src_addr, dst_addr, ttl = struct.unpack_from(">H", raw, cursor)
            cursor += 5
        
        if flags & PacketFlags.HAS_GROUP:
            if len(raw) < cursor + 2:
                raise ValueError("Truncated group extension")
            (group_id,) = struct.unpack_from(">H", raw, cursor)
            cursor += 2
        
        return Packet(
            feature_id = feature_id,
            msg_type = msg_type,
            msg_id = msg_id,
            chunk_idx = chunk_idx,
            n_chunks = n_chunks,
            payload = raw[cursor:],
            flags = flags,
            src_addr = src_addr,
            dst_addr = dst_addr,
            ttl = ttl,
            group_id = group_id,
            version = version
        )

# --- Reassembled application-layer message -------------------------------------

@dataclass
class Message:
    """A fully reassembled application message from one peer."""
    feature_id: FeatureID
    msg_type:   int
    msg_id:     int
    payload:    bytes
    src_addr:   int     = LOOPBACK_ADDR
    dst_addr:   int     = LOOPBACK_ADDR
    group_id:   int     = 0
    flags:      int     = 0

# -- Packet builder -------------------------------------------------------------

def build_packets(
    feature_id: FeatureID,
    msg_type:   int,
    msg_id:     int,
    payload:    bytes | str,
    *,
    flags:      int = 0,
    src_addr:   int = LOOPBACK_ADDR,
    dst_addr:   int = LOOPBACK_ADDR,
    ttl:        int = 0,
    group_id:   int = 0,
) -> list[bytes]:
    """
    Split a payload into CHUNK_SIZE-byte BLE packets with the extended header.
    Returns a list of encoded bytes ready to enqueue on a transport.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    
    windows = [payload[i : i + CHUNK_SIZE] for i in range(0, max(1, len(payload)), CHUNK_SIZE)]
    n = len(windows)
    return [
        Packet(
            feature_id = feature_id,
            msg_type = msg_type,
            msg_id = msg_id,
            chunk_idx = idx,
            n_chunks = n,
            payload = chunk,
            flags = flags,
            src_addr = src_addr,
            dst_addr = dst_addr,
            ttl = ttl,
            group_id = group_id
        ).encode()
        for idx, chunk in enumerate(windows)
    ]

# --- Per-peer reassembler -----------------------------------------------------

class Reassembler:
    """
    Buffers incoming Packets per (feature_id, msg_id) and fires `on_complete`
    with a fully assembled Message once all chunks have arrived.

    One Reassembler instance per peer - independent reassembly streams,
    correct handling of interleaved messages across features.
    """

    def __init__(self, on_complete: Callable[[Message], None]):
        self._on_complete = on_complete
        # Key: (feature_id, msg_id) -> {chunk_idx: payload_bytes}
        self._chunks: dict[tuple, dict[int, bytes]] = defaultdict(dict)
        self._meta: dict[tuple, Packet] = {}    # first-chunk metadata
    
    def feed(self, raw: bytes):
        try:
            pkt = Packet.decode(raw)
        except Exception:
            return

        key = (int(pkt.feature_id), pkt.msg_id)
        self._chunks[key][pkt.chunk_idx] = pkt.payload
        if key not in self._meta:
            self._meta[key] = pkt
        
        if len(self._chunks[key]) == pkt.n_chunks:
            meta = self._meta.pop(key)
            payload = b"".join(self._chunks.pop(key)[i] for i in range(pkt.n_chunks))
            self._on_complete(Message(
                feature_id = meta.feature_id,
                msg_type = meta.msg_type,
                msg_id = meta.msg_id,
                payload = payload,
                src_addr = meta.src_addr,
                dst_addr = meta.dst_addr,
                group_id = meta.group_id,
                flags = meta.flags
            ))