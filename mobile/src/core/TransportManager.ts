/**
 * TransportManager
 *
 * The brain of the mesh engine. It:
 *  - Initialises BLEEngine (always) and Multipeer (iOS only, when available).
 *  - Receives raw hex frames from BLEEngine/Multipeer, assembles them into
 *    logical MeshMessages via MessageProtocol, and emits typed events on the
 *    EventBus so feature modules can react without coupling to transports.
 *  - Exposes send()/sendToMany()/broadcastMessage() which select the best
 *    available transport per-peer and handle fallback.
 *  - Drives PeerRegistry updates from scan results and connection events.
 *
 * FIX: Message deduplication.
 * All inbound messages are checked against a fixed-size LRU seen-ID cache
 * before dispatch. Prevents duplicate delivery when a sender retransmits
 * (e.g. DM retry) and both copies arrive via different paths.
 *
 * FIX: Group fan-out batching.
 * sendToMany() now opens at most MAX_BLE_CONCURRENT_SENDS GATT connections
 * at once. Saturating the BLE radio with N simultaneous connects for a
 * large group reliably causes failures on most platforms.
 *
 * FIX: broadcastMessage() for efficient presence-style broadcasts.
 * Uses notifyAll() (peripheral push, no new connections) + Multipeer fan-out
 * rather than opening individual unicast GATT connections to each peer.
 */

import {
  startMultipeerSession,
  stopMultipeerSession,
  sendMultipeerMessage,
  getCapabilities,
  addEventListener,
} from 'munim-bluetooth'

import {
  MULTIPEER_SERVICE_TYPE,
  MAX_BLE_CONCURRENT_SENDS,
  BATCH_SEND_DELAY_MS,
  SEEN_MESSAGE_CACHE_SIZE,
} from '../constants/ble'
import { BLEEngine } from './BLEEngine'
import { MessageProtocol } from './MessageProtocol'
import { PeerRegistry } from './PeerRegistry'
import type { EventBus } from './EventBus'
import type {
  MeshMessage,
  PeerInfoPayload,
  PeerCapability,
  MeshEngineConfig,
  Transport,
} from '../types/ble'

// --- TransportManager --------------------------------------------------------

export class TransportManager {
  private config: MeshEngineConfig
  private bus: EventBus
  private registry: PeerRegistry
  private protocol: MessageProtocol
  private ble: BLEEngine
  private multipeerAvailable = false
  private unsubscribers: Array<() => void> = []

  // FIX: Inbound deduplication cache.
  // A Set used as a bounded LRU: when it reaches SEEN_MESSAGE_CACHE_SIZE,
  // the oldest entry (first inserted, exploiting Set iteration order) is
  // evicted before adding the new one. This prevents duplicate dispatch when
  // the sender retransmits and both copies reach us.
  private seenMessageIds = new Set<string>()

  constructor(config: MeshEngineConfig, bus: EventBus, registry: PeerRegistry) {
    this.config = config
    this.bus = bus
    this.registry = registry
    this.protocol = new MessageProtocol()

    const selfInfo: PeerInfoPayload = {
      id: config.selfId,
      name: config.displayName,
      v: 1,
      caps: (config.features ?? ['dm', 'group', 'locate', 'presence']) as PeerCapability[],
    }

    this.ble = new BLEEngine(
      {
        onFrameReceived:    (hex, bleDeviceId) => this.handleInboundFrame(hex, bleDeviceId, 'ble-gatt'),
        onPeerInfoRead:     (info, bleDeviceId) => this.handlePeerInfoRead(info, bleDeviceId),
        onDeviceConnected:  (bleDeviceId) => this.handleBleConnected(bleDeviceId),
        onDeviceDisconnected:(bleDeviceId) => this.handleBleDisconnected(bleDeviceId),
        onRssiUpdated:      (bleDeviceId, rssi) => this.handleRssiUpdated(bleDeviceId, rssi),
        onError:            (err, ctx) => this.bus.emit('engine:error', { error: err, context: ctx }),
      },
      selfInfo,
    )
  }

  // -- Initialisation ---------------------------------------------------------

  async init(): Promise<void> {
    await this.ble.init()
    this.ble.onDeviceFound((device: any) => this.handleScanResult(device))

    try {
      const caps = await getCapabilities()
      this.multipeerAvailable = !!(caps as any).multipeerConnectivity
    } catch { /* ignore */ }

    if (this.multipeerAvailable) {
      await this.startMultipeer()
    }

    if (this.config.background) {
      this.ble.startBackground({
        androidNotificationTitle: this.config.androidNotificationTitle,
        androidNotificationText:  this.config.androidNotificationText,
      })
      this.bus.emit('background:started', undefined as any)
    }

    this.bus.emit('engine:ready', {
      selfId: this.config.selfId,
      displayName: this.config.displayName,
    })
  }

  // -- Multipeer --------------------------------------------------------------

  private async startMultipeer(): Promise<void> {
    startMultipeerSession({
      serviceType: MULTIPEER_SERVICE_TYPE,
      displayName: this.config.displayName,
      discoveryInfo: [{ key: 'pid', value: this.config.selfId }],
      autoInvite: true,
      autoAcceptInvitations: true,
      encryptionPreference: 'required',
    })

    this.unsubscribers.push(
      addEventListener('multipeerPeerStateChanged', (peer: any) => {
        if (peer.state === 'connected') {
          const peerId = peer.discoveryInfo?.pid ?? peer.id
          this.registry.upsertFromMultipeer({
            peerId,
            displayName: peer.displayName ?? peer.id,
            multipeerPeerId: peer.id,
            capabilities: ['dm', 'group', 'locate', 'presence'],
          })
          this.registry.setConnectionState(peerId, 'connected')
        } else if (peer.state === 'notConnected') {
          const existing = this.registry.getByMultipeerPeerId(peer.id)
          if (existing) this.registry.setConnectionState(existing.id, 'disconnected')
        }
      }),
    )

    this.unsubscribers.push(
      addEventListener('multipeerMessageReceived', (event: any) => {
        this.handleInboundFrame(event.value, event.peerId, 'multipeer')
      }),
    )
  }

  // -- Send -------------------------------------------------------------------

  /**
   * Encode and send a logical MeshMessage to a specific peer.
   * Falls back to BLE GATT if Multipeer is unavailable.
   * For messages with no recipient, delegates to broadcastMessage().
   */
  async send(msg: MeshMessage): Promise<void> {
    const hexFrames = this.protocol.encode(msg, this.config.selfId)

    // No explicit recipient — treat as a broadcast.
    if (!('to' in msg) && !('groupId' in msg)) {
      await this._broadcastFrames(hexFrames)
      return
    }

    const recipientId = 'to' in msg ? msg.to : 'groupId' in msg ? (msg as any).groupId : null
    if (!recipientId) {
      await this._broadcastFrames(hexFrames)
      return
    }

    const peer = this.registry.get(recipientId)
    if (!peer) throw new Error(`Unknown peer: ${recipientId}`)

    await this.sendToPeer(peer.id, hexFrames, peer.preferredTransport)
  }

  /**
   * Send a message to multiple specific peers (group chat fan-out).
   *
   * FIX: Batched fan-out.
   * Previously this opened N simultaneous GATT connections for N peers.
   * Most BLE stacks cap concurrent connections at 7-8 (some Android devices
   * at 4), so large groups would silently drop messages to overflow members.
   * Now sends in batches of MAX_BLE_CONCURRENT_SENDS with a short pause
   * between batches to let the radio recover.
   */
  async sendToMany(msg: MeshMessage, peerIds: string[]): Promise<void> {
    const hexFrames = this.protocol.encode(msg, this.config.selfId)

    for (let i = 0; i < peerIds.length; i += MAX_BLE_CONCURRENT_SENDS) {
      const batch = peerIds.slice(i, i + MAX_BLE_CONCURRENT_SENDS)
      await Promise.allSettled(
        batch.map((id) => {
          const peer = this.registry.get(id)
          return peer
            ? this.sendToPeer(id, hexFrames, peer.preferredTransport)
            : Promise.resolve()
        }),
      )
      // Short pause between batches so the BLE controller isn't hammered with
      // back-to-back connection requests across the full group.
      if (i + MAX_BLE_CONCURRENT_SENDS < peerIds.length) {
        await sleep(BATCH_SEND_DELAY_MS)
      }
    }
  }

  /**
   * Broadcast a MeshMessage to all reachable peers using the most efficient
   * path for each transport.
   *
   * FIX: Use notifyAll() for BLE peers (peripheral push — no new connections
   * needed) instead of opening individual unicast GATT connections to each
   * peer. Multipeer peers receive a single sendMultipeerMessage() call with
   * all their IDs collected upfront. This is the intended path for high-
   * frequency messages like presence heartbeats.
   */
  async broadcastMessage(msg: MeshMessage): Promise<void> {
    const hexFrames = this.protocol.encode(msg, this.config.selfId)
    await this._broadcastFrames(hexFrames)
  }

  private async _broadcastFrames(hexFrames: string[]): Promise<void> {
    // Push to all BLE centrals currently subscribed to our GATT peripheral.
    // This is a single peripheral-side operation — no connection setup needed.
    for (const frame of hexFrames) {
      this.ble.notifyAll(frame)
    }

    // Separately fan out via Multipeer to iOS peers. Collect all multipeer
    // IDs and send once per frame rather than one call per peer per frame.
    if (this.multipeerAvailable) {
      const multipeerIds = this.registry
        .getAll()
        .filter((p) => p.multipeerPeerId != null)
        .map((p) => p.multipeerPeerId!)

      if (multipeerIds.length > 0) {
        for (const frame of hexFrames) {
          try {
            await sendMultipeerMessage(frame, multipeerIds, true)
          } catch { /* best-effort */ }
        }
      }
    }
  }

  private async sendToPeer(
    peerId: string,
    hexFrames: string[],
    preferredTransport: Transport,
  ): Promise<void> {
    const peer = this.registry.get(peerId)
    if (!peer) return

    // Prefer Multipeer for iOS-to-iOS; fall through to BLE GATT otherwise.
    if (
      preferredTransport === 'multipeer' &&
      this.multipeerAvailable &&
      peer.multipeerPeerId
    ) {
      try {
        for (const frame of hexFrames) {
          await sendMultipeerMessage(frame, [peer.multipeerPeerId], true)
        }
        return
      } catch {
        // Fall through to BLE GATT.
      }
    }

    if (!peer.bleDeviceId) {
      throw new Error(`No BLE device ID for peer ${peerId}`)
    }
    await this.ble.send(peer.bleDeviceId, hexFrames)
  }

  // -- Inbound frame handling -------------------------------------------------

  private handleInboundFrame(
    hexFrame: string,
    _sourceDeviceId: string,
    transport: Transport,
  ): void {
    this.protocol.receive(hexFrame, transport, (msg) => this.dispatchMessage(msg, transport))
  }

  /**
   * FIX: Deduplication before dispatch.
   * Checks msgId against the seen-ID cache. Discards duplicates silently.
   * The cache is bounded: when full, the oldest entry (lowest insertion
   * order in the Set) is evicted before the new ID is inserted.
   */
  private dispatchMessage(msg: MeshMessage, _transport: Transport): void {
    if (this.isDuplicate(msg.msgId)) return

    switch (msg.kind) {
      case 'dm':           this.bus.emit('message:dm',           msg as any); break
      case 'dm_ack':       this.bus.emit('message:dm_ack',       msg as any); break
      case 'group':        this.bus.emit('message:group',        msg as any); break
      case 'group_invite': this.bus.emit('message:group_invite', msg as any); break
      case 'group_meta':   this.bus.emit('message:group_meta',   msg as any); break
      case 'presence':     this.bus.emit('message:presence',     msg as any); break
      case 'ping':         this.bus.emit('message:ping',         msg as any); break
      case 'pong':         this.bus.emit('message:pong',         msg as any); break
      case 'locate_req':   this.bus.emit('message:locate_req',   msg as any); break
      case 'locate_res':   this.bus.emit('message:locate_res',   msg as any); break
    }
  }

  private isDuplicate(msgId: string): boolean {
    if (this.seenMessageIds.has(msgId)) return true
    // Evict the oldest entry when the cache is full. Set preserves insertion
    // order, so values().next().value is the oldest.
    if (this.seenMessageIds.size >= SEEN_MESSAGE_CACHE_SIZE) {
      const oldest = this.seenMessageIds.values().next().value
      if (oldest !== undefined) this.seenMessageIds.delete(oldest)
    }
    this.seenMessageIds.add(msgId)
    return false
  }

  // -- Peer discovery callbacks -----------------------------------------------

  private handleScanResult(device: any): void {
    if (
      !device.serviceUUIDs?.some(
        (u: string) => u.toLowerCase() === 'c39b6354-f7e2-4a8b-92d3-5e8a1b0f2c7d',
      )
    ) {
      return
    }
    this.registry.upsertFromScan({
      peerId:       device.id,
      displayName:  device.localName ?? device.name ?? device.id,
      bleDeviceId:  device.id,
      rssi:         device.rssi ?? null,
      capabilities: ['dm', 'group', 'locate', 'presence'],
    })
  }

  private handlePeerInfoRead(info: PeerInfoPayload, bleDeviceId: string): void {
    const provisional = this.registry.getByBleDeviceId(bleDeviceId)
    if (provisional && provisional.id !== info.id) {
      this.registry.upsertFromScan({
        peerId:       info.id,
        displayName:  info.name,
        bleDeviceId,
        rssi:         provisional.rssi,
        capabilities: info.caps,
      })
    } else {
      this.registry.upsertFromScan({
        peerId:       info.id,
        displayName:  info.name,
        bleDeviceId,
        rssi:         null,
        capabilities: info.caps,
      })
    }
    this.registry.setConnectionState(info.id, 'connected')
  }

  private handleBleConnected(bleDeviceId: string): void {
    const peer = this.registry.getByBleDeviceId(bleDeviceId)
    if (peer) this.registry.setConnectionState(peer.id, 'connected')
  }

  private handleBleDisconnected(bleDeviceId: string): void {
    const peer = this.registry.getByBleDeviceId(bleDeviceId)
    if (peer) this.registry.setConnectionState(peer.id, 'disconnected')
  }

  private handleRssiUpdated(bleDeviceId: string, rssi: number): void {
    const peer = this.registry.getByBleDeviceId(bleDeviceId)
    if (peer) this.registry.updateRssi(peer.id, rssi)
  }

  // -- Teardown ---------------------------------------------------------------

  async destroy(): Promise<void> {
    if (this.multipeerAvailable) stopMultipeerSession()
    for (const unsub of this.unsubscribers) unsub()
    if (this.config.background) {
      this.ble.stopBackground()
      this.bus.emit('background:stopped', undefined as any)
    }
    await this.ble.destroy()
    this.registry.stop()
    this.protocol.flush()
    this.seenMessageIds.clear()
    this.bus.emit('engine:stopped', undefined as any)
  }

  // -- Accessors --------------------------------------------------------------

  get bleEngine(): BLEEngine            { return this.ble }
  get peerRegistry(): PeerRegistry      { return this.registry }
  get isMultipeerAvailable(): boolean   { return this.multipeerAvailable }
}

function sleep(ms: number): Promise<void> {
  return new Promise((res) => setTimeout(res, ms))
}