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

import { PRESENCE_HEARTBEAT_MS, PRESENCE_TTL_MS } from '../constants/ble'
import type { EventBus } from '../core/EventBus'
import type { PeerRegistry } from '../core/PeerRegistry'
import type { TransportManager } from '../core/TransportManager'
import type { PresenceMessage, PresenceStatus } from '../types/ble'
import { generateUUID } from '../utils/uuid'

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
      const hexFrames = (this.transport as any).protocol?.encode?.(msg, this.selfId)
      // Presence is broadcast to all — use notify path (peripheral pushes to subscribed centrals)
      // as well as sending directly to connected peers.
      await (this.transport as any).broadcast(
        // Re-encode via TransportManager's internal method if available,
        // otherwise fall through to send().
        hexFrames ?? []
      )
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