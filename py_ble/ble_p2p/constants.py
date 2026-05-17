"""
constants.py — Protocol-wide UUIDs, timing parameters, and enumerations.

All values here are the single source of truth. Change MTU or UUIDs only here;
nothing else in the codebase should hard-code them.
"""
from enum import IntEnum, IntFlag

# ─────────────────────────────────────────────────────────────
# GATT Service / Characteristic UUIDs
# Custom 128-bit UUIDs in the "BLEP" (BLE P2P) namespace.
# ─────────────────────────────────────────────────────────────
SERVICE_UUID     = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_WRITE_UUID  = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Central → Peripheral (Write / Write-No-Response)
CHAR_NOTIFY_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Peripheral → Central (Notify + Read)
CHAR_INFO_UUID   = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"  # Device metadata (Read-only)

# ─────────────────────────────────────────────────────────────
# Binary Frame Layout
#
#  ┌──────┬─────┬──────┬───────┬─────┬─────────┬──────────┬───────────┬─────────┬────────┬──────────────┬─────────────┬──────────┬─────────┐
#  │MAGIC │VER  │TYPE  │FLAGS  │SEQ  │FRAG_ID  │FRAG_TOTAL│FRAG_IDX   │SRC_ID   │DST_ID  │TIMESTAMP_MS  │PAYLOAD_LEN  │PAYLOAD…  │CRC16    │
#  │ 2B   │ 1B  │ 1B   │ 1B    │ 2B  │  2B     │   1B     │   1B      │  8B     │  8B    │     8B       │     2B      │  ≤205B   │  2B     │
#  └──────┴─────┴──────┴───────┴─────┴─────────┴──────────┴───────────┴─────────┴────────┴──────────────┴─────────────┴──────────┴─────────┘
#  Total header overhead: 37 bytes.  CRC trailer: 2 bytes.
# ─────────────────────────────────────────────────────────────
PROTOCOL_MAGIC   : bytes = b"\xBE\xEF"
PROTOCOL_VERSION : int   = 1

# struct format string (big-endian): see message.py for field names.
HEADER_STRUCT_FMT = ">2sBBBHHBB8s8sQH"   # yields 37 bytes
HEADER_SIZE       = 37                     # struct.calcsize(HEADER_STRUCT_FMT) == 37
CRC_SIZE          = 2

BLE_MTU_CONSERVATIVE = 244               # safe across all BLE 4.2+ adapters
MAX_PAYLOAD_PER_FRAG = BLE_MTU_CONSERVATIVE - HEADER_SIZE - CRC_SIZE  # 205 bytes

# ─────────────────────────────────────────────────────────────
# Timing & Retry
# ─────────────────────────────────────────────────────────────
SCAN_DURATION_S  = 5.0    # active scan window per cycle
SCAN_INTERVAL_S  = 15.0   # idle gap between scan cycles
CONNECT_TIMEOUT  = 10.0   # GATT connection timeout
SESSION_WINDOW_S = 4.0    # wait this long after writes for incoming notifications
FRAG_TIMEOUT_S   = 60.0   # drop incomplete fragment groups after this
ACK_TIMEOUT_S    = 8.0    # give up waiting for ACK after this
MAX_RETRIES      = 3
RECONNECT_BACKOFF_S = 30.0  # min wait before retrying a failed peer

# ─────────────────────────────────────────────────────────────
# Protocol Enumerations
# ─────────────────────────────────────────────────────────────
class MsgType(IntEnum):
    HANDSHAKE      = 0x01   # Identity + capability announcement
    HANDSHAKE_ACK  = 0x02   # Handshake reply
    DATA           = 0x10   # Raw application data
    ACK            = 0x11   # Delivery acknowledgment
    NACK           = 0x12   # Negative acknowledgment (error info in payload)
    PING           = 0x20   # Liveness / RTT probe
    PONG           = 0x21   # Ping response
    ROUTE          = 0x30   # Gossip: mesh routing table update
    FEATURE        = 0x60   # Feature-namespaced message (see FeatureID)
    ERROR          = 0xFF   # Protocol-level error


class MsgFlags(IntFlag):
    NONE         = 0x00
    ENCRYPTED    = 0x01   # Payload is E2E encrypted (future: NaCl secretbox)
    COMPRESSED   = 0x02   # Payload is zlib-compressed
    REQUIRES_ACK = 0x04   # Sender awaits explicit ACK
    RELAY        = 0x08   # Intermediate nodes should forward if not destined for them
    BROADCAST    = 0x10   # Addressed to all reachable nodes (DST_ID ignored)
    PRIORITY     = 0x20   # Jump outbound queue (used for PING / ACK / HANDSHAKE)


class FeatureID(IntEnum):
    DIRECT_MESSAGE = 0x01
    GROUP_CHAT     = 0x02
    DEVICE_LOCATOR = 0x03
    PRESENCE       = 0x04   # Online/away/offline status beacon
    FILE_TRANSFER  = 0x05   # Chunked binary transfer (future)
    CUSTOM_BASE    = 0x80   # Third-party / user extensions start here


class Capability(IntFlag):
    NONE          = 0x00
    RELAY         = 0x01   # Will forward messages on behalf of others
    STORE_FORWARD = 0x02   # Will queue messages for offline peers
    ENCRYPTION    = 0x04   # Supports E2E encrypted payloads
    GROUP_CHAT    = 0x08
    LOCATOR       = 0x10
    FILE_TRANSFER = 0x20


# Sentinel: 8 zero-bytes in DST_ID field means "deliver to everyone"
BROADCAST_ID: bytes = b"\x00" * 8

# Device-config directory (created on first run)
import pathlib
CONFIG_DIR = pathlib.Path.home() / ".ble_p2p"