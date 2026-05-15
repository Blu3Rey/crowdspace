"""
core/protocol.py — Wire-protocol constants for the BLE mesh.

UUIDs
-----
We use the Nordic UART Service (NUS) UUID base to maximise compatibility with
existing BLE tooling.  The four characteristic UUIDs are derived from the
service UUID by incrementing the last nibble.

Packet header (44 bytes, big-endian)
--------------------------------------
  offset  size  field
  ------  ----  -----
       0     1  version     : protocol version (currently 0x01)
       1     1  msg_type    : MsgType constant
       2     1  ttl         : hops remaining
       3     1  flags       : bitmask of Flags constants
       4    16  src_id      : 128-bit source node identifier
      20    16  dst_id      : 128-bit destination (0xFF*16 = broadcast)
      36     4  seq_num     : per-source 32-bit sequence counter
      40     2  payload_len : byte length of the trailing payload
      42     2  checksum    : CRC-16/CCITT-FALSE over header[0:42] + payload
                            (checksum field itself is excluded from CRC input)
"""

import struct

# ── Service & Characteristic UUIDs ───────────────────────────────────────────
MESH_SERVICE_UUID  = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
RX_CHAR_UUID       = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write  (central → peripheral)
TX_CHAR_UUID       = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notify (peripheral → central)
INFO_CHAR_UUID     = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"  # Read   (node identity)

# ── Special addresses ─────────────────────────────────────────────────────────
BROADCAST_ADDR = b"\xff" * 16   # flood to every node
NULL_ADDR      = b"\x00" * 16

# ── Protocol version ──────────────────────────────────────────────────────────
PROTOCOL_VERSION = 0x01

# ── Header layout ─────────────────────────────────────────────────────────────
#   !BBBB 16s 16s I H H
#   1+1+1+1+16+16+4+2+2 = 44 bytes
HEADER_FORMAT    = "!BBBB16s16sIHH"
HEADER_SIZE      = struct.calcsize(HEADER_FORMAT)   # 44
FRAG_HEADER_SIZE = 4   # frag_id(2) + total_frags(1) + frag_index(1)


class MsgType:
    """Byte constants for the *msg_type* header field.

    Control (0x0x)
    ~~~~~~~~~~~~~~
    DISCOVERY   — broadcast on startup and periodically; payload = INFO_CHAR content
    HEARTBEAT   — keep-alive broadcast; same payload as DISCOVERY
    ACK         — acknowledges a packet; seq_num = ACKed seq_num; no payload
    ROUTE_REQ   — broadcast route discovery; payload = target_id (16 B)
    ROUTE_REPLY — unicast route reply;     payload = target_id (16 B) + hop_count (1 B)
    FRAGMENT    — carries one fragment;    payload = frag header (4 B) + chunk

    Messaging (0x1x)
    ~~~~~~~~~~~~~~~~
    DIRECT_MSG  — unicast text/binary;           payload = msg bytes
    GROUP_JOIN  — announce group membership;     payload = group_id string (UTF-8)
    GROUP_LEAVE — retract group membership;      payload = group_id string (UTF-8)
    GROUP_MSG   — multicast to group;            payload = group_id_len(1B) + group_id + msg

    Locating (0x2x)
    ~~~~~~~~~~~~~~~
    LOC_REQ    — request RSSI ping; payload = target_id (16 B) or BROADCAST
    LOC_RESP   — RSSI ping reply;   payload = requester_id(16B) + rssi(1B signed) + hops(1B)
    LOC_REPORT — aggregated table;  payload = N × (node_id(16B) + rssi(1B) + hops(1B))

    Extension (0xFx)
    ~~~~~~~~~~~~~~~~
    CUSTOM      — user-defined; payload = sub_type(1B) + custom bytes
    """

    # control
    DISCOVERY    = 0x01
    HEARTBEAT    = 0x02
    ACK          = 0x03
    ROUTE_REQ    = 0x04
    ROUTE_REPLY  = 0x05
    FRAGMENT     = 0x06

    # messaging
    DIRECT_MSG   = 0x10
    GROUP_JOIN   = 0x11
    GROUP_LEAVE  = 0x12
    GROUP_MSG    = 0x13

    # locating
    LOC_REQ      = 0x20
    LOC_RESP     = 0x21
    LOC_REPORT   = 0x22

    # extension
    CUSTOM       = 0xF0

    _names: dict = {
        0x01: "DISCOVERY",  0x02: "HEARTBEAT",  0x03: "ACK",
        0x04: "ROUTE_REQ",  0x05: "ROUTE_REPLY", 0x06: "FRAGMENT",
        0x10: "DIRECT_MSG", 0x11: "GROUP_JOIN",  0x12: "GROUP_LEAVE",
        0x13: "GROUP_MSG",
        0x20: "LOC_REQ",    0x21: "LOC_RESP",    0x22: "LOC_REPORT",
        0xF0: "CUSTOM",
    }

    @classmethod
    def name(cls, t: int) -> str:
        return cls._names.get(t, f"UNKNOWN(0x{t:02x})")


class Flags:
    """Bitmask constants for the *flags* header field."""
    NONE       = 0x00
    ACK_REQ    = 0x01   # sender expects an ACK
    ENCRYPTED  = 0x02   # payload is AES-256-GCM encrypted
    FRAGMENTED = 0x04   # this packet is one fragment of a larger message
    COMPRESSED = 0x08   # payload is zlib-compressed
    RELIABLE   = 0x10   # apply ACK + retransmission logic