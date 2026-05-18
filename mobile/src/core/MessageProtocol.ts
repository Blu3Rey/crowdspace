/**
 * MessageProtocol
 *
 * Stateless encode / decode / fragment / reassemble helpers for the BLE wire
 * protocol. Every physical write is a hex-encoded WireFrame. Logical messages
 * larger than MAX_CHUNK_BYTES are split across multiple frames; the receiver
 * accumulates them until all parts arrive (or a timeout fires).
 *
 * Wire frame JSON structure (compact keys to minimise bytes on air):
 *   { v, id, p, n, f?, t?, k?, ts?, d }
 *
 * The assembled payload in field `d` is a Base64-encoded JSON string whose
 * shape is specific to each MessageKind (e.g. `{ text: "hello" }` for DM).
 */

import {
  FRAGMENT_REASSEMBLY_TIMEOUT_MS,
  MAX_CHUNK_BYTES,
  MAX_FRAGMENTS,
  PROTOCOL_VERSION
} from '../constants/ble'
import type {
  MeshMessage,
  MessageKind,
  PeerInfoPayload,
  WireFrame
} from '../types/ble'
import {
  base64ToStr,
  hexToStr,
  strToBase64,
  strToHex,
  textEncode
} from '../utils/hex'
import { generateUUID, shortId } from '../utils/uuid'

// ─── Pending Fragment Store ───────────────────────────────────────────────────

interface PendingAssembly {
  frames: Map<number, string>   // partIndex → base64 chunk
  totalParts: number
  firstFrame: Omit<WireFrame, 'd'>
  receivedAt: number
  timerId: ReturnType<typeof setTimeout>
}

type OnAssembled = (msg: MeshMessage, transport: 'ble-gatt' | 'multipeer') => void

// ─── MessageProtocol ──────────────────────────────────────────────────────────

export class MessageProtocol {
  private pending = new Map<string, PendingAssembly>()

  // ── Encoding ──────────────────────────────────────────────────────────────

  /**
   * Encode a logical MeshMessage into one or more hex strings ready to write
   * to a GATT characteristic or send via Multipeer.
   *
   * Each hex string, when decoded, is a compact WireFrame JSON.
   * Returns an array — single-element when no fragmentation is needed.
   */
  encode(msg: MeshMessage, selfId: string): string[] {
    const payload = messageToPayloadJson(msg)
    const bytes = textEncode(payload)

    // Choose chunk size so each WireFrame JSON encodes within MAX_CHUNK_BYTES
    // after the frame envelope overhead.
    const ENVELOPE_OVERHEAD = 80  // approximate JSON envelope bytes
    const chunkSize = MAX_CHUNK_BYTES - ENVELOPE_OVERHEAD
    const b64 = strToBase64(payload)
    const chunks = splitBase64(b64, Math.floor(chunkSize * 1.333)) // base64 is ~4/3 of bytes

    if (chunks.length > MAX_FRAGMENTS) {
      throw new Error(
        `Message too large: requires ${chunks.length} fragments (max ${MAX_FRAGMENTS})`,
      )
    }

    const msgShortId = shortId()

    return chunks.map((chunk, idx): string => {
      const frame: WireFrame = {
        v: PROTOCOL_VERSION,
        id: msgShortId,
        p: idx,
        n: chunks.length,
        d: chunk,
      }
      // Metadata only travels in the first frame to save bytes in continuations.
      if (idx === 0) {
        frame.f = selfId
        frame.t = 'to' in msg ? msg.to : ('groupId' in msg ? msg.groupId : null)
        frame.k = msg.kind
        frame.ts = msg.timestamp
      }
      return strToHex(JSON.stringify(frame))
    })
  }

  // ── Decoding ──────────────────────────────────────────────────────────────

  /**
   * Receive a raw hex string (from a GATT write or Multipeer message),
   * parse it into a WireFrame, and either return the completed MeshMessage
   * (single frame) or buffer it for reassembly.
   *
   * Returns the assembled message when all fragments have arrived, or null
   * when waiting for more parts.
   */
  receive(
    hexData: string,
    transport: 'ble-gatt' | 'multipeer',
    onAssembled: OnAssembled,
  ): void {
    let frame: WireFrame
    try {
      frame = JSON.parse(hexToStr(hexData)) as WireFrame
    } catch {
      console.warn('[MessageProtocol] Failed to parse wire frame', hexData.slice(0, 40))
      return
    }

    if (frame.v !== PROTOCOL_VERSION) {
      console.warn('[MessageProtocol] Unknown protocol version', frame.v)
      return
    }

    if (frame.n === 1) {
      // Single, non-fragmented message.
      const msg = assembleMessage(frame, frame.d, transport)
      if (msg) onAssembled(msg, transport)
      return
    }

    // Fragment — buffer until all parts arrive.
    // Note: split into if/else so TypeScript can unambiguously narrow `assembly`
    // to PendingAssembly before the .frames.set() call below. Using `let` +
    // `if (!assembly) { early-return OR assign }` confuses control-flow analysis
    // when there is a nested conditional return inside the falsy branch.
    const existing = this.pending.get(frame.id)
    let assembly: PendingAssembly

    if (existing !== undefined) {
      assembly = existing
    } else {
      if (frame.p !== 0) {
        // We missed the first fragment; discard this orphan.
        console.warn('[MessageProtocol] Orphan fragment (missed first), id=', frame.id)
        return
      }
      const timerId = setTimeout(() => {
        console.warn('[MessageProtocol] Fragment timeout for id=', frame.id)
        this.pending.delete(frame.id)
      }, FRAGMENT_REASSEMBLY_TIMEOUT_MS)

      assembly = {
        frames: new Map(),
        totalParts: frame.n,
        firstFrame: { ...frame, d: '' },
        receivedAt: Date.now(),
        timerId,
      }
      this.pending.set(frame.id, assembly)
    }

    assembly.frames.set(frame.p, frame.d)

    if (assembly.frames.size === assembly.totalParts) {
      clearTimeout(assembly.timerId)
      this.pending.delete(frame.id)

      // Reassemble in order.
      const orderedChunks: string[] = []
      for (let i = 0; i < assembly.totalParts; i++) {
        const chunk = assembly.frames.get(i)
        if (!chunk) {
          console.error('[MessageProtocol] Missing fragment', i, 'for id=', frame.id)
          return
        }
        orderedChunks.push(chunk)
      }

      const fullB64 = orderedChunks.join('')
      const msg = assembleMessage(assembly.firstFrame, fullB64, transport)
      if (msg) onAssembled(msg, transport)
    }
  }

  /** Clear all pending reassembly state (e.g. on engine stop). */
  flush(): void {
    for (const { timerId } of this.pending.values()) {
      clearTimeout(timerId)
    }
    this.pending.clear()
  }

  // ── Peer Info ─────────────────────────────────────────────────────────────

  /**
   * Encode a PeerInfoPayload into a hex string suitable for storing in
   * the PEER_INFO_CHAR_UUID characteristic.
   */
  static encodePeerInfo(info: PeerInfoPayload): string {
    return strToHex(JSON.stringify(info))
  }

  /**
   * Decode a PeerInfoPayload from a hex characteristic value.
   * Returns null if the hex is malformed.
   */
  static decodePeerInfo(hex: string): PeerInfoPayload | null {
    try {
      const obj = JSON.parse(hexToStr(hex)) as PeerInfoPayload
      if (!obj.id || !obj.name) return null
      return obj
    } catch {
      return null
    }
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Convert a MeshMessage to its inner payload JSON string. */
function messageToPayloadJson(msg: MeshMessage): string {
  // The WireFrame envelope already carries from / to / kind / timestamp,
  // so the inner payload only needs the message-specific fields.
  switch (msg.kind) {
    case 'dm':
      return JSON.stringify({ text: msg.text, raw: msg.rawPayload })
    case 'dm_ack':
      return JSON.stringify({ acked: msg.ackedMsgId })
    case 'group':
      return JSON.stringify({ text: msg.text, raw: msg.rawPayload })
    case 'group_ack':
      return JSON.stringify({ acked: (msg as any).ackedMsgId })
    case 'group_invite':
      return JSON.stringify({
        gid: msg.groupId,
        gname: msg.groupName,
        by: msg.invitedBy,
        members: msg.members,
      })
    case 'group_meta':
      return JSON.stringify({ gid: msg.groupId, name: msg.name, members: msg.members })
    case 'presence':
      return JSON.stringify({ status: msg.status, name: msg.displayName })
    case 'ping':
    case 'pong':
      return JSON.stringify({ nonce: msg.nonce })
    case 'locate_req':
      return JSON.stringify({ nonce: msg.nonce })
    case 'locate_res':
      return JSON.stringify({ nonce: msg.nonce, rssi: msg.peerRssi })
    default:
      return JSON.stringify({})
  }
}

/** Reconstruct a MeshMessage from a first-frame envelope + reassembled Base64. */
function assembleMessage(
  firstFrame: Omit<WireFrame, 'd'>,
  fullB64: string,
  transport: 'ble-gatt' | 'multipeer',
): MeshMessage | null {
  try {
    const payloadStr = base64ToStr(fullB64)
    const payload = JSON.parse(payloadStr)

    const base = {
      msgId: generateUUID(), // local ID; real ID is firstFrame.id (short)
      from: firstFrame.f ?? 'unknown',
      timestamp: firstFrame.ts ?? Date.now(),
      kind: firstFrame.k ?? 'dm',
      transport,
    }

    switch (firstFrame.k as MessageKind) {
      case 'dm':
        return { ...base, kind: 'dm', to: firstFrame.t as string, text: payload.text, rawPayload: payload.raw }
      case 'dm_ack':
        return { ...base, kind: 'dm_ack', ackedMsgId: payload.acked }
      case 'group':
        return { ...base, kind: 'group', groupId: firstFrame.t as string, text: payload.text, rawPayload: payload.raw }
      case 'group_ack':
        return { ...base, kind: 'group_ack', ackedMsgId: payload.acked } as any
      case 'group_invite':
        return {
          ...base,
          kind: 'group_invite',
          groupId: payload.gid,
          groupName: payload.gname,
          invitedBy: payload.by,
          members: payload.members,
        }
      case 'group_meta':
        return { ...base, kind: 'group_meta', groupId: payload.gid, name: payload.name, members: payload.members }
      case 'presence':
        return { ...base, kind: 'presence', status: payload.status, displayName: payload.name }
      case 'ping':
        return { ...base, kind: 'ping', nonce: payload.nonce }
      case 'pong':
        return { ...base, kind: 'pong', nonce: payload.nonce }
      case 'locate_req':
        return { ...base, kind: 'locate_req', nonce: payload.nonce }
      case 'locate_res':
        return { ...base, kind: 'locate_res', nonce: payload.nonce, peerRssi: payload.rssi }
      default:
        console.warn('[MessageProtocol] Unknown kind:', firstFrame.k)
        return null
    }
  } catch (err) {
    console.error('[MessageProtocol] Failed to assemble message:', err)
    return null
  }
}

/** Split a Base64 string into chunks of at most `chunkLen` characters. */
function splitBase64(b64: string, chunkLen: number): string[] {
  const chunks: string[] = []
  for (let i = 0; i < b64.length; i += chunkLen) {
    chunks.push(b64.slice(i, i + chunkLen))
  }
  return chunks.length === 0 ? [''] : chunks
}