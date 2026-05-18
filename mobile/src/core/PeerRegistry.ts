/**
 * PeerRegistry
 *
 * Central in-memory store for all discovered and connected peers.
 *
 * Responsibilities:
 *  • Upsert peers as they are discovered via BLE scan or Multipeer.
 *  • Track connection state transitions.
 *  • Maintain a rolling RSSI window and derived distance estimates.
 *  • Expire stale peers (not seen recently).
 *  • Emit EventBus events on peer state changes.
 */

import {
  PATH_LOSS_EXPONENT,
  PEER_STALE_TIMEOUT_MS,
  RSSI_AT_1M,
  RSSI_SAMPLE_COUNT,
} from '../constants/ble'
import type { Peer, PeerCapability, PeerConnectionState } from '../types/ble'
import type { EventBus } from './EventBus'

export class PeerRegistry {
  private peers = new Map<string, Peer>()
  private bus: EventBus
  private rssiWindows = new Map<string, number[]>()
  private staleTimer: ReturnType<typeof setInterval> | null = null

  constructor(bus: EventBus) {
    this.bus = bus
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  start(): void {
    this.staleTimer = setInterval(() => this.evictStale(), 15_000)
  }

  stop(): void {
    if (this.staleTimer) clearInterval(this.staleTimer)
    this.peers.clear()
    this.rssiWindows.clear()
  }

  // ── Upsert / Update ───────────────────────────────────────────────────────

  /**
   * Upsert a peer from a BLE scan result.
   * `bleDeviceId` is the platform-opaque device ID from munim-bluetooth.
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
    if (isNew) {
      this.bus.emit('peer:discovered', peer)
    } else {
      this.bus.emit('peer:updated', peer)
    }
    return peer
  }

  // ── State Transitions ─────────────────────────────────────────────────────

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

  // ── RSSI ──────────────────────────────────────────────────────────────────

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

  // ── Queries ───────────────────────────────────────────────────────────────

  get(peerId: string): Peer | undefined {
    return this.peers.get(peerId)
  }

  getByBleDeviceId(bleId: string): Peer | undefined {
    for (const peer of this.peers.values()) {
      if (peer.bleDeviceId === bleId) return peer
    }
    return undefined
  }

  getByMultipeerPeerId(mpId: string): Peer | undefined {
    for (const peer of this.peers.values()) {
      if (peer.multipeerPeerId === mpId) return peer
    }
    return undefined
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

  // ── Stale Eviction ────────────────────────────────────────────────────────

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
        this.bus.emit('peer:lost', peer)
      }
    }
  }
}