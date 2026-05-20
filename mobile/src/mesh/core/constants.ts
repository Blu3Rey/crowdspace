/**
 * @file constants.ts
 * All compile-time constants for the Anon Mesh protocol.
 * Values here are the single source of truth — nothing is hard-coded elsewhere.
 */

// ─── GATT UUIDs ─────────────────────────────────────────────────────────────

/**
 * Fixed mesh service UUID scanned/filtered by centrals.
 * Using a 128-bit UUID avoids collision with standard Bluetooth SIG services.
 * Stays fixed so clients can filter scans efficiently.
 */
export const MESH_SERVICE_UUID = 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'

/**
 * ANNOUNCE (read + notify):
 *   16 bytes — [tokenHash(8)] [pubkeyFingerprint(8)]
 *   Read by an incoming central to identify the peripheral and decide whether
 *   they're a known contact. Notified on token rotation.
 */
export const CHAR_ANNOUNCE = 'a1b2c3d4-0001-7890-abcd-ef1234567890'

/**
 * INBOX (write + writeWithoutResponse):
 *   Chunked MeshPacket bytes destined *for this device*.
 *   Use writeWithoutResponse for throughput; fall back to write for reliability.
 */
export const CHAR_INBOX = 'a1b2c3d4-0002-7890-abcd-ef1234567890'

/**
 * RELAY (write + writeWithoutResponse):
 *   Chunked MeshPacket bytes destined for *someone else*.
 *   Separated from INBOX so the peripheral can apply different rate limits
 *   to relay traffic vs. direct messages.
 */
export const CHAR_RELAY = 'a1b2c3d4-0003-7890-abcd-ef1234567890'

/**
 * ACK (notify):
 *   12 bytes — the packetId of the last successfully delivered message.
 *   The peripheral pushes this to all subscribed centrals.
 */
export const CHAR_ACK = 'a1b2c3d4-0004-7890-abcd-ef1234567890'

// ─── Wire format sizes ───────────────────────────────────────────────────────

/** Protocol version byte */
export const PROTOCOL_VERSION = 0x01

/**
 * Routing header packed size (bytes):
 *   1  version
 *  12  packetId (random bytes, not a UUID string)
 *   8  targetTokenHash
 *   8  senderTokenHash
 *   1  ttl
 *   1  hopCount
 *   1  type
 *   4  timestampSec (uint32 big-endian)
 * ─────
 *  36  total
 */
export const ROUTING_HEADER_SIZE = 36

/**
 * Chunk header (bytes):
 *   1  streamId
 *   1  seqNum
 *   1  totalChunks
 * ─────
 *   3  total
 */
export const CHUNK_HEADER_SIZE = 3

/** NaCl box nonce size */
export const NACL_NONCE_SIZE = 24

/** NaCl box overhead (Poly1305 MAC) */
export const NACL_BOX_OVERHEAD = 16

// ─── MTU budget ──────────────────────────────────────────────────────────────

/**
 * Target ATT MTU (negotiated on Android; iOS handles internally).
 * Modern BLE 5.0 devices support up to 512; we target 247 as the widest
 * value reliably achievable across both platforms.
 */
export const TARGET_MTU = 247

/**
 * ATT protocol subtracts 3 bytes for opcode + handle from each write.
 * Then we reserve CHUNK_HEADER_SIZE for our own chunking framing.
 * Result: 241 usable payload bytes per write.
 */
export const CHUNK_PAYLOAD_SIZE = TARGET_MTU - 3 - CHUNK_HEADER_SIZE  // = 241

/**
 * Absolute maximum packet size (header + encrypted payload).
 * = 253 chunks × 241 bytes/chunk ≈ 61 KB. We cap conservatively at 16 KB
 * to stay well within BLE memory limits on constrained devices.
 */
export const MAX_PACKET_BYTES = 16_384

/**
 * Maximum raw content bytes before encryption.
 * Leaves room for header, nonce, and NaCl overhead within MAX_PACKET_BYTES.
 */
export const MAX_CONTENT_BYTES = 8_192

// ─── Routing ─────────────────────────────────────────────────────────────────

/** Default TTL for outbound DATA packets */
export const DEFAULT_TTL = 7

/** Spray-and-wait: number of copies to spray before waiting */
export const DEFAULT_SPRAY_FACTOR = 4

/** How long (ms) to cache a seen packetId to suppress relay loops */
export const SEEN_CACHE_TTL_MS = 24 * 60 * 60 * 1_000  // 24 hours

/** Maximum number of seen packetIds to hold in the LRU cache */
export const SEEN_CACHE_MAX_SIZE = 4_096

/** Maximum number of simultaneous outbound GATT connections */
export const MAX_CONCURRENT_CONNECTIONS = 5

/** Timeout (ms) before abandoning a partially assembled chunked stream */
export const STREAM_ASSEMBLY_TIMEOUT_MS = 30_000

// ─── Token rotation ──────────────────────────────────────────────────────────

/**
 * Token rotation window (ms). Design choice 2: 15 minutes mirrors
 * Apple's Find My and Google/Apple Contact Tracing rotation period,
 * balancing privacy against the need for contacts to re-identify you.
 */
export const TOKEN_ROTATION_INTERVAL_MS = 15 * 60 * 1_000

/**
 * We also accept tokens from the previous and next window to handle
 * clock drift and edge-of-window timing between devices.
 */
export const TOKEN_WINDOW_TOLERANCE = 1

/** HKDF derivation labels (ASCII, used as salt/info bytes) */
export const KDF_LABEL_TOKEN        = 'anon-mesh:token:v1'
export const KDF_LABEL_SESSION_KEY  = 'anon-mesh:session:v1'
export const KDF_LABEL_FINGERPRINT  = 'anon-mesh:fingerprint:v1'

// ─── Radio orchestration ─────────────────────────────────────────────────────

/**
 * Advertising phase duration.
 * Design choice 3: asynchronous time-slicing since the BLE chip cannot
 * scan and advertise at full power simultaneously.
 */
export const DEFAULT_ADVERTISING_PHASE_MS = 800

/** Scanning phase duration */
export const DEFAULT_SCANNING_PHASE_MS = 1_200

/**
 * How long (ms) to attempt a GATT connection before timing out.
 * munim-bluetooth has a native 15 s connect timeout; we use a shorter
 * app-level timeout to keep the radio loop responsive.
 */
export const GATT_CONNECT_TIMEOUT_MS = 10_000

/** How long to hold open an idle GATT connection before closing it */
export const GATT_IDLE_CLOSE_MS = 8_000

// ─── Multipeer (iOS) ─────────────────────────────────────────────────────────

/**
 * Bonjour service type for Apple Multipeer Connectivity.
 * Must be declared in Info.plist as _anon-mesh._tcp.
 * 1–15 lowercase letters/numbers/hyphens (MCSession requirement).
 */
export const MULTIPEER_SERVICE_TYPE = 'anon-mesh'