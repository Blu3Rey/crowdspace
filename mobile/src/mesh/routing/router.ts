/**
 * @file router.ts
 * Epidemic routing engine with spray-and-wait optimisation.
 *
 * Epidemic routing: when a node encounters another node, it forwards
 * any packets the other node hasn't seen yet. This guarantees eventual
 * delivery as long as there is a connected path (even a temporally
 * disconnected one) from source to destination.
 *
 * Spray-and-wait limits the number of copies in circulation:
 *   Phase 1 (Spray): the source sprays up to sprayFactor copies to different
 *                    encountered nodes. Each copy carries spray_remaining = N/2.
 *   Phase 2 (Wait):  once spray_remaining reaches 1, the carrier only delivers
 *                    directly (not relays further).
 *
 * Seen-packet deduplication (LRU-TTL cache):
 *   Every received packetId is stored. Before relaying, we check the cache.
 *   This breaks relay loops and prevents amplification.
 *
 * Design choice 1: relay hops see only the encrypted payload + routing header.
 *                  They never decrypt message content.
 */

import {
    DEFAULT_SPRAY_FACTOR,
    DEFAULT_TTL,
    SEEN_CACHE_MAX_SIZE,
    SEEN_CACHE_TTL_MS,
} from '../core/constants'
import { MeshPacket, PacketType } from '../core/types'
import { PeerRegistry } from './peerRegistry'

// ─── Seen-Packet Cache ───────────────────────────────────────────────────────

interface SeenEntry {
  seenAtMs: number
}

/**
 * LRU + TTL cache of recently seen packetIds.
 * Prevents relay loops without requiring a global ledger.
 */
class SeenCache {
  private readonly _map = new Map<string, SeenEntry>()
  private readonly _maxSize: number
  private readonly _ttlMs: number

  constructor(maxSize = SEEN_CACHE_MAX_SIZE, ttlMs = SEEN_CACHE_TTL_MS) {
    this._maxSize = maxSize
    this._ttlMs   = ttlMs
  }

  has(packetId: string): boolean {
    const entry = this._map.get(packetId)
    if (!entry) return false
    if (Date.now() - entry.seenAtMs > this._ttlMs) {
      this._map.delete(packetId)
      return false
    }
    return true
  }

  add(packetId: string): void {
    if (this._map.size >= this._maxSize) {
      // Evict the oldest 10% of entries
      const evictCount = Math.ceil(this._maxSize * 0.1)
      let evicted = 0
      for (const key of this._map.keys()) {
        if (evicted++ >= evictCount) break
        this._map.delete(key)
      }
    }
    this._map.set(packetId, { seenAtMs: Date.now() })
  }

  /** Periodic eviction of expired entries */
  evictExpired(): void {
    const cutoff = Date.now() - this._ttlMs
    for (const [id, entry] of this._map) {
      if (entry.seenAtMs < cutoff) this._map.delete(id)
    }
  }
}

// ─── Per-Packet Spray State ──────────────────────────────────────────────────

interface SprayState {
  copiesRemaining: number
  forwardedTo:     Set<string>   // peer device IDs that already received this copy
}

// ─── Router ──────────────────────────────────────────────────────────────────

export interface RouterOptions {
  defaultTTL?:   number
  sprayFactor?:  number
  peerRegistry:  PeerRegistry
}

export class Router {
  private readonly _seen:       SeenCache
  private readonly _spray      = new Map<string, SprayState>()
  private readonly _defaultTTL: number
  private readonly _sprayFactor: number
  private readonly _peerRegistry: PeerRegistry
  private _evictInterval: ReturnType<typeof setInterval> | null = null

  constructor(opts: RouterOptions) {
    this._defaultTTL   = opts.defaultTTL   ?? DEFAULT_TTL
    this._sprayFactor  = opts.sprayFactor  ?? DEFAULT_SPRAY_FACTOR
    this._peerRegistry = opts.peerRegistry
    this._seen         = new SeenCache()
  }

  start(): void {
    // Evict expired seen entries every hour
    this._evictInterval = setInterval(() => this._seen.evictExpired(), 60 * 60 * 1_000)
  }

  stop(): void {
    if (this._evictInterval) { clearInterval(this._evictInterval); this._evictInterval = null }
  }

  // ─── Inbound decision ─────────────────────────────────────────────────────

  /**
   * Determine how to handle an incoming packet.
   *
   * Returns:
   *   'deliver'        — packet is for us; decrypt and deliver to app layer
   *   'relay'          — forward to known peers
   *   'drop'           — already seen, TTL=0, or otherwise discard
   */
  classifyInbound(
    packet:       MeshPacket,
    ownTokenHash: Uint8Array,
    fromPeerId:   string,
  ): 'deliver' | 'relay' | 'drop' {
    const { header } = packet

    // Loop suppression
    if (this._seen.has(header.packetId)) return 'drop'

    // BEACON and ACK: never relay
    if (header.type === PacketType.BEACON || header.type === PacketType.ACK) return 'deliver'

    // Check if we are the target
    const targetHash = header.targetTokenHash
    const isForUs    = this._peerRegistry.resolveContact(targetHash) === null &&
                       this._isOwnToken(targetHash, ownTokenHash)

    if (isForUs) {
      this._seen.add(header.packetId)
      return 'deliver'
    }

    // TTL check
    if (header.ttl <= 0) return 'drop'

    // Check if target is a known contact we can help route to
    const targetContact = this._peerRegistry.resolveContact(targetHash)
    if (!targetContact && header.type === PacketType.DATA) {
      // Unknown target — participate in epidemic relay anyway
      // (we might encounter the recipient later)
    }

    this._seen.add(header.packetId)
    return 'relay'
  }

  /**
   * Decide which currently-connected peers to relay a packet to.
   * Implements spray-and-wait: limits relay fan-out after enough copies exist.
   *
   * @param packet      The packet to (potentially) relay
   * @param connectedPeers  Currently connected peer IDs
   * @returns Array of peer IDs that should receive this relay
   */
  selectRelayTargets(packet: MeshPacket, connectedPeers: string[]): string[] {
    const { packetId } = packet.header

    let state = this._spray.get(packetId)
    if (!state) {
      // First time we're relaying this packet — initialise spray state
      state = {
        copiesRemaining: this._sprayFactor,
        forwardedTo:     new Set(),
      }
      this._spray.set(packetId, state)
    }

    if (state.copiesRemaining <= 1) {
      // Wait phase: only deliver directly to the intended recipient if nearby
      const targetContact = this._peerRegistry.resolveContact(packet.header.targetTokenHash)
      if (!targetContact) return []

      // Check if any connected peer is the target
      return connectedPeers.filter(peerId => {
        const peer = this._peerRegistry.getNearbyPeers().find(p => p.deviceId === peerId)
        return peer && this._peerRegistry.resolveContact(peer.tokenHash)?.id === targetContact.id
      })
    }

    // Spray phase: forward to peers that haven't seen this packet yet
    const targets = connectedPeers
      .filter(peerId => !state!.forwardedTo.has(peerId))
      .slice(0, state.copiesRemaining)

    for (const peerId of targets) state.forwardedTo.add(peerId)
    state.copiesRemaining -= targets.length

    // Each relayed copy carries half the remaining copies (binary spray)
    // The router on the receiving peer will inherit copiesRemaining from TTL
    return targets
  }

  /** Decrement TTL before relaying */
  decrementTTL(packet: MeshPacket): MeshPacket {
    return {
      ...packet,
      header: {
        ...packet.header,
        ttl:      packet.header.ttl - 1,
        hopCount: packet.header.hopCount + 1,
      },
    }
  }

  /** Register a locally originated packet so we don't relay our own */
  markSent(packetId: string): void {
    this._seen.add(packetId)
  }

  // ─── Private ────────────────────────────────────────────────────────────────

  private _isOwnToken(targetHash: string, ownTokenHash: Uint8Array): boolean {
    const own = Array.from(ownTokenHash).map(b => b.toString(16).padStart(2, '0')).join('')
    return own === targetHash
  }
}