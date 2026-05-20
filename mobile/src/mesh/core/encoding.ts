/**
 * @file encoding.ts
 * Compact binary serialisation / deserialisation for all wire structures.
 *
 * Design choice 4 (Binary Serialisation & MTU Budgeting):
 * We never send JSON or string-keyed objects over BLE. Every structure is
 * packed into a flat Uint8Array. This halves packet sizes compared to JSON
 * and eliminates key-name overhead entirely.
 *
 * All multi-byte integers are big-endian (network byte order).
 */

import {
    CHUNK_HEADER_SIZE,
    CHUNK_PAYLOAD_SIZE,
    ROUTING_HEADER_SIZE
} from './constants'
import { ChunkHeader, ContentType, MeshPacket, PacketType, PendingStream, RoutingHeader } from './types'

// ─── Utility ─────────────────────────────────────────────────────────────────

/** Convert a hex string to a Uint8Array */
export function hexToBytes(hex: string): Uint8Array {
  if (hex.length % 2 !== 0) throw new RangeError(`Odd-length hex string: ${hex}`)
  const out = new Uint8Array(hex.length / 2)
  for (let i = 0; i < out.length; i++) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16)
  }
  return out
}

/** Convert a Uint8Array to a lowercase hex string */
export function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('')
}

/** Concatenate multiple Uint8Arrays into one */
export function concatBytes(...arrays: Uint8Array[]): Uint8Array {
  const totalLength = arrays.reduce((acc, a) => acc + a.length, 0)
  const result = new Uint8Array(totalLength)
  let offset = 0
  for (const arr of arrays) {
    result.set(arr, offset)
    offset += arr.length
  }
  return result
}

// ─── Routing Header ──────────────────────────────────────────────────────────

/**
 * Pack a RoutingHeader into ROUTING_HEADER_SIZE (36) bytes.
 *
 * Layout:
 *   [0]      version         uint8
 *   [1-12]   packetId        12 raw bytes (hex decoded)
 *   [13-20]  targetTokenHash  8 raw bytes (hex decoded)
 *   [21-28]  senderTokenHash  8 raw bytes (hex decoded)
 *   [29]     ttl             uint8
 *   [30]     hopCount        uint8
 *   [31]     type            uint8 (PacketType)
 *   [32-35]  timestampSec    uint32 big-endian
 */
export function packRoutingHeader(h: RoutingHeader): Uint8Array {
  const buf = new Uint8Array(ROUTING_HEADER_SIZE)
  const view = new DataView(buf.buffer)

  buf[0] = h.version
  buf.set(hexToBytes(h.packetId),        1)   // 12 bytes
  buf.set(hexToBytes(h.targetTokenHash), 13)  //  8 bytes
  buf.set(hexToBytes(h.senderTokenHash), 21)  //  8 bytes
  buf[29] = h.ttl
  buf[30] = h.hopCount
  buf[31] = h.type
  view.setUint32(32, h.timestampSec, false)   // big-endian

  return buf
}

/** Unpack a RoutingHeader from the first ROUTING_HEADER_SIZE bytes of a buffer */
export function unpackRoutingHeader(buf: Uint8Array): RoutingHeader {
  if (buf.length < ROUTING_HEADER_SIZE) {
    throw new RangeError(`Buffer too short for routing header: ${buf.length}`)
  }
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength)

  return {
    version:         buf[0],
    packetId:        bytesToHex(buf.slice(1, 13)),
    targetTokenHash: bytesToHex(buf.slice(13, 21)),
    senderTokenHash: bytesToHex(buf.slice(21, 29)),
    ttl:             buf[29],
    hopCount:        buf[30],
    type:            buf[31] as PacketType,
    timestampSec:    view.getUint32(32, false),
  }
}

// ─── Full Packet ─────────────────────────────────────────────────────────────

/**
 * Serialise a MeshPacket to a flat byte array:
 *   [header: 36 bytes][payload: variable]
 */
export function packPacket(packet: MeshPacket): Uint8Array {
  const header = packRoutingHeader(packet.header)
  return concatBytes(header, packet.payload)
}

/** Deserialise a flat byte array back into a MeshPacket */
export function unpackPacket(raw: Uint8Array): MeshPacket {
  const header  = unpackRoutingHeader(raw)
  const payload = raw.slice(ROUTING_HEADER_SIZE)
  return { header, payload }
}

// ─── Inner Plaintext (post-decryption) ───────────────────────────────────────

/**
 * Pack the inner plaintext that gets encrypted into the payload.
 *
 * Layout:
 *   [0]                contentType      uint8 (ContentType)
 *   [1]                numParents       uint8 (count of parent message IDs)
 *   [2 .. 2+N*12-1]   parentIds        N × 12 bytes each
 *   [2+N*12 ..]       content          remaining bytes
 *
 * Design choice 5 (Causal Ordering): each message explicitly encodes its
 * causal parents, forming a portable DAG that merges cleanly across partitions.
 */
export function packInnerPlaintext(
  contentType: ContentType,
  parentIds:   string[],
  content:     Uint8Array,
): Uint8Array {
  if (parentIds.length > 255) throw new RangeError('Too many parent IDs (max 255)')

  const parentBytes = parentIds.flatMap(id => Array.from(hexToBytes(id.slice(0, 24))))
  const buf = new Uint8Array(2 + parentIds.length * 12 + content.length)

  buf[0] = contentType
  buf[1] = parentIds.length

  let offset = 2
  for (const id of parentIds) {
    buf.set(hexToBytes(id.slice(0, 24)), offset) // 12 bytes per parent ID
    offset += 12
  }
  buf.set(content, offset)

  return buf
}

/** Unpack an inner plaintext buffer (output of decryption) */
export function unpackInnerPlaintext(buf: Uint8Array): {
  contentType: ContentType
  parentIds:   string[]
  content:     Uint8Array
} {
  if (buf.length < 2) throw new RangeError('Inner plaintext too short')

  const contentType = buf[0] as ContentType
  const numParents  = buf[1]

  const parentIds: string[] = []
  let offset = 2
  for (let i = 0; i < numParents; i++) {
    parentIds.push(bytesToHex(buf.slice(offset, offset + 12)))
    offset += 12
  }

  const content = buf.slice(offset)
  return { contentType, parentIds, content }
}

// ─── ANNOUNCE Characteristic ─────────────────────────────────────────────────

/**
 * ANNOUNCE payload (16 bytes):
 *   [0-7]   tokenHash        8 bytes (first 8 bytes of SHA-256(currentToken))
 *   [8-15]  pubkeyFingerprint 8 bytes (first 8 bytes of SHA-256(identityPublicKey))
 *
 * This is the only thing a scanning central reads before deciding to connect.
 * It is minimal by design — enough to identify a known contact, nothing more.
 */
export function packAnnounce(tokenHash: Uint8Array, pubkeyFingerprint: Uint8Array): Uint8Array {
  const buf = new Uint8Array(16)
  buf.set(tokenHash.slice(0, 8),        0)
  buf.set(pubkeyFingerprint.slice(0, 8), 8)
  return buf
}

export function unpackAnnounce(buf: Uint8Array): {
  tokenHash:         Uint8Array
  pubkeyFingerprint: Uint8Array
} {
  if (buf.length < 16) throw new RangeError('ANNOUNCE payload too short')
  return {
    tokenHash:         buf.slice(0, 8),
    pubkeyFingerprint: buf.slice(8, 16),
  }
}

// ─── HANDSHAKE Payload ────────────────────────────────────────────────────────

/**
 * HANDSHAKE payload (64 bytes):
 *   [0-31]  identityPublicKey   32 bytes (X25519)
 *   [32-39] tokenHash           8 bytes
 *   [40-47] pubkeyFingerprint   8 bytes
 *   [48-63] reserved / padding  16 bytes (zero, reserved for signature in v2)
 *
 * Design choice 1 (Cryptographic Catch): since there is no central server to
 * store PreKeys, first contact is performed either via QR code (out-of-band)
 * or via this direct GATT HANDSHAKE. The shared secret is derived from
 * X25519 DH between the two identity key pairs.
 */
export function packHandshakePayload(
  identityPublicKey:  Uint8Array,
  tokenHash:          Uint8Array,
  pubkeyFingerprint:  Uint8Array,
): Uint8Array {
  const buf = new Uint8Array(64)
  buf.set(identityPublicKey.slice(0, 32), 0)
  buf.set(tokenHash.slice(0, 8),         32)
  buf.set(pubkeyFingerprint.slice(0, 8), 40)
  // [48-63] zeroed (reserved)
  return buf
}

export function unpackHandshakePayload(buf: Uint8Array): {
  identityPublicKey:  Uint8Array
  tokenHash:          Uint8Array
  pubkeyFingerprint:  Uint8Array
} {
  if (buf.length < 64) throw new RangeError('HANDSHAKE payload too short')
  return {
    identityPublicKey:  buf.slice(0, 32),
    tokenHash:          buf.slice(32, 40),
    pubkeyFingerprint:  buf.slice(40, 48),
  }
}

// ─── Chunking ─────────────────────────────────────────────────────────────────

/**
 * Split a flat byte array into BLE-safe chunks (each ≤ CHUNK_PAYLOAD_SIZE bytes).
 * Each chunk gets a 3-byte header prepended:
 *   [0] streamId    — caller-supplied; identifies this transfer to the peer
 *   [1] seqNum      — 0-indexed chunk position
 *   [2] totalChunks — total chunks in this stream (max 255)
 *
 * Design choice 4: we never exceed one MTU worth of data per write, ensuring
 * compatibility with older devices that default to 23-byte MTU by padding
 * chunk sizes to the negotiated limit.
 */
export function chunkBytes(data: Uint8Array, streamId: number): Uint8Array[] {
  const totalChunks = Math.ceil(data.length / CHUNK_PAYLOAD_SIZE)
  if (totalChunks > 255) throw new RangeError(`Packet too large to chunk: ${data.length} bytes`)

  const chunks: Uint8Array[] = []
  for (let i = 0; i < totalChunks; i++) {
    const chunkData = data.slice(i * CHUNK_PAYLOAD_SIZE, (i + 1) * CHUNK_PAYLOAD_SIZE)
    const frame     = new Uint8Array(CHUNK_HEADER_SIZE + chunkData.length)
    frame[0] = streamId & 0xFF
    frame[1] = i
    frame[2] = totalChunks
    frame.set(chunkData, CHUNK_HEADER_SIZE)
    chunks.push(frame)
  }
  return chunks
}

/** Parse the chunk header from a raw BLE write payload */
export function parseChunkHeader(frame: Uint8Array): { header: ChunkHeader; data: Uint8Array } {
  if (frame.length < CHUNK_HEADER_SIZE) throw new RangeError('Frame too short for chunk header')
  return {
    header: {
      streamId:    frame[0],
      seqNum:      frame[1],
      totalChunks: frame[2],
    },
    data: frame.slice(CHUNK_HEADER_SIZE),
  }
}

/**
 * Feed a chunk into a PendingStream accumulator.
 * Returns the fully assembled buffer when all chunks have arrived, or null
 * if assembly is still in progress.
 */
export function feedChunk(
  streams: Map<string, PendingStream>,
  peerId:  string,
  frame:   Uint8Array,
): Uint8Array | null {
  const { header, data } = parseChunkHeader(frame)
  const key = `${peerId}:${header.streamId}`

  let stream = streams.get(key)
  if (!stream) {
    stream = {
      streamId:    header.streamId,
      totalChunks: header.totalChunks,
      chunks:      new Map(),
      firstSeenMs: Date.now(),
    }
    streams.set(key, stream)
  }

  stream.chunks.set(header.seqNum, data)

  if (stream.chunks.size === stream.totalChunks) {
    // Reassemble in order
    const assembled = new Uint8Array(
      Array.from(stream.chunks.values()).reduce((acc, c) => acc + c.length, 0)
    )
    let offset = 0
    for (let i = 0; i < stream.totalChunks; i++) {
      const chunk = stream.chunks.get(i)
      if (!chunk) return null  // Should never happen if totalChunks is correct
      assembled.set(chunk, offset)
      offset += chunk.length
    }
    streams.delete(key)
    return assembled
  }

  return null
}

/** Evict stale streams older than timeoutMs from the assembly map */
export function evictStaleStreams(
  streams:   Map<string, PendingStream>,
  timeoutMs: number,
): void {
  const now = Date.now()
  for (const [key, stream] of streams) {
    if (now - stream.firstSeenMs > timeoutMs) streams.delete(key)
  }
}