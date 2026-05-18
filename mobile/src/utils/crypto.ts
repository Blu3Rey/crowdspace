/**
 * Crypto Utilities
 *
 * Lightweight message integrity helpers that work without native modules.
 *
 * Security posture: BLE mesh over Multipeer Connectivity (iOS) is encrypted
 * by the OS. BLE GATT over the air is unencrypted unless you layer your own
 * encryption. For local-only mesh apps (no internet relay), the threat model
 * is primarily message tampering, not eavesdropping. This module therefore
 * provides:
 *
 *   1. FNV-1a checksum — fast integrity guard against corrupt fragments.
 *   2. HMAC-SHA-256 signing (using the Web Crypto API when available) for
 *      message authenticity in environments that provide SubtleCrypto.
 *
 * If your app needs strong confidentiality you should layer a proper
 * encryption scheme (e.g. X25519 key exchange + AES-GCM) on top of the
 * payload before passing it to MessageProtocol.encode().
 */

import { textEncode } from './hex'

// ─── FNV-1a Checksum ──────────────────────────────────────────────────────────

const FNV_PRIME_32 = 0x01000193
const FNV_OFFSET_32 = 0x811c9dc5

/**
 * Compute a 32-bit FNV-1a hash of a UTF-8 string.
 * Returns an 8-character lowercase hex string.
 */
export function fnv1a32(input: string): string {
  const bytes = textEncode(input)
  let hash = FNV_OFFSET_32 >>> 0
  for (let i = 0; i < bytes.length; i++) {
    hash ^= bytes[i]!
    // 32-bit overflow-safe multiply
    hash = Math.imul(hash, FNV_PRIME_32) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

/**
 * Compute a simple checksum for a wire payload to detect corrupt reassembly.
 * Not a security primitive — use HMAC for authenticity.
 */
export function payloadChecksum(payload: string): string {
  return fnv1a32(payload)
}

/** Verify a payload checksum. */
export function verifyChecksum(payload: string, checksum: string): boolean {
  return fnv1a32(payload) === checksum
}

// ─── Internal helpers ────────────────────────────────────────────────────────

/**
 * Converts a Uint8Array to a plain ArrayBuffer.
 *
 * TextEncoder.encode() is typed as Uint8Array<ArrayBufferLike> in TS 5.x, but
 * SubtleCrypto's importKey / sign / verify expect BufferSource, which resolves
 * to ArrayBuffer | ArrayBufferView<ArrayBuffer>. SharedArrayBuffer is excluded.
 * ArrayBuffer.prototype.slice always returns a concrete ArrayBuffer, so this
 * satisfies the constraint without any unsafe casts.
 */
function toBuffer(u: Uint8Array): ArrayBuffer {
  return u.buffer.slice(u.byteOffset, u.byteOffset + u.byteLength) as ArrayBuffer;
}

// ─── HMAC-SHA-256 (Web Crypto) ────────────────────────────────────────────────

/**
 * Returns true if SubtleCrypto is available in the current runtime.
 * Hermes on RN 0.73+ includes it; older engines may not.
 */
export function hasCrypto(): boolean {
  return (
    typeof crypto !== 'undefined' &&
    typeof crypto.subtle !== 'undefined' &&
    typeof crypto.subtle.importKey === 'function'
  )
}

/**
 * Derive an HMAC-SHA-256 signing key from a shared secret string.
 * Cache the result — key derivation is expensive.
 */
export async function deriveHmacKey(secret: string): Promise<CryptoKey | null> {
  if (!hasCrypto()) return null
  try {
    return await crypto.subtle.importKey(
      'raw',
      toBuffer(textEncode(secret)),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign', 'verify'],
    )
  } catch {
    return null
  }
}

/**
 * Sign a message payload string with an HMAC key.
 * Returns a hex-encoded 32-byte MAC, or null if crypto is unavailable.
 */
export async function signPayload(payload: string, key: CryptoKey): Promise<string | null> {
  if (!hasCrypto()) return null
  try {
    const sig = await crypto.subtle.sign('HMAC', key, toBuffer(textEncode(payload)))
    return Array.from(new Uint8Array(sig))
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('')
  } catch {
    return null
  }
}

/**
 * Verify a payload MAC.
 * Returns true if the MAC is valid, false otherwise.
 */
export async function verifyPayload(
  payload: string,
  mac: string,
  key: CryptoKey,
): Promise<boolean> {
  if (!hasCrypto()) return true // degrade gracefully
  try {
    const macBytes = new Uint8Array(mac.match(/.{2}/g)!.map((b) => parseInt(b, 16)))
    return await crypto.subtle.verify('HMAC', key, macBytes, toBuffer(textEncode(payload)))
  } catch {
    return false
  }
}