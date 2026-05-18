/**
 * DeviceLocator Feature
 *
 * Provides RSSI-based proximity estimation and ping/pong round-trip timing.
 *
 * Two complementary mechanisms:
 *
 *  1. **Passive RSSI** — BLEEngine polls readRSSI() at a fixed interval on
 *     every connected device. PeerRegistry maintains a rolling average.
 *     Distance is estimated from the log-distance path-loss model.
 *
 *  2. **Active locate request** — A `locate_req` frame is sent to a specific
 *     peer. The peer responds with a `locate_res` that includes its own RSSI
 *     reading of the requesting device. Combining both readings gives a
 *     symmetric distance estimate.
 *
 *  3. **Ping/pong** — Measures round-trip time (RTT) as a proxy for link
 *     quality. BLE latency correlates loosely with distance.
 */

import type { EventBus } from '../core/EventBus'
import type { PeerRegistry } from '../core/PeerRegistry'
import type { TransportManager } from '../core/TransportManager'
import type { LocateRequest, LocateResponse, PingMessage } from '../types/ble'
import { generateUUID } from '../utils/uuid'

export interface RangeResult {
  peerId: string
  /** Local RSSI reading (dBm). */
  localRssi: number | null
  /** Peer's RSSI reading of us (dBm). */
  peerRssi: number | null
  /** Estimated distance in metres from local readings. */
  estimatedDistanceM: number | null
  /** Round-trip time in milliseconds. */
  rttMs: number | null
}

export class DeviceLocator {
  private transport: TransportManager
  private registry: PeerRegistry
  private bus: EventBus
  private selfId: string
  private pendingLocate = new Map<string, {
    resolve: (res: RangeResult) => void
    reject: (err: Error) => void
    peerId: string
    startTime: number
    timerId: ReturnType<typeof setTimeout>
  }>()
  private pendingPing = new Map<string, {
    resolve: (rttMs: number) => void
    reject: (err: Error) => void
    startTime: number
    timerId: ReturnType<typeof setTimeout>
  }>()
  private unsubscribers: Array<() => void> = []

  constructor(
    selfId: string,
    transport: TransportManager,
    registry: PeerRegistry,
    bus: EventBus,
  ) {
    this.selfId = selfId
    this.transport = transport
    this.registry = registry
    this.bus = bus
    this.registerListeners()
  }

  // ── Active Range Request ──────────────────────────────────────────────────

  /**
   * Request a symmetric RSSI range measurement from a specific peer.
   * Returns a RangeResult once the peer responds or the request times out.
   */
  locate(peerId: string, timeoutMs = 10_000): Promise<RangeResult> {
    return new Promise((resolve, reject) => {
      const nonce = generateUUID().slice(0, 8)
      const req: LocateRequest = {
        msgId: generateUUID(),
        from: this.selfId,
        to: peerId,
        kind: 'locate_req',
        nonce,
        timestamp: Date.now(),
        transport: 'ble-gatt',
      }

      const timerId = setTimeout(() => {
        this.pendingLocate.delete(nonce)
        const peer = this.registry.get(peerId)
        resolve({
          peerId,
          localRssi: peer?.rssiSmoothed ?? null,
          peerRssi: null,
          estimatedDistanceM: peer?.estimatedDistance ?? null,
          rttMs: null,
        })
      }, timeoutMs)

      this.pendingLocate.set(nonce, { resolve, reject, peerId, startTime: Date.now(), timerId })

      this.transport.send(req).catch((err) => {
        clearTimeout(timerId)
        this.pendingLocate.delete(nonce)
        reject(err)
      })
    })
  }

  // ── Ping / RTT ────────────────────────────────────────────────────────────

  /** Measure round-trip time to a peer via ping/pong. */
  ping(peerId: string, timeoutMs = 8_000): Promise<number> {
    return new Promise((resolve, reject) => {
      const nonce = generateUUID().slice(0, 8)
      const msg: PingMessage = {
        msgId: generateUUID(),
        from: this.selfId,
        to: peerId,
        kind: 'ping',
        nonce,
        timestamp: Date.now(),
        transport: 'ble-gatt',
      }

      const timerId = setTimeout(() => {
        this.pendingPing.delete(nonce)
        reject(new Error('Ping timeout'))
      }, timeoutMs)

      this.pendingPing.set(nonce, { resolve, reject, startTime: Date.now(), timerId })

      this.transport.send(msg).catch((err) => {
        clearTimeout(timerId)
        this.pendingPing.delete(nonce)
        reject(err)
      })
    })
  }

  /**
   * Scan nearby peers and return a sorted list by estimated distance.
   * Fires active locate requests to all connected peers.
   */
  async scanNearby(timeoutMs = 12_000): Promise<RangeResult[]> {
    const connected = this.registry.getConnected()
    const results = await Promise.allSettled(
      connected.map((peer) => this.locate(peer.id, timeoutMs)),
    )
    return results
      .filter((r) => r.status === 'fulfilled')
      .map((r) => (r as PromiseFulfilledResult<RangeResult>).value)
      .sort((a, b) => {
        if (a.estimatedDistanceM == null) return 1
        if (b.estimatedDistanceM == null) return -1
        return a.estimatedDistanceM - b.estimatedDistanceM
      })
  }

  // ── Inbound Handling ──────────────────────────────────────────────────────

  private registerListeners(): void {
    // Respond to locate_req from peers.
    this.unsubscribers.push(
      this.bus.on('message:locate_req', async (req) => {
        const peerInRegistry = this.registry.getByBleDeviceId(req.from) ?? this.registry.get(req.from)
        const peerRssi = peerInRegistry?.rssiSmoothed ?? null
        const res: LocateResponse = {
          msgId: generateUUID(),
          from: this.selfId,
          to: req.from,
          kind: 'locate_res',
          nonce: req.nonce,
          peerRssi,
          timestamp: Date.now(),
          transport: req.transport,
        }
        try { await this.transport.send(res) } catch { /* best-effort */ }
      }),
    )

    // Receive locate_res and resolve pending requests.
    this.unsubscribers.push(
      this.bus.on('message:locate_res', (res) => {
        const pending = this.pendingLocate.get(res.nonce)
        if (!pending) return
        clearTimeout(pending.timerId)
        this.pendingLocate.delete(res.nonce)
        const peer = this.registry.get(pending.peerId)
        pending.resolve({
          peerId: pending.peerId,
          localRssi: peer?.rssiSmoothed ?? null,
          peerRssi: res.peerRssi,
          estimatedDistanceM: peer?.estimatedDistance ?? null,
          rttMs: Date.now() - pending.startTime,
        })
      }),
    )

    // Respond to pings.
    this.unsubscribers.push(
      this.bus.on('message:ping', async (ping) => {
        const pong: PingMessage = {
          msgId: generateUUID(),
          from: this.selfId,
          to: ping.from,
          kind: 'pong',
          nonce: ping.nonce,
          timestamp: Date.now(),
          transport: ping.transport,
        }
        try { await this.transport.send(pong) } catch { /* best-effort */ }
      }),
    )

    // Resolve pending pings on pong receipt.
    this.unsubscribers.push(
      this.bus.on('message:pong', (pong) => {
        const pending = this.pendingPing.get(pong.nonce)
        if (!pending) return
        clearTimeout(pending.timerId)
        this.pendingPing.delete(pong.nonce)
        pending.resolve(Date.now() - pending.startTime)
      }),
    )
  }

  // ── Passive RSSI Queries ──────────────────────────────────────────────────

  /** Get the current best-estimate distance to a peer (metres). Passive, no network traffic. */
  getDistance(peerId: string): number | null {
    return this.registry.get(peerId)?.estimatedDistance ?? null
  }

  /** Get the smoothed RSSI to a peer (dBm). */
  getRssi(peerId: string): number | null {
    return this.registry.get(peerId)?.rssiSmoothed ?? null
  }

  // ── Teardown ──────────────────────────────────────────────────────────────

  destroy(): void {
    for (const { timerId } of this.pendingLocate.values()) clearTimeout(timerId)
    for (const { timerId } of this.pendingPing.values()) clearTimeout(timerId)
    this.pendingLocate.clear()
    this.pendingPing.clear()
    for (const unsub of this.unsubscribers) unsub()
  }
}