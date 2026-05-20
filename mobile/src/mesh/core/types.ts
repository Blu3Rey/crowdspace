/**
 * @file types.ts
 * All shared type definitions for the Anon Mesh decentralised messaging core.
 * No runtime code lives here — only pure TypeScript interfaces/enums.
 */

// ─── Wire-level packet classification ──────────────────────────────────────

/**
 * PacketType occupies 1 byte in the routing header.
 * Any relay hop can read this to decide how to handle the packet.
 */
export const enum PacketType {
  DATA       = 0x01, // Encrypted unicast message for a specific recipient
  RELAY      = 0x02, // Epidemic relay hop; payload stays encrypted for ultimate recipient
  ACK        = 0x03, // Delivery acknowledgement; small plaintext payload
  HANDSHAKE  = 0x04, // First-contact public-key exchange
  BEACON     = 0x05, // Presence announcement; zero-byte payload
}

/**
 * ContentType is encoded in the inner plaintext (post-decryption).
 * Higher-level features map to their own content types, keeping
 * the transport layer fully domain-agnostic.
 */
export const enum ContentType {
  TEXT           = 0x01,
  BINARY         = 0x02, // Arbitrary binary blob (e.g. small file chunk)
  REACTION       = 0x03, // Emoji reaction referencing a parent message ID
  GROUP_INVITE   = 0x04,
  GROUP_MESSAGE  = 0x05,
  LOCATION_HINT  = 0x06, // Coarse location for the device-locating feature
  READ_RECEIPT   = 0x07,
  CUSTOM         = 0xFF, // Extension point for future feature types
}

// ─── Routing Header ─────────────────────────────────────────────────────────

/**
 * RoutingHeader is ALWAYS plaintext — readable by every relay hop.
 * It must contain everything needed to route the packet without decrypting
 * the payload. Packed into exactly ROUTING_HEADER_SIZE bytes on the wire.
 *
 * Design choice 1 (Payload Decoupling): the header reveals nothing about
 * message content; the target is identified only by an ephemeral token hash.
 */
export interface RoutingHeader {
  /** Protocol version byte. Current: 0x01 */
  version: number

  /** 12-byte hex string. Globally unique, random per packet. */
  packetId: string

  /**
   * First 8 bytes of SHA-256(recipient's current rotating token), hex-encoded.
   * Allows the recipient (and known contacts acting as relays) to recognise
   * packets without revealing who the recipient is to unknown hops.
   */
  targetTokenHash: string

  /**
   * First 8 bytes of SHA-256(sender's current rotating token), hex-encoded.
   * Used for sending ACKs back along the path and for tie-breaking.
   */
  senderTokenHash: string

  /** Remaining relay hops. Decremented by each relay. 0 = do not forward. */
  ttl: number

  /** Hop count so far (informational, monotonically increasing). */
  hopCount: number

  type: PacketType

  /** Unix seconds at the origin device. Truncated to uint32 for compactness. */
  timestampSec: number
}

/** A complete mesh packet ready to transmit or freshly received. */
export interface MeshPacket {
  header:  RoutingHeader
  /**
   * For DATA/RELAY: NaCl box ciphertext (nonce prepended, 24 bytes).
   * For HANDSHAKE: packed identity public key + ephemeral DH key.
   * For ACK: 12-byte packetId of the acknowledged packet.
   * For BEACON: zero-length.
   */
  payload: Uint8Array
}

// ─── Application Layer ──────────────────────────────────────────────────────

/**
 * A decrypted, fully resolved application-level message.
 * This is what the feature layer (DM, group chat, etc.) works with.
 */
export interface MeshMessage {
  /** = packetId of the originating DATA packet */
  id: string

  /** Stable identity fingerprint: hex(SHA-256(senderPublicKey))[0..15] */
  senderId: string

  /** Stable identity fingerprint of the intended recipient */
  recipientId: string

  contentType: ContentType

  /** Raw content bytes. Interpretation depends on contentType. */
  content: Uint8Array

  /**
   * Causal parent message IDs from the sender's perspective.
   * Design choice 5 (CRDTs / Causal Ordering): every message explicitly
   * references its causal predecessors, forming a DAG that survives
   * network partitions and clock drift.
   */
  parentIds: string[]

  /** Origin timestamp in milliseconds (from the sender's clock). */
  timestampMs: number

  /** Local wall-clock time when this device received the message. */
  receivedAtMs: number

  /** Number of relay hops taken (from routing header). */
  hopCount: number

  /** Signal strength of the last delivering peer, if available. */
  rssi?: number
}

/** A node in the per-conversation causal DAG. */
export interface CausalNode {
  message:  MeshMessage
  parentIds: string[]
  childIds:  string[]
}

// ─── Identity & Cryptography ─────────────────────────────────────────────────

/** X25519 key pair. Identity key is long-term; ephemeral keys are per-session. */
export interface KeyPair {
  publicKey: Uint8Array  // 32 bytes
  secretKey: Uint8Array  // 32 bytes. Never transmitted; never logged.
}

/**
 * Rotating advertisement token.
 *
 * Design choice 2 (Ephemeral Advertisement Rotation): derived from a root key
 * shared only between approved contacts. Changes every TOKEN_ROTATION_INTERVAL_MS.
 * Strangers observe only random-looking 8-byte hashes in routing headers.
 */
export interface RotatingToken {
  /** 8-byte raw token (not sent directly; its hash goes in headers). */
  token:       Uint8Array
  /** First 8 bytes of SHA-256(token). Goes in routing headers and advertisements. */
  tokenHash:   Uint8Array
  windowIndex: number
  validFromMs: number
  expiresAtMs: number
}

/**
 * A known, approved contact. Persisted across sessions.
 */
export interface Contact {
  /**
   * Stable identifier: hex(SHA-256(identityPublicKey))[0..15]
   * Human-readable fingerprint; matches the senderId in MeshMessage.
   */
  id: string

  /** Their X25519 public key (32 bytes). Obtained via QR or first-contact GATT handshake. */
  identityPublicKey: Uint8Array

  /**
   * Shared root key: HKDF over X25519(mySecretKey, theirPublicKey).
   * Used to derive rotating tokens and session encryption keys.
   * Never transmitted.
   */
  sharedRootKey: Uint8Array

  alias?:     string
  addedAtMs:  number
  lastSeenMs?: number
  lastRssi?:  number
}

// ─── Transport Layer ────────────────────────────────────────────────────────

/** Transport kinds understood by the engine */
export type TransportKind = 'ble-gatt' | 'multipeer'

/** A nearby BLE/Multipeer device, not yet verified as a known contact */
export interface NearbyPeer {
  /** munim-bluetooth device ID or Multipeer peer ID */
  deviceId:       string
  /** 8-byte hex token hash observed from the ANNOUNCE characteristic or header */
  tokenHash:      string
  transport:      TransportKind
  discoveredAtMs: number
  rssi?:          number
}

// ─── Chunking Layer ─────────────────────────────────────────────────────────

/**
 * 3-byte header prepended to every BLE GATT write.
 * Handles fragmentation because BLE MTU is typically 241 usable bytes.
 *
 * Design choice 4 (MTU Budgeting): we pack tightly and never send padded JSON.
 */
export interface ChunkHeader {
  /** 0–255: disambiguates concurrent transfers to the same peer */
  streamId: number
  /** 0-indexed position of this chunk */
  seqNum: number
  /** Total number of chunks for this stream (1-indexed count) */
  totalChunks: number
}

/** State for a partially assembled inbound chunked stream */
export interface PendingStream {
  streamId:    number
  totalChunks: number
  /** Map of seqNum → raw chunk payload bytes */
  chunks:      Map<number, Uint8Array>
  firstSeenMs: number
}

// ─── Radio Layer ─────────────────────────────────────────────────────────────

export type RadioPhase = 'advertising' | 'scanning' | 'connecting' | 'idle'

export interface RadioStatus {
  phase:         RadioPhase
  phaseStartedMs: number
}

// ─── Engine API ──────────────────────────────────────────────────────────────

export interface MeshEngineOptions {
  /** Max relay hops for outbound DATA packets. Default: 7 */
  defaultTTL?: number
  /** Spray-and-wait copies before entering "wait" phase. Default: 4 */
  sprayFactor?: number
  /** Token rotation interval in ms. Default: 900_000 (15 min) */
  tokenRotationIntervalMs?: number
  /** BLE advertising phase duration in ms. Default: 800 */
  advertisingPhaseMs?: number
  /** BLE scanning phase duration in ms. Default: 1200 */
  scanningPhaseMs?: number
  /** Start a background BLE session. Default: false */
  enableBackground?: boolean
  /** Android foreground notification text */
  androidNotificationText?: string
  /** Use Apple Multipeer for iOS-to-iOS. Default: true on iOS */
  enableMultipeer?: boolean
  /** Multipeer Bonjour service type. Must match Info.plist. Default: 'anon-mesh' */
  multipeerServiceType?: string
}

/** Typed event map for MeshEngine.on / MeshEngine.off */
export interface MeshEventMap {
  'message':             MeshMessage
  'peer:discovered':     NearbyPeer
  'peer:connected':      { deviceId: string; transport: TransportKind }
  'peer:disconnected':   { deviceId: string }
  'contact:nearby':      { contact: Contact; deviceId: string; rssi?: number }
  'packet:relayed':      { packetId: string; toPeer: string }
  'ack:received':        { packetId: string }
  'handshake:completed': { contactId: string }
  'radio:phase':         RadioStatus
  'error':               { code: string; message: string; detail?: unknown }
}