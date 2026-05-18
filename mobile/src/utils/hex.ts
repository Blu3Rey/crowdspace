/**
 * Hex Utilities
 *
 * munim-bluetooth transmits all characteristic values as lowercase hex strings.
 * These helpers convert between JS strings / Uint8Arrays and hex, and handle
 * Base64 conversion used inside wire frames for compact payload encoding.
 */

// ─── Hex ──────────────────────────────────────────────────────────────────────

/** Encode a UTF-8 string → hex string (lowercase). */
export function strToHex(str: string): string {
  let result = ''
  for (let i = 0; i < str.length; i++) {
    const code = str.charCodeAt(i)
    // Handle multi-byte Unicode by encoding the UTF-8 bytes.
    if (code < 0x80) {
      result += code.toString(16).padStart(2, '0')
    } else {
      // TextEncoder is available in React Native's Hermes / JSC engines.
      return uint8ArrayToHex(textEncode(str))
    }
  }
  return result
}

/** Decode a hex string → UTF-8 string. */
export function hexToStr(hex: string): string {
  const bytes = hexToUint8Array(hex)
  return textDecode(bytes)
}

/** Encode a Uint8Array → hex string. */
export function uint8ArrayToHex(bytes: Uint8Array): string {
  let result = ''
  for (let i = 0; i < bytes.length; i++) {
    result += bytes[i]!.toString(16).padStart(2, '0')
  }
  return result
}

/** Decode a hex string → Uint8Array. */
export function hexToUint8Array(hex: string): Uint8Array {
  if (hex.length % 2 !== 0) hex = '0' + hex
  const bytes = new Uint8Array(hex.length / 2)
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16)
  }
  return bytes
}

// ─── Base64 ───────────────────────────────────────────────────────────────────

const B64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** Encode a Uint8Array → Base64 string. */
export function uint8ArrayToBase64(bytes: Uint8Array): string {
  let result = ''
  const len = bytes.length
  for (let i = 0; i < len; i += 3) {
    const a = bytes[i]!
    const b = i + 1 < len ? bytes[i + 1]! : 0
    const c = i + 2 < len ? bytes[i + 2]! : 0
    result += B64_CHARS[a >> 2]
    result += B64_CHARS[((a & 0x03) << 4) | (b >> 4)]
    result += i + 1 < len ? B64_CHARS[((b & 0x0f) << 2) | (c >> 6)] : '='
    result += i + 2 < len ? B64_CHARS[c & 0x3f] : '='
  }
  return result
}

/** Decode a Base64 string → Uint8Array. */
export function base64ToUint8Array(b64: string): Uint8Array {
  const clean = b64.replace(/[^A-Za-z0-9+/]/g, '')
  const len = clean.length
  const bytes = new Uint8Array(Math.floor((len * 3) / 4))
  let byteIdx = 0
  for (let i = 0; i < len; i += 4) {
    const a = B64_CHARS.indexOf(clean[i]!)
    const b = B64_CHARS.indexOf(clean[i + 1]!)
    const c = B64_CHARS.indexOf(clean[i + 2] ?? 'A')
    const d = B64_CHARS.indexOf(clean[i + 3] ?? 'A')
    bytes[byteIdx++] = (a << 2) | (b >> 4)
    if (clean[i + 2] && clean[i + 2] !== '=') bytes[byteIdx++] = ((b & 0xf) << 4) | (c >> 2)
    if (clean[i + 3] && clean[i + 3] !== '=') bytes[byteIdx++] = ((c & 0x3) << 6) | d
  }
  return bytes.slice(0, byteIdx)
}

/** UTF-8 string → Base64. */
export function strToBase64(str: string): string {
  return uint8ArrayToBase64(textEncode(str))
}

/** Base64 → UTF-8 string. */
export function base64ToStr(b64: string): string {
  return textDecode(base64ToUint8Array(b64))
}

// ─── TextEncoder / TextDecoder shims ─────────────────────────────────────────

/**
 * Encode a JS string to UTF-8 bytes.
 * Uses the global TextEncoder when available (Hermes / JSC both provide it),
 * falling back to a manual implementation for environments that don't.
 */
export function textEncode(str: string): Uint8Array {
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(str)
  }
  // Manual UTF-8 encoding
  const bytes: number[] = []
  for (let i = 0; i < str.length; i++) {
    let code = str.charCodeAt(i)
    if (code >= 0xd800 && code <= 0xdbff) {
      const high = code
      const low = str.charCodeAt(++i)
      code = ((high - 0xd800) << 10) + (low - 0xdc00) + 0x10000
    }
    if (code < 0x80) {
      bytes.push(code)
    } else if (code < 0x800) {
      bytes.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f))
    } else if (code < 0x10000) {
      bytes.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f))
    } else {
      bytes.push(
        0xf0 | (code >> 18),
        0x80 | ((code >> 12) & 0x3f),
        0x80 | ((code >> 6) & 0x3f),
        0x80 | (code & 0x3f),
      )
    }
  }
  return new Uint8Array(bytes)
}

/**
 * Decode UTF-8 bytes to a JS string.
 */
export function textDecode(bytes: Uint8Array): string {
  if (typeof TextDecoder !== 'undefined') {
    return new TextDecoder('utf-8').decode(bytes)
  }
  // Manual UTF-8 decoding
  let str = ''
  let i = 0
  while (i < bytes.length) {
    const byte = bytes[i]!
    let code: number
    if ((byte & 0x80) === 0) {
      code = byte
      i++
    } else if ((byte & 0xe0) === 0xc0) {
      code = ((byte & 0x1f) << 6) | (bytes[i + 1]! & 0x3f)
      i += 2
    } else if ((byte & 0xf0) === 0xe0) {
      code = ((byte & 0x0f) << 12) | ((bytes[i + 1]! & 0x3f) << 6) | (bytes[i + 2]! & 0x3f)
      i += 3
    } else {
      code =
        ((byte & 0x07) << 18) |
        ((bytes[i + 1]! & 0x3f) << 12) |
        ((bytes[i + 2]! & 0x3f) << 6) |
        (bytes[i + 3]! & 0x3f)
      i += 4
    }
    if (code >= 0x10000) {
      code -= 0x10000
      str += String.fromCharCode(0xd800 + (code >> 10), 0xdc00 + (code & 0x3ff))
    } else {
      str += String.fromCharCode(code)
    }
  }
  return str
}