/**
 * UUID Utilities
 *
 * UUID v4 generation without a native dependency — uses crypto.getRandomValues
 * when available (Hermes / JSC both expose it via the global scope).
 */

/** Generate a UUID v4 string. */
export function generateUUID(): string {
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    const bytes = new Uint8Array(16)
    crypto.getRandomValues(bytes)
    // Set version 4 and variant bits
    bytes[6] = (bytes[6]! & 0x0f) | 0x40
    bytes[8] = (bytes[8]! & 0x3f) | 0x80
    return formatUUID(bytes)
  }
  // Fallback: Math.random (not cryptographically secure, but acceptable for
  // peer IDs when crypto is unavailable — should not happen on RN with Hermes)
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

function formatUUID(bytes: Uint8Array): string {
  const hex = Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join('-')
}

/**
 * Generate a short collision-resistant ID (first 8 hex chars of a UUID v4).
 * Used to identify fragments of the same logical message without transmitting
 * the full 36-character UUID in every frame.
 */
export function shortId(): string {
  return generateUUID().replace(/-/g, '').slice(0, 8)
}

/** Validate that a string looks like a UUID v4. */
export function isValidUUID(str: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(str)
}