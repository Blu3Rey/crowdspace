/**
 * BLE Mesh Network — Public API
 *
 * Everything a consumer needs is re-exported from here.
 * Internal modules (BLEEngine, EventBus, etc.) are NOT re-exported to keep
 * the public surface minimal and the internals refactor-safe.
 */

// ── Engine ────────────────────────────────────────────────────────────────────
export { bootstrapPeerIdentity, MeshEngine, setDisplayName } from './MeshEngine'

// ── Store ─────────────────────────────────────────────────────────────────────
export { setAsyncStorage } from './store/MessageStore'

// ── Hooks ─────────────────────────────────────────────────────────────────────
export {
    useDeviceLocator, useDirectMessage,
    useGroupChat, useMeshNetwork, usePresence
} from './hooks'

// ── Types ─────────────────────────────────────────────────────────────────────
export type {
    DirectMessage,
    DmAck,
    // Groups
    Group, GroupInvite, GroupMessage, GroupMeta, LocateRequest,
    LocateResponse,
    // Config
    MeshEngineConfig,
    // Messages
    MeshMessage, MessageKind,
    // Peers
    Peer,
    PeerCapability,
    PeerConnectionState, PeerInfoPayload, PingMessage, PresenceMessage, PresenceStatus,
    // Results
    Result, StoredMessage,
    // Transport
    Transport
} from './types/ble'

export type { RangeResult } from './features/DeviceLocator'

export type {
    UseDeviceLocatorResult, UseDMResult,
    UseGroupChatResult, UseMeshNetworkResult, UsePresenceResult
} from './hooks'

// ── Constants (opt-in) ────────────────────────────────────────────────────────
export {
    MESH_SERVICE_UUID, MSG_NOTIFY_CHAR_UUID, MSG_WRITE_CHAR_UUID, MULTIPEER_SERVICE_TYPE, PEER_INFO_CHAR_UUID, PROTOCOL_VERSION
} from './constants/ble'
