/**
 * Presence Feature
 *
 * Broadcasts this device's online status to all reachable peers on a
 * configurable heartbeat interval, and tracks the status of remote peers
 * based on the presence messages they broadcast.
 *
 * Peers whose presence heartbeat has not been received within PRESENCE_TTL_MS
 * are automatically degraded to 'offline'.
 */

import { generateUUID } from '../utils/uuid'
import { PRESENCE_HEARTBEAT_MS, PRESENCE_TTL_MS } from '../constants/ble'
import type { TransportManager } from '../core/TransportManager'
import type { PeerRegistry } from '../core/PeerRegistry'
import type { EventBus } from '../core/EventBus'
import type { PresenceStatus, PresenceMessage } from '../types/ble'

export class Presence {
  private transport: TransportManager
  private registry: PeerRegistry
  private bus: EventBus
  private selfId: string
  private displayName: string
  private status: PresenceStatus = 'online'
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private staleTtlTimer: ReturnType<typeof setInterval> | null = null
  /** peerId → last presence timestamp */
  private lastSeen = new Map<string, number>()
  private unsubscribers: Array<() => void> = []

  constructor(
    selfId: string,
    displayName: string,
    transport: TransportManager,
    registry: PeerRegistry,
    bus: EventBus,
  ) {
    this.selfId = selfId
    this.displayName = displayName
    this.transport = transport
    this.registry = registry
    this.bus = bus
  }

  // ── Lifecycle ──────────────────────────────────────────────────────────────

  start(): void {
    this.registerListeners()
    // Immediate broadcast on startup.
    void this.broadcastPresence()
    this.heartbeatTimer = setInterval(() => {
      void this.broadcastPresence()
    }, PRESENCE_HEARTBEAT_MS)

    // Expire stale presences.
    this.staleTtlTimer = setInterval(() => this.evictStale(), 10_000)
  }

  stop(): void {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer)
    if (this.staleTtlTimer) clearInterval(this.staleTtlTimer)
    for (const unsub of this.unsubscribers) unsub()
    void this.broadcastStatus('offline')
  }

  // ── Status ─────────────────────────────────────────────────────────────────

  /** Update this device's presence status and broadcast immediately. */
  async setStatus(status: PresenceStatus): Promise<void> {
    this.status = status
    await this.broadcastPresence()
  }

  getStatus(): PresenceStatus {
    return this.status
  }

  /** Get the last known presence status of a remote peer. */
  getPeerStatus(peerId: string): PresenceStatus {
    return this.registry.get(peerId)?.presenceStatus ?? 'offline'
  }

  // ── Broadcast ─────────────────────────────────────────────────────────────

  private async broadcastPresence(): Promise<void> {
    await this.broadcastStatus(this.status)
  }

  private async broadcastStatus(status: PresenceStatus): Promise<void> {
    const msg: PresenceMessage = {
      msgId: generateUUID(),
      from: this.selfId,
      kind: 'presence',
      status,
      displayName: this.displayName,
      timestamp: Date.now(),
      transport: 'ble-gatt',
    }
    try {
      // FIX: Previously used (this.transport as any).protocol?.encode?.() and
      // (this.transport as any).broadcast() — accessing private internals via
      // type-cast. When protocol was undefined the optional chain silently
      // returned undefined, broadcast([]) was called with an empty array, and
      // every heartbeat was a no-op with no error thrown.
      //
      // broadcastMessage() is the correct public API: it encodes the message,
      // pushes frames via notifyAll() to all subscribed BLE centrals (no new
      // connections needed), and fans out via Multipeer for iOS peers.
      await this.transport.broadcastMessage(msg)
    } catch {
      // best-effort; presence is non-critical
    }
  }

  // ── Receive ───────────────────────────────────────────────────────────────

  private registerListeners(): void {
    this.unsubscribers.push(
      this.bus.on('message:presence', (msg) => this.handlePresence(msg)),
    )
  }

  private handlePresence(msg: PresenceMessage): void {
    if (msg.from === this.selfId) return
    this.lastSeen.set(msg.from, Date.now())
    this.registry.setPresence(msg.from, msg.status)
    this.registry.setDisplayName(msg.from, msg.displayName)
  }

  // ── Stale eviction ────────────────────────────────────────────────────────

  private evictStale(): void {
    const cutoff = Date.now() - PRESENCE_TTL_MS
    for (const [peerId, ts] of this.lastSeen) {
      if (ts < cutoff) {
        this.registry.setPresence(peerId, 'offline')
        this.lastSeen.delete(peerId)
      }
    }
  }
}