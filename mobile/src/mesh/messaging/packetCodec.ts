/**
 * @file packetCodec.ts
 * Two responsibilities in one file (they are tightly coupled):
 *
 * 1. PacketBuilder — construct outbound MeshPackets from application data.
 * 2. PacketHandler — decode inbound raw bytes into MeshMessages.
 * 3. CausalMessageStore — maintain the per-thread causal DAG for message ordering.
 *
 * Design choice 5 (Distributed Consistency / Causal Ordering):
 *   Every message carries `parentIds`: the set of message IDs the sender had
 *   already received when they composed this message. This forms a DAG. When
 *   two partitioned branches re-merge, the DAG can be topologically sorted to
 *   produce a consistent, causally-correct thread view — without relying on
 *   wall-clock time and without a central sequencer.
 */

import {
    DEFAULT_TTL,
    MAX_CONTENT_BYTES,
    PROTOCOL_VERSION,
} from '../core/constants'
import {
    buildAckPayload,
    decryptPayload,
    encryptPayload,
    generatePacketId,
    parseAckPayload
} from '../core/crypto'
import {
    bytesToHex,
    hexToBytes,
    packHandshakePayload,
    packInnerPlaintext,
    packPacket,
    unpackHandshakePayload,
    unpackInnerPlaintext,
    unpackPacket,
} from '../core/encoding'
import {
    CausalNode,
    ContentType,
    MeshMessage,
    MeshPacket,
    PacketType
} from '../core/types'
import { PeerRegistry } from '../routing/peerRegistry'

// ─── PacketBuilder ────────────────────────────────────────────────────────────

export interface BuildDataPacketOptions {
  recipientId:   string         // Contact ID (stable fingerprint)
  contentType:   ContentType
  content:       Uint8Array
  parentIds?:    string[]       // Causal parent message IDs
  ttl?:          number
  peerRegistry:  PeerRegistry
}

export interface BuildResult {
  packet: MeshPacket
  raw:    Uint8Array
}

/**
 * Build an outbound encrypted DATA packet for a known contact.
 * Throws if the recipient is not found in peerRegistry.
 */
export function buildDataPacket(opts: BuildDataPacketOptions): BuildResult {
  const { recipientId, contentType, content, parentIds = [], peerRegistry } = opts

  if (content.length > MAX_CONTENT_BYTES) {
    throw new RangeError(`Content too large: ${content.length} bytes (max ${MAX_CONTENT_BYTES})`)
  }

  const recipient = peerRegistry.getContact(recipientId)
  if (!recipient) throw new Error(`Unknown recipient: ${recipientId}`)

  const targetToken = peerRegistry.getTokenForContact(recipientId)
  if (!targetToken) throw new Error(`No token for recipient: ${recipientId}`)

  const ownToken     = peerRegistry.getOwnToken()
  const packetId     = generatePacketId()

  // Encrypt the inner plaintext
  const plaintext = packInnerPlaintext(contentType, parentIds, content)
  const payload   = encryptPayload(
    plaintext,
    recipient.identityPublicKey,
    peerRegistry.getOwnSecretKey(),
  )

  const packet: MeshPacket = {
    header: {
      version:         PROTOCOL_VERSION,
      packetId,
      targetTokenHash: bytesToHex(targetToken.tokenHash),
      senderTokenHash: bytesToHex(ownToken.tokenHash),
      ttl:             opts.ttl ?? DEFAULT_TTL,
      hopCount:        0,
      type:            PacketType.DATA,
      timestampSec:    Math.floor(Date.now() / 1_000),
    },
    payload,
  }

  return { packet, raw: packPacket(packet) }
}

/**
 * Build a HANDSHAKE packet to send to a newly discovered peer.
 * Includes our identity public key + current token hash + fingerprint.
 * The receiver can use this to initiate a shared-secret derivation.
 */
export function buildHandshakePacket(peerRegistry: PeerRegistry): BuildResult {
  const ownToken     = peerRegistry.getOwnToken()
  const fingerprint  = hexToBytes(peerRegistry.getOwnFingerprint())
  const packetId     = generatePacketId()

  const payload = packHandshakePayload(
    peerRegistry.getOwnPublicKey(),
    ownToken.tokenHash,
    fingerprint,
  )

  const packet: MeshPacket = {
    header: {
      version:         PROTOCOL_VERSION,
      packetId,
      targetTokenHash: '0000000000000000',  // Broadcast / unknown target
      senderTokenHash: bytesToHex(ownToken.tokenHash),
      ttl:             0,                    // HANDSHAKE: never relay
      hopCount:        0,
      type:            PacketType.HANDSHAKE,
      timestampSec:    Math.floor(Date.now() / 1_000),
    },
    payload,
  }

  return { packet, raw: packPacket(packet) }
}

/**
 * Build an ACK packet acknowledging receipt of a DATA packet.
 */
export function buildAckPacket(
  acknowledgedPacketId: string,
  peerRegistry:         PeerRegistry,
  recipientTokenHash?:  string,
): BuildResult {
  const ownToken = peerRegistry.getOwnToken()
  const packetId = generatePacketId()

  const packet: MeshPacket = {
    header: {
      version:         PROTOCOL_VERSION,
      packetId,
      targetTokenHash: recipientTokenHash ?? '0000000000000000',
      senderTokenHash: bytesToHex(ownToken.tokenHash),
      ttl:             0,  // ACKs are not relayed
      hopCount:        0,
      type:            PacketType.ACK,
      timestampSec:    Math.floor(Date.now() / 1_000),
    },
    payload: buildAckPayload(acknowledgedPacketId),
  }

  return { packet, raw: packPacket(packet) }
}

// ─── PacketHandler ────────────────────────────────────────────────────────────

export interface HandleResult {
  type:       'message' | 'handshake' | 'ack' | 'relay' | 'drop'
  message?:   MeshMessage
  /** For 'handshake': the decoded remote public key, so MeshEngine can add the contact */
  remotePublicKey?:    Uint8Array
  remoteTokenHash?:   string
  remoteFingerprint?: string
  /** For 'ack': the packetId being acknowledged */
  acknowledgedPacketId?: string
  /** The full MeshPacket (useful for relay) */
  packet?:    MeshPacket
}

/**
 * Process a raw inbound byte array from any transport.
 *
 * @param raw           Raw bytes from BLE write or Multipeer message
 * @param fromPeerId    Transport-level peer identifier
 * @param peerRegistry  Access to contacts and own keys
 * @param ownTokenHash  Our current rotating token hash (for recipient detection)
 */
export function handleInboundRaw(
  raw:          Uint8Array,
  fromPeerId:   string,
  peerRegistry: PeerRegistry,
  ownTokenHash: Uint8Array,
  receivedRssi?: number,
): HandleResult {
  let packet: MeshPacket
  try {
    packet = unpackPacket(raw)
  } catch (err) {
    return { type: 'drop' }
  }

  const { header, payload } = packet
  const targetHash = header.targetTokenHash

  switch (header.type) {
    // ── BEACON ───────────────────────────────────────────────────────────────
    case PacketType.BEACON:
      return { type: 'drop', packet }  // Handled at radio layer only

    // ── ACK ──────────────────────────────────────────────────────────────────
    case PacketType.ACK: {
      try {
        const acknowledgedPacketId = parseAckPayload(payload)
        return { type: 'ack', acknowledgedPacketId, packet }
      } catch {
        return { type: 'drop' }
      }
    }

    // ── HANDSHAKE ─────────────────────────────────────────────────────────────
    case PacketType.HANDSHAKE: {
      try {
        const { identityPublicKey, tokenHash, pubkeyFingerprint } = unpackHandshakePayload(payload)
        return {
          type:               'handshake',
          remotePublicKey:    identityPublicKey,
          remoteTokenHash:    bytesToHex(tokenHash),
          remoteFingerprint:  bytesToHex(pubkeyFingerprint),
          packet,
        }
      } catch {
        return { type: 'drop' }
      }
    }

    // ── DATA ──────────────────────────────────────────────────────────────────
    case PacketType.DATA: {
      // Check if we are the intended recipient
      const ownHashHex = bytesToHex(ownTokenHash)
      const isForUs    = targetHash === ownHashHex

      if (isForUs) {
        const message = _decryptDataPacket(packet, peerRegistry, receivedRssi)
        if (!message) return { type: 'drop' }
        return { type: 'message', message, packet }
      }

      // Not for us — relay if TTL allows
      if (header.ttl <= 0) return { type: 'drop', packet }
      return { type: 'relay', packet }
    }

    // ── RELAY ─────────────────────────────────────────────────────────────────
    case PacketType.RELAY: {
      // Same logic: check if we are the target embedded in the inner packet
      const ownHashHex = bytesToHex(ownTokenHash)
      if (header.targetTokenHash === ownHashHex) {
        const message = _decryptDataPacket(packet, peerRegistry, receivedRssi)
        if (!message) return { type: 'drop' }
        return { type: 'message', message, packet }
      }
      if (header.ttl <= 0) return { type: 'drop', packet }
      return { type: 'relay', packet }
    }

    default:
      return { type: 'drop' }
  }
}

/** Attempt to decrypt a DATA/RELAY packet. Returns null on decryption failure. */
function _decryptDataPacket(
  packet:       MeshPacket,
  peerRegistry: PeerRegistry,
  rssi?:        number,
): MeshMessage | null {
  const { header, payload } = packet

  // Try each known contact as the sender until decryption succeeds
  for (const contact of peerRegistry.getAllContacts()) {
    const plaintext = decryptPayload(
      payload,
      contact.identityPublicKey,
      peerRegistry.getOwnSecretKey(),
    )
    if (!plaintext) continue

    try {
      const { contentType, parentIds, content } = unpackInnerPlaintext(plaintext)
      const message: MeshMessage = {
        id:           header.packetId,
        senderId:     contact.id,
        recipientId:  peerRegistry.getOwnFingerprint(),
        contentType,
        content,
        parentIds,
        timestampMs:  header.timestampSec * 1_000,
        receivedAtMs: Date.now(),
        hopCount:     header.hopCount,
        rssi,
      }
      return message
    } catch {
      continue
    }
  }

  return null  // Could not decrypt with any known contact key
}

// ─── Causal Message Store (DAG) ───────────────────────────────────────────────

/**
 * Per-conversation causal DAG.
 *
 * Design choice 5: messages form a DAG where each node references its causal
 * predecessors. On merge (when a partition heals), we topologically sort the
 * combined DAG. This produces a display order that respects causality while
 * being stable and deterministic — regardless of wall-clock differences.
 *
 * One CausalMessageStore should exist per conversation (direct or group).
 */
export class CausalMessageStore {
  private _nodes = new Map<string, CausalNode>()

  insert(message: MeshMessage): void {
    if (this._nodes.has(message.id)) return  // Idempotent

    const node: CausalNode = {
      message,
      parentIds: message.parentIds,
      childIds:  [],
    }
    this._nodes.set(message.id, node)

    // Link parents → this node
    for (const parentId of message.parentIds) {
      const parent = this._nodes.get(parentId)
      if (parent && !parent.childIds.includes(message.id)) {
        parent.childIds.push(message.id)
      }
    }
  }

  /**
   * Return all messages in causal order (topological sort, Kahn's algorithm).
   * Messages with no common causal ancestor are sorted by timestampMs as
   * a stable tiebreaker. This matches how Git resolves merge commits.
   */
  getSorted(): MeshMessage[] {
    const inDegree = new Map<string, number>()
    for (const [id, node] of this._nodes) {
      if (!inDegree.has(id)) inDegree.set(id, 0)
      for (const childId of node.childIds) {
        inDegree.set(childId, (inDegree.get(childId) ?? 0) + 1)
      }
    }

    // Start with nodes that have no predecessors in our store
    const queue: CausalNode[] = []
    for (const [id, degree] of inDegree) {
      if (degree === 0) queue.push(this._nodes.get(id)!)
    }
    // Stable tiebreaker: sort by origin timestamp
    queue.sort((a, b) => a.message.timestampMs - b.message.timestampMs)

    const result: MeshMessage[] = []
    const visited = new Set<string>()

    while (queue.length > 0) {
      // Pop from front (BFS order = causal order)
      const node = queue.shift()!
      if (visited.has(node.message.id)) continue
      visited.add(node.message.id)
      result.push(node.message)

      const children = node.childIds
        .map(id => this._nodes.get(id))
        .filter(Boolean) as CausalNode[]

      children.sort((a, b) => a.message.timestampMs - b.message.timestampMs)
      for (const child of children) {
        const newDegree = (inDegree.get(child.message.id) ?? 1) - 1
        inDegree.set(child.message.id, newDegree)
        if (newDegree === 0) queue.push(child)
      }
    }

    return result
  }

  /** IDs of the most recent "frontier" messages — used as parentIds for the next send */
  getFrontierIds(): string[] {
    const childSet = new Set<string>()
    for (const node of this._nodes.values()) {
      for (const id of node.childIds) childSet.add(id)
    }
    // Frontier = nodes that are not anyone's child
    return Array.from(this._nodes.keys()).filter(id => !childSet.has(id))
  }

  has(id: string): boolean { return this._nodes.has(id) }
  size(): number { return this._nodes.size }
  clear(): void { this._nodes.clear() }
}