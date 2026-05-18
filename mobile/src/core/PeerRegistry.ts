/**
 * PeerRegistry
 *
 * Central in-memory store for all discovered and connected peers.
 *
 * Responsibilities:
 *  - Upsert peers as they are discovered via BLE scan or Multipeer.
 *  - Track connection state transitions.
 *  - Maintain a rolling RSSI window and derived distance estimates.
 *  - Expire stale peers (not seen recently).
 *  - Emit EventBus events on peer state changes.
 *
 * FIX: O(1) reverse-lookup indices.
 * getByBleDeviceId() and getByMultipeerPeerId() are called on every RSSI
 * update, disconnection, and inbound frame — i.e. constantly. Both were O(n)
 * linear scans over all peers. Two reverse-lookup Maps (bleDeviceId -> peerId,
 * multipeerPeerId -> peerId) make them O(1) at the cost of a few extra map
 * writes on upsert.
 */

import {
  RSSI_SAMPLE_COUNT,
  RSSI_AT_1M,
  PATH_LOSS_EXPONENT,
  PEER_STALE_TIMEOUT_MS,
} from '../constants/ble'
import type { Peer, PeerConnectionState, Transport, PeerCapability } from '../types/ble'
import type { EventBus } from './EventBus'

export class PeerRegistry {
  private peers = new Map<string, Peer>()
  private bus: EventBus
  private rssiWindows = new Map<string, number[]>()
  private staleTimer: ReturnType<typeof setInterval> | null = null

  // FIX: Reverse-lookup indices for O(1) access by transport-layer IDs.
  // Maintained in sync with the peers map on every upsert and eviction.
  private bleDeviceIdIndex    = new Map<string, string>() // bleDeviceId    -> peerId
  private multipeerIdIndex    = new Map<string, string>() // multipeerPeerId -> peerId

  constructor(bus: EventBus) {
    this.bus = bus
  }

  // -- Lifecycle --------------------------------------------------------------

  start(): void {
    this.staleTimer = setInterval(() => this.evictStale(), 15_000)
  }

  stop(): void {
    if (this.staleTimer) clearInterval(this.staleTimer)
    this.peers.clear()
    this.rssiWindows.clear()
    this.bleDeviceIdIndex.clear()
    this.multipeerIdIndex.clear()
  }

  // -- Upsert / Update --------------------------------------------------------

  /**
   * Upsert a peer from a BLE scan result.
   * bleDeviceId is the platform-opaque device identifier from munim-bluetooth.
   */
  upsertFromScan(params: {
    peerId: string
    displayName: string
    bleDeviceId: string
    rssi: number | null
    capabilities: PeerCapability[]
  }): Peer {
    const existing = this.peers.get(params.peerId)
    const isNew = !existing

    // FIX: If this peer had a different bleDeviceId before (e.g. device
    // re-paired), clean up the stale index entry to prevent a ghost key.
    if (existing?.bleDeviceId && existing.bleDeviceId !== params.bleDeviceId) {
      this.bleDeviceIdIndex.delete(existing.bleDeviceId)
    }

    const peer: Peer = {
      id: params.peerId,
      displayName: params.displayName,
      bleDeviceId: params.bleDeviceId,
      multipeerPeerId: existing?.multipeerPeerId ?? null,
      capabilities: params.capabilities,
      connectionState: existing?.connectionState ?? 'discovered',
      presenceStatus: existing?.presenceStatus ?? 'offline',
      rssi: params.rssi,
      rssiSmoothed: this.smoothRssi(params.peerId, params.rssi),
      estimatedDistance: params.rssi != null ? this.estimateDistance(params.rssi) : null,
      lastSeen: Date.now(),
      preferredTransport: existing?.preferredTransport ?? 'ble-gatt',
    }

    this.peers.set(params.peerId, peer)
    this.bleDeviceIdIndex.set(params.bleDeviceId, params.peerId)

    if (isNew) {
      this.bus.emit('peer:discovered', peer)
    } else {
      this.bus.emit('peer:updated', peer)
    }
    return peer
  }

  /**
   * Upsert a peer from an Apple Multipeer discovery event (iOS only).
   */
  upsertFromMultipeer(params: {
    peerId: string
    displayName: string
    multipeerPeerId: string
    capabilities: PeerCapability[]
  }): Peer {
    const existing = this.peers.get(params.peerId)
    const isNew = !existing

    // FIX: Remove stale multipeer index entry if the multipeer ID changed.
    if (existing?.multipeerPeerId && existing.multipeerPeerId !== params.multipeerPeerId) {
      this.multipeerIdIndex.delete(existing.multipeerPeerId)
    }

    const peer: Peer = {
      id: params.peerId,
      displayName: params.displayName,
      bleDeviceId: existing?.bleDeviceId ?? null,
      multipeerPeerId: params.multipeerPeerId,
      capabilities: params.capabilities,
      connectionState: existing?.connectionState ?? 'discovered',
      presenceStatus: existing?.presenceStatus ?? 'offline',
      rssi: existing?.rssi ?? null,
      rssiSmoothed: existing?.rssiSmoothed ?? null,
      estimatedDistance: existing?.estimatedDistance ?? null,
      lastSeen: Date.now(),
      preferredTransport: 'multipeer',
    }

    this.peers.set(params.peerId, peer)
    this.multipeerIdIndex.set(params.multipeerPeerId, params.peerId)

    if (isNew) {
      this.bus.emit('peer:discovered', peer)
    } else {
      this.bus.emit('peer:updated', peer)
    }
    return peer
  }

  // -- State Transitions ------------------------------------------------------

  setConnectionState(peerId: string, state: PeerConnectionState): void {
    const peer = this.peers.get(peerId)
    if (!peer) return
    const updated: Peer = { ...peer, connectionState: state, lastSeen: Date.now() }
    this.peers.set(peerId, updated)

    if (state === 'connected' || state === 'subscribed') {
      this.bus.emit('peer:connected', updated)
    } else if (state === 'disconnected' || state === 'unreachable') {
      this.bus.emit('peer:disconnected', updated)
    } else {
      this.bus.emit('peer:updated', updated)
    }
  }

  setPresence(peerId: string, status: Peer['presenceStatus']): void {
    const peer = this.peers.get(peerId)
    if (!peer) return
    const updated: Peer = { ...peer, presenceStatus: status, lastSeen: Date.now() }
    this.peers.set(peerId, updated)
    this.bus.emit('peer:updated', updated)
  }

  setDisplayName(peerId: string, name: string): void {
    const peer = this.peers.get(peerId)
    if (!peer) return
    const updated: Peer = { ...peer, displayName: name, lastSeen: Date.now() }
    this.peers.set(peerId, updated)
    this.bus.emit('peer:updated', updated)
  }

  // -- RSSI -------------------------------------------------------------------

  updateRssi(peerId: string, rssi: number): void {
    const peer = this.peers.get(peerId)
    if (!peer) return
    const smoothed = this.smoothRssi(peerId, rssi)
    const updated: Peer = {
      ...peer,
      rssi,
      rssiSmoothed: smoothed,
      estimatedDistance: this.estimateDistance(smoothed ?? rssi),
      lastSeen: Date.now(),
    }
    this.peers.set(peerId, updated)
    this.bus.emit('peer:updated', updated)
  }

  private smoothRssi(peerId: string, rssi: number | null): number | null {
    if (rssi == null) return this.peers.get(peerId)?.rssiSmoothed ?? null
    let window = this.rssiWindows.get(peerId) ?? []
    window = [...window.slice(-(RSSI_SAMPLE_COUNT - 1)), rssi]
    this.rssiWindows.set(peerId, window)
    return window.reduce((a, b) => a + b, 0) / window.length
  }

  /**
   * Estimate distance in metres from an RSSI reading using the log-distance
   * path-loss model: d = 10 ^ ((RSSI_AT_1M - rssi) / (10 * n))
   */
  private estimateDistance(rssi: number): number {
    return Math.pow(10, (RSSI_AT_1M - rssi) / (10 * PATH_LOSS_EXPONENT))
  }

  // -- Queries ----------------------------------------------------------------

  get(peerId: string): Peer | undefined {
    return this.peers.get(peerId)
  }

  /** O(1) lookup via reverse index. */
  getByBleDeviceId(bleId: string): Peer | undefined {
    const peerId = this.bleDeviceIdIndex.get(bleId)
    return peerId ? this.peers.get(peerId) : undefined
  }

  /** O(1) lookup via reverse index. */
  getByMultipeerPeerId(mpId: string): Peer | undefined {
    const peerId = this.multipeerIdIndex.get(mpId)
    return peerId ? this.peers.get(peerId) : undefined
  }

  getAll(): Peer[] {
    return Array.from(this.peers.values())
  }

  getConnected(): Peer[] {
    return this.getAll().filter(
      (p) => p.connectionState === 'connected' || p.connectionState === 'subscribed',
    )
  }

  getNearby(): Peer[] {
    return this.getAll().filter((p) => p.connectionState !== 'unreachable')
  }

  // -- Stale Eviction ---------------------------------------------------------

  private evictStale(): void {
    const cutoff = Date.now() - PEER_STALE_TIMEOUT_MS
    for (const [id, peer] of this.peers) {
      if (
        peer.lastSeen < cutoff &&
        peer.connectionState !== 'connected' &&
        peer.connectionState !== 'subscribed'
      ) {
        this.peers.delete(id)
        this.rssiWindows.delete(id)
        // FIX: Clean up reverse-index entries for evicted peers.
        if (peer.bleDeviceId)    this.bleDeviceIdIndex.delete(peer.bleDeviceId)
        if (peer.multipeerPeerId) this.multipeerIdIndex.delete(peer.multipeerPeerId)
        this.bus.emit('peer:lost', peer)
      }
    }
  }
}