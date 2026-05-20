/**
 * BLE Mesh Network — Constants
 *
 * All UUIDs are 128-bit (full form) to ensure uniqueness and cross-platform
 * compatibility. The Multipeer service type must be 1–15 lowercase letters /
 * numbers / hyphens per Apple's MCSession rules.
 */

// ─── GATT Service & Characteristics ──────────────────────────────────────────
/** Primary mesh service UUID advertised by every node. */
export const MESH_SERVICE_UUID = 'c39b6354-f7e2-4a8b-92d3-5e8a1b0f2c7d'

/**
 * Write-only (central → peripheral) characteristic.
 * Properties: write + writeWithoutResponse.
 * Centrals write fragmented wire frames here to deliver messages.
 */
export const MSG_WRITE_CHAR_UUID = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'

/**
 * Notify characteristic (peripheral → central).
 * Properties: notify + read.
 * Peripheral pushes frames via updateCharacteristicValue(notify=true).
 */
export const MSG_NOTIFY_CHAR_UUID = 'b2c3d4e5-f6a7-8901-bcde-f01234567891'

/**
 * Static peer-info characteristic.
 * Properties: read.
 * Holds a hex-encoded PeerInfoPayload. Updated once per session on startup.
 */
export const PEER_INFO_CHAR_UUID = 'c3d4e5f6-a7b8-9012-cdef-012345678902'

// ─── Apple Multipeer ──────────────────────────────────────────────────────────
/** Must match NSBonjourServices entry in Info.plist: _mesh-msg._tcp */
export const MULTIPEER_SERVICE_TYPE = 'mesh-msg'

// ─── Protocol ─────────────────────────────────────────────────────────────────
export const PROTOCOL_VERSION = 1 as const

/**
 * Maximum raw bytes per GATT write.
 * 180 bytes keeps us comfortably inside ATT MTU on both platforms:
 *  • Android: after requestMTU(512) typically grants 185–517 bytes
 *  • iOS: auto-negotiates ~185 bytes with modern CBCentralManager
 * Hex-encoding doubles this to 360 hex chars per write.
 */
export const MAX_CHUNK_BYTES = 180

/** Maximum number of fragments a single logical message may span. */
export const MAX_FRAGMENTS = 64

/** Interval between RSSI polls on a connected device (ms). */
export const RSSI_POLL_INTERVAL_MS = 1_500

/** Number of RSSI samples used for smoothing in device locator. */
export const RSSI_SAMPLE_COUNT = 8

/** Environmental path-loss exponent for distance estimation (free-space ≈ 2). */
export const PATH_LOSS_EXPONENT = 2.7

/** Measured RSSI at 1 metre reference distance (dBm). Calibrate per device. */
export const RSSI_AT_1M = -59

// ─── Timing ───────────────────────────────────────────────────────────────────
/** How often the central scanner restarts to refresh the peer list (ms). */
export const SCAN_CYCLE_MS = 15_000

/** How long to wait for a GATT connection before giving up (ms). */
export const CONNECT_TIMEOUT_MS = 15_000

/** How long to wait for fragment reassembly before discarding (ms). */
export const FRAGMENT_REASSEMBLY_TIMEOUT_MS = 30_000

/** Delay between consecutive reconnect attempts (ms). */
export const RECONNECT_BASE_DELAY_MS = 800

/** Max reconnect attempts before marking a peer as unreachable. */
export const MAX_RECONNECT_ATTEMPTS = 4

/** Inactivity window after which a peer is considered stale (ms). */
export const PEER_STALE_TIMEOUT_MS = 60_000

/** How long a presence heartbeat broadcast is considered fresh (ms). */
export const PRESENCE_TTL_MS = 30_000

/** Interval between outbound presence heartbeats (ms). */
export const PRESENCE_HEARTBEAT_MS = 20_000

/** Default connection keep-alive; ephemeral connections are closed after this idle period (ms). */
export const EPHEMERAL_IDLE_TIMEOUT_MS = 8_000

// ─── Async Storage Keys ───────────────────────────────────────────────────────
export const STORAGE_KEY_PEER_ID = '@mesh/peerId'
export const STORAGE_KEY_DISPLAY_NAME = '@mesh/displayName'
export const STORAGE_KEY_GROUPS = '@mesh/groups'
export const STORAGE_KEY_MESSAGES_PREFIX = '@mesh/messages/'