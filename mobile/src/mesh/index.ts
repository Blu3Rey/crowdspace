/**
 * @file index.ts
 * Public API surface of the anon-mesh core package.
 * Feature layers (direct messages, group chat, device locating, etc.)
 * should only import from here — never from internal submodules.
 */

// ── Engine ────────────────────────────────────────────────────────────────────
export { generateIdentityKeyPair, MeshEngine } from './MeshEngine'
export type { MeshEventMap } from './MeshEngine'

// ── React hook ────────────────────────────────────────────────────────────────
export {
    useMesh, utf8Decode, utf8Encode
} from './useMesh'
export type { MeshStatus, UseMeshResult } from './useMesh'

// ── Types (re-exported for feature layers) ────────────────────────────────────
export type {
    CausalNode, Contact, KeyPair,
    MeshEngineOptions, MeshMessage, NearbyPeer, RadioPhase,
    RadioStatus, RotatingToken,
    TransportKind
} from './core/types'

export { ContentType, PacketType } from './core/types'

// ── Causal message store (for feature layers that manage their own threads) ───
export { CausalMessageStore } from './messaging/packetCodec'

// ── Encoding utilities (for feature layers serialising custom content types) ──
export {
    bytesToHex, hexToBytes, packInnerPlaintext,
    unpackInnerPlaintext
} from './core/encoding'
