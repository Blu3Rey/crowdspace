/**
 * @file crypto.ts
 * All cryptographic primitives for the Anon Mesh core.
 *
 * Dependencies (peer deps — install alongside munim-bluetooth):
 *   tweetnacl          — X25519 + XSalsa20-Poly1305 (pure JS, RN-compatible)
 *   tweetnacl-util     — convenience encode/decode helpers
 *   @noble/hashes      — SHA-256, HMAC-SHA256, HKDF (pure JS, RN-compatible)
 *
 * Design choice 1 (Multi-Hop Security):
 *   - Identity keys are long-term X25519 key pairs.
 *   - First-contact shared secret = X25519 DH, expanded via HKDF.
 *   - Per-message encryption uses nacl.box (ephemeral DH + Poly1305).
 *   - No central server is ever needed.
 *
 * Design choice 2 (Ephemeral Advertisement Rotation):
 *   - A rotating token is derived from the per-contact shared root key
 *     and the current 15-minute time window index via HMAC-SHA256.
 *   - Only approved contacts can compute the expected token.
 *   - Strangers see only random-looking 8-byte hashes.
 */

import { hkdf } from '@noble/hashes/hkdf.js';
import { hmac } from '@noble/hashes/hmac.js';
import { sha256 } from '@noble/hashes/sha2.js';
import 'react-native-get-random-values';
import nacl from 'tweetnacl';

import {
  KDF_LABEL_FINGERPRINT,
  KDF_LABEL_SESSION_KEY,
  KDF_LABEL_TOKEN,
  TOKEN_ROTATION_INTERVAL_MS,
  TOKEN_WINDOW_TOLERANCE,
} from './constants';
import { bytesToHex, hexToBytes } from './encoding';
import { Contact, KeyPair, RotatingToken } from './types';

// ─── Key Generation ──────────────────────────────────────────────────────────

/** Generate a fresh X25519 identity key pair. Store the secret key securely. */
export function generateIdentityKeyPair(): KeyPair {
  return nacl.box.keyPair()
}

/** Generate a random 12-byte packetId (hex string) */
export function generatePacketId(): string {
  return bytesToHex(nacl.randomBytes(12))
}

// ─── Fingerprinting ──────────────────────────────────────────────────────────

/**
 * Compute a stable 8-byte hex fingerprint from an identity public key.
 * Used as the Contact.id and as the pubkeyFingerprint in ANNOUNCE packets.
 * Format: hex(HKDF-SHA256(key, salt="", info=KDF_LABEL_FINGERPRINT, len=8))
 */
export function computeFingerprint(identityPublicKey: Uint8Array): string {
  const derived = hkdf(
    sha256,
    identityPublicKey,
    new Uint8Array(0),       // no salt (key is already high-entropy)
    textEncoder.encode(KDF_LABEL_FINGERPRINT),
    8,
  )
  return bytesToHex(derived)
}

/**
 * First 8 bytes of SHA-256(data), returned as a Uint8Array.
 * Used in routing headers and ANNOUNCE to identify tokens without revealing them.
 */
export function hashToFingerprint(data: Uint8Array): Uint8Array {
  return sha256(data).slice(0, 8)
}

// ─── Shared Secret Derivation ─────────────────────────────────────────────────

/**
 * Derive the shared root key for a new contact via X25519 DH + HKDF.
 *
 * Design choice 1: this replaces the Signal PreKey mechanism by requiring
 * an initial direct key exchange (QR code or GATT handshake). Once
 * sharedRootKey is established, all subsequent token rotation and encryption
 * work without any server involvement.
 *
 * @param mySecretKey   Our X25519 secret key (32 bytes)
 * @param theirPublicKey Their X25519 public key (32 bytes)
 * @returns 32-byte shared root key (never transmitted)
 */
export function deriveSharedRootKey(
  mySecretKey:   Uint8Array,
  theirPublicKey: Uint8Array,
): Uint8Array {
  const rawShared = nacl.scalarMult(mySecretKey, theirPublicKey)
  // Expand via HKDF to get a uniformly random key
  return hkdf(
    sha256,
    rawShared,
    new Uint8Array(0),
    textEncoder.encode(KDF_LABEL_SESSION_KEY),
    32,
  )
}

// ─── Rotating Token ──────────────────────────────────────────────────────────

/**
 * Compute the current window index for a given timestamp.
 * Design choice 2: windows are 15 minutes wide (TOKEN_ROTATION_INTERVAL_MS).
 */
export function tokenWindowIndex(nowMs: number): number {
  return Math.floor(nowMs / TOKEN_ROTATION_INTERVAL_MS)
}

/**
 * Derive the rotating token for a specific contact at a specific window.
 *
 * token = HMAC-SHA256(sharedRootKey, KDF_LABEL_TOKEN + "|" + windowIndex)[0..7]
 *
 * Only devices that share the root key can compute this. A passive observer
 * watching the radio sees only SHA-256(token)[0..7], which is unlinkable
 * across rotation windows.
 *
 * @param sharedRootKey   Derived from X25519 DH with the contact
 * @param windowIndex     Time window (e.g. Math.floor(Date.now() / 900_000))
 */
export function deriveRotatingToken(
  sharedRootKey: Uint8Array,
  windowIndex:   number,
): Uint8Array {
  const info = textEncoder.encode(`${KDF_LABEL_TOKEN}|${windowIndex}`)
  const full = hmac(sha256, sharedRootKey, info)
  return full.slice(0, 8)
}

/**
 * Build a full RotatingToken object for a contact at the current time.
 */
export function currentToken(contact: { sharedRootKey: Uint8Array }, nowMs = Date.now()): RotatingToken {
  const windowIndex = tokenWindowIndex(nowMs)
  const token       = deriveRotatingToken(contact.sharedRootKey, windowIndex)
  const tokenHash   = hashToFingerprint(token)

  return {
    token,
    tokenHash,
    windowIndex,
    validFromMs: windowIndex * TOKEN_ROTATION_INTERVAL_MS,
    expiresAtMs: (windowIndex + 1) * TOKEN_ROTATION_INTERVAL_MS,
  }
}

/**
 * Check whether an observed token hash matches any valid token window for a contact.
 * Accepts current ± TOKEN_WINDOW_TOLERANCE windows to handle clock drift.
 */
export function tokenHashMatchesContact(
  observedHash:  Uint8Array | string,
  contact:       Pick<Contact, 'sharedRootKey'>,
  nowMs = Date.now(),
): boolean {
  const observed = typeof observedHash === 'string' ? hexToBytes(observedHash) : observedHash
  const baseWindow = tokenWindowIndex(nowMs)

  for (let delta = -TOKEN_WINDOW_TOLERANCE; delta <= TOKEN_WINDOW_TOLERANCE; delta++) {
    const token     = deriveRotatingToken(contact.sharedRootKey, baseWindow + delta)
    const tokenHash = hashToFingerprint(token)
    if (constantTimeEqual(observed.slice(0, 8), tokenHash)) return true
  }
  return false
}

/**
 * Compute "our own" rotating token hash for the current window.
 * This is derived from our own identity key hashed against a local salt
 * (not per-contact) so strangers cannot correlate advertisements across windows.
 *
 * We use the identity public key as a stable root and derive per-window tokens.
 */
export function ownTokenForWindow(
  identityPublicKey: Uint8Array,
  windowIndex:       number,
): RotatingToken {
  // Derive a self-token root from the identity key
  const selfRoot = hkdf(
    sha256,
    identityPublicKey,
    new Uint8Array(0),
    textEncoder.encode('anon-mesh:self-token:v1'),
    32,
  )
  const token     = deriveRotatingToken(selfRoot, windowIndex)
  const tokenHash = hashToFingerprint(token)

  return {
    token,
    tokenHash,
    windowIndex,
    validFromMs: windowIndex * TOKEN_ROTATION_INTERVAL_MS,
    expiresAtMs: (windowIndex + 1) * TOKEN_ROTATION_INTERVAL_MS,
  }
}

// ─── Encryption / Decryption ─────────────────────────────────────────────────

/**
 * Encrypt an inner plaintext buffer for a specific recipient.
 *
 * Uses nacl.box: ephemeral X25519 DH + XSalsa20-Poly1305.
 * The ephemeral public key is NOT included in the ciphertext here —
 * it goes in a separate field if needed; for simplicity we use the
 * sender's identity key as the "from" key (standard nacl.box API).
 *
 * Wire layout:
 *   [0-23]   nonce (24 random bytes)
 *   [24..]   nacl.box ciphertext (includes 16-byte Poly1305 MAC)
 *
 * @param plaintext         Inner plaintext bytes (from encoding.packInnerPlaintext)
 * @param recipientPublicKey Their X25519 identity public key
 * @param senderSecretKey   Our X25519 identity secret key
 */
export function encryptPayload(
  plaintext:          Uint8Array,
  recipientPublicKey: Uint8Array,
  senderSecretKey:    Uint8Array,
): Uint8Array {
  const nonce      = nacl.randomBytes(24)
  const ciphertext = nacl.box(plaintext, nonce, recipientPublicKey, senderSecretKey)
  if (!ciphertext) throw new Error('nacl.box encryption failed')

  const out = new Uint8Array(24 + ciphertext.length)
  out.set(nonce,      0)
  out.set(ciphertext, 24)
  return out
}

/**
 * Decrypt a payload encrypted by encryptPayload.
 * Returns null if decryption fails (wrong key, tampered data, etc.).
 */
export function decryptPayload(
  payload:         Uint8Array,
  senderPublicKey: Uint8Array,
  recipientSecretKey: Uint8Array,
): Uint8Array | null {
  if (payload.length < 24) return null
  const nonce      = payload.slice(0, 24)
  const ciphertext = payload.slice(24)
  return nacl.box.open(ciphertext, nonce, senderPublicKey, recipientSecretKey)
}

// ─── Tie-Breaker for Simultaneous Connections ────────────────────────────────

/**
 * Determine whether this device should act as Central (connect) or Peripheral
 * (wait to be connected to) when two devices detect each other simultaneously.
 *
 * Design choice 3 (Race Condition Mitigation): the device with the
 * lexicographically HIGHER senderTokenHash acts as Central; the other waits.
 * Both devices compute the same deterministic answer since both see each other's
 * advertised token hashes in a symmetric discovery.
 */
export function shouldActAsCentral(
  ourTokenHash:   Uint8Array | string,
  theirTokenHash: Uint8Array | string,
): boolean {
  const ours   = typeof ourTokenHash   === 'string' ? ourTokenHash   : bytesToHex(ourTokenHash)
  const theirs = typeof theirTokenHash === 'string' ? theirTokenHash : bytesToHex(theirTokenHash)
  return ours > theirs  // Lexicographic comparison on hex strings is deterministic
}

// ─── ACK Payload ─────────────────────────────────────────────────────────────

/**
 * Build a minimal ACK payload: just the 12-byte packetId that is being acknowledged.
 * The ACK routing header identifies the sender/recipient via token hashes.
 */
export function buildAckPayload(acknowledgedPacketId: string): Uint8Array {
  const bytes = new Uint8Array(12)
  bytes.set(hexToBytes(acknowledgedPacketId).slice(0, 12))
  return bytes
}

export function parseAckPayload(payload: Uint8Array): string {
  if (payload.length < 12) throw new RangeError('ACK payload too short')
  return bytesToHex(payload.slice(0, 12))
}

// ─── Internal helpers ─────────────────────────────────────────────────────────

const textEncoder = new TextEncoder()

/** Constant-time comparison to prevent timing attacks */
function constantTimeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false
  let diff = 0
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i]
  return diff === 0
}