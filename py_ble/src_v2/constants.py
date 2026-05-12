
# --- Service / Characteristic UUIDs --------------------------------------
SERVICE_UUID = "bada5500-c0de-cafe-babe-000000000001"
TX_CHAR_UUID = "bada5500-c0de-cafe-babe-000000000002"   # Peripheral -> Central NOTIFY
RX_CHAR_UUID = "bada5500-c0de-cafe-babe-000000000003"   # Central -> Peripheral WRITE

DEVICE_NAME = "BLE-STACK"

# --- Timing --------------------------------------------------------------
SCAN_TIMEOUT        = 6.0   # seconds to scan before becoming peripheral
GATT_REGISTER_DELAY = 0.6   # BlueZ D-Bus registration settling time
PING_INTERVAL       = 15.0  # keepalive period
INTER_PKT_GAP       = 0.020 # seconds between successive BLE packets

# --- Protocol ------------------------------------------------------------
PROTOCOL_VERSION    = 1
CHUNK_SIZE          = 174       # payload bytes per BLE packet (6-byte header leaves 174 of 180)
MAX_MSG_ID          = 256
BROADCAST_ADDR      = 0xFFFF    # short-address meaning "all peers"
LOOPBACK_ADDR       = 0x0000    # short-address meaning "connected peer" (no routing)

# --- Mesh defaults -------------------------------------------------------
DEFAULT_TTL     = 5     # hops before a mesh packet is dropped
MESH_SEEN_CACHE = 512   # max(src_addr, seq) pairs to remember

# --- Ranging -------------------------------------------------------------
RANGING_INTERVAL    = 2.0   # seconds between RSSI ping-pairs
RSSI_N_FACTOR       = 2.0   # path-loss exponent (2.0 = free space)
TX_POWER_DEFAULT    = -59   # measured RSSI at 1 m (calibrate per device)
KALMAN_Q            = 0.008 # process noise
KALMAN_R            = 1.0   # measurement noise

# --- Group chat ----------------------------------------------------------
MAX_GROUPS  = 16

# --- Platform detection --------------------------------------------------
import platform as _platform
 
IS_LINUX   = _platform.system() == "Linux"
IS_MACOS   = _platform.system() == "Darwin"
IS_WINDOWS = _platform.system() == "Windows"
 
# ATT write mode — controls response= on write_gatt_char
#
# Linux / BlueZ:
#   The bless write_request_func is only triggered by ATT Write Request
#   (opcode 0x12, response=True).  ATT Write Command (0x52, response=False)
#   bypasses the D-Bus WriteValue path entirely.  Must stay True.
#
# macOS / Core Bluetooth:
#   response=False (ATT Write Command) is fully supported and ~3× faster
#   because there is no per-chunk acknowledgment round-trip.  The connection
#   interval is typically 15–45 ms; eliminating the Write Response halves
#   the effective per-chunk latency.
#
# Windows / WinRT:
#   response=True is safer; some WinRT driver versions drop write commands
#   silently on certain chipsets.
USE_WRITE_RESPONSE = IS_LINUX or IS_WINDOWS
 
# Packet priority levels (used by PriorityQueue inside CentralTransport)
CTRL_PRIORITY = 0   # HANDSHAKE, PING, PONG, ACK, GOODBYE  — always drains first
DATA_PRIORITY = 1   # CHAT, RANGING, MESH, GROUP            — normal traffic
 
# ── MTU / chunk size ──────────────────────────────────────────────────────────
# CHUNK_SIZE=174 requires a negotiated ATT_MTU of at least 183 bytes
#   (174 payload + 6 protocol header + 3 ATT header = 183).
# BlueZ (Linux):  negotiates 517 automatically — safe.
# Core Bluetooth: negotiates ≥185 on BLE 4.2+   — safe.
# WinRT:          can be as low as 23 on old drivers; bump CHUNK_SIZE down
#                 to 10 if you need Windows support on legacy hardware.
MIN_MTU_REQUIRED = 183
