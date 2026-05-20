/**
 * BLE Mesh Network — Shared Types
 *
 * Single source-of-truth for all public and internal type definitions.
 * Import from here rather than defining inline to keep cross-module
 * references consistent and refactor-safe.
 */

// ─── Transport ────────────────────────────────────────────────────────────────

/** The physical channel used to deliver a frame. */
export type Transport = 'ble-gatt' | 'multipeer'

// ─── Peer ─────────────────────────────────────────────────────────────────────

/** Stable capabilities a peer advertises at connection time. */
export type PeerCapability = 'dm' | 'group' | 'locate' | 'presence'

/** Lifecycle state of a discovered/connected peer. */
export type PeerConnectionState =
  | 'discovered'    // seen in scan results, no connection yet
  | 'connecting'    // GATT connect in progress
  | 'connected'     // GATT connected, services discovered
  | 'subscribed'    // also subscribed to MSG_NOTIFY_CHAR
  | 'disconnected'  // previously connected, now disconnected
  | 'unreachable'   // max reconnect attempts exhausted

/** Peer availability for the presence feature. */
export type PresenceStatus = 'online' | 'away' | 'busy' | 'offline'

/** Full peer descriptor stored in PeerRegistry. */
export interface Peer {
  /** Stable peer UUID (persisted by the remote device). */
  id: string
  /** Human-readable display name. */
  displayName: string
  /** BLE device ID as returned by munim-bluetooth (platform-specific). */
  bleDeviceId: string | null
  /** Multipeer peer ID (iOS only). */
  multipeerPeerId: string | null
  /** Advertised capabilities. */
  capabilities: PeerCapability[]
  /** Current lifecycle state. */
  connectionState: PeerConnectionState
  /** Current presence status (updated by Presence feature). */
  presenceStatus: PresenceStatus
  /** Last RSSI reading in dBm. */
  rssi: number | null
  /** Smoothed RSSI (rolling average). */
  rssiSmoothed: number | null
  /** Estimated distance in metres (derived from RSSI). */
  estimatedDistance: number | null
  /** Unix timestamp (ms) of last observed activity. */
  lastSeen: number
  /** Transport by which this peer was most recently reachable. */
  preferredTransport: Transport
}

/** Compact peer info transmitted in the PEER_INFO_CHAR characteristic. */
export interface PeerInfoPayload {
  /** Stable peer UUID. */
  id: string
  /** Display name. */
  name: string
  /** Protocol version. */
  v: number
  /** Supported capabilities. */
  caps: PeerCapability[]
}

// ─── Wire Protocol ────────────────────────────────────────────────────────────

/** All logical message types routed by TransportManager / features. */
export type MessageKind =
  | 'dm'            // direct message (text / binary payload)
  | 'dm_ack'        // delivery acknowledgement for a DM
  | 'group'         // group chat message
  | 'group_ack'     // delivery acknowledgement for a group message
  | 'group_invite'  // invitation to join a group
  | 'group_meta'    // group metadata update (name, members)
  | 'presence'      // presence heartbeat
  | 'ping'          // liveness probe
  | 'pong'          // liveness probe response
  | 'locate_req'    // request RSSI-based range info
  | 'locate_res'    // RSSI range response

/**
 * Compact wire frame transmitted per GATT write or Multipeer send.
 * All optional fields are present only in the first fragment (p === 0).
 */
export interface WireFrame {
  /** Protocol version (always PROTOCOL_VERSION). */
  v: number
  /** First 8 chars of the logical message UUID (used for fragment reassembly). */
  id: string
  /** Zero-based part index. */
  p: number
  /** Total number of parts (1 = single, non-fragmented). */
  n: number
  /** Sender peer ID (first fragment only). */
  f?: string
  /** Recipient peer ID or group ID; null = broadcast (first fragment only). */
  t?: string | null
  /** MessageKind (first fragment only). */
  k?: MessageKind
  /** Unix timestamp ms (first fragment only). */
  ts?: number
  /** Base64-encoded payload chunk. */
  d: string
}

// ─── Logical Messages ─────────────────────────────────────────────────────────

/** Base fields shared by every fully-assembled logical message. */
export interface BaseMessage {
  /** Full UUID for this message. */
  msgId: string
  /** Sender peer ID. */
  from: string
  /** Unix timestamp ms. */
  timestamp: number
  /** Message kind. */
  kind: MessageKind
  /** Transport this message arrived on. */
  transport: Transport
}

/** Direct message payload. */
export interface DirectMessage extends BaseMessage {
  kind: 'dm'
  /** Recipient peer ID. */
  to: string
  /** Decoded text content. */
  text: string
  /** Optional raw binary payload (base64). */
  rawPayload?: string
}

/** DM acknowledgement. */
export interface DmAck extends BaseMessage {
  kind: 'dm_ack'
  /** ID of the DM being acknowledged. */
  ackedMsgId: string
}

/** Group chat message payload. */
export interface GroupMessage extends BaseMessage {
  kind: 'group'
  /** Group UUID. */
  groupId: string
  /** Sender peer ID. */
  from: string
  /** Decoded text content. */
  text: string
  /** Optional raw binary payload (base64). */
  rawPayload?: string
}

/** Group invitation payload. */
export interface GroupInvite extends BaseMessage {
  kind: 'group_invite'
  /** Group UUID. */
  groupId: string
  /** Human-readable group name. */
  groupName: string
  /** Inviting peer ID. */
  invitedBy: string
  /** Initial member list. */
  members: string[]
}

/** Group metadata update. */
export interface GroupMeta extends BaseMessage {
  kind: 'group_meta'
  groupId: string
  name?: string
  members?: string[]
}

/** Presence heartbeat payload. */
export interface PresenceMessage extends BaseMessage {
  kind: 'presence'
  status: PresenceStatus
  /** Display name of the sender (helps peers skip a GATT read). */
  displayName: string
}

/** Ping/pong messages. */
export interface PingMessage extends BaseMessage {
  kind: 'ping' | 'pong'
  /** Echo back the same nonce to match request/response pairs. */
  nonce: string
}

/** Device locator request. */
export interface LocateRequest extends BaseMessage {
  kind: 'locate_req'
  nonce: string
}

/** Device locator response. */
export interface LocateResponse extends BaseMessage {
  kind: 'locate_res'
  nonce: string
  /** RSSI from the peer's perspective of the requester. */
  peerRssi: number | null
}

/** Union of all logical message types. */
export type MeshMessage =
  | DirectMessage
  | DmAck
  | GroupMessage
  | GroupInvite
  | GroupMeta
  | PresenceMessage
  | PingMessage
  | LocateRequest
  | LocateResponse

// ─── Groups ───────────────────────────────────────────────────────────────────

/** A group chat group tracked by the local device. */
export interface Group {
  id: string
  name: string
  /** Peer IDs of all members (includes self). */
  members: string[]
  /** Peer ID of the original creator. */
  createdBy: string
  createdAt: number
  updatedAt: number
}

// ─── Store ────────────────────────────────────────────────────────────────────

/** Persisted message record (direct or group). */
export interface StoredMessage {
  msgId: string
  kind: 'dm' | 'group'
  from: string
  to: string       // peer ID for DM, group ID for group
  text: string
  rawPayload?: string
  timestamp: number
  delivered: boolean
  read: boolean
  transport: Transport
}

// ─── EventBus Topics ─────────────────────────────────────────────────────────

/** Typed event map used by the internal EventBus. */
export interface MeshEventMap {
  // Peer lifecycle
  'peer:discovered': Peer
  'peer:updated': Peer
  'peer:connected': Peer
  'peer:disconnected': Peer
  'peer:lost': Peer

  // Messages received
  'message:dm': DirectMessage
  'message:dm_ack': DmAck
  'message:group': GroupMessage
  'message:group_invite': GroupInvite
  'message:group_meta': GroupMeta
  'message:presence': PresenceMessage
  'message:ping': PingMessage
  'message:pong': PingMessage
  'message:locate_req': LocateRequest
  'message:locate_res': LocateResponse

  // Engine status
  'engine:ready': { selfId: string; displayName: string }
  'engine:stopped': void
  'engine:error': { error: Error; context: string }

  // Background session
  'background:started': void
  'background:stopped': void
  'background:restored': { isScanning: boolean; isAdvertising: boolean }

  // Delivery acknowledgement
  'ack:dm': { msgId: string; from: string }
  'ack:group': { msgId: string; groupId: string; from: string }
}

// ─── Configuration ────────────────────────────────────────────────────────────

/** Configuration passed to MeshEngine.init(). */
export interface MeshEngineConfig {
  /** This device's stable peer UUID (load from storage or generate). */
  selfId: string
  /** Display name shown to nearby peers. */
  displayName: string
  /** Whether to also start a background session. Default: false. */
  background?: boolean
  /** Android foreground-service notification text. */
  androidNotificationTitle?: string
  androidNotificationText?: string
  /** Features to enable. Defaults to all. */
  features?: ('dm' | 'group' | 'locate' | 'presence')[]
  /** Override default chunk size (bytes). */
  chunkBytes?: number
}

// ─── Result Types ─────────────────────────────────────────────────────────────

export type Result<T, E = Error> =
  | { ok: true; value: T }
  | { ok: false; error: E }