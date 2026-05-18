/**
 * TransportManager
 *
 * The brain of the mesh engine. It:
 *  • Initialises BLEEngine (always) and Multipeer (iOS only, when available).
 *  • Receives raw hex frames from BLEEngine/Multipeer, assembles them into
 *    logical MeshMessages via MessageProtocol, and emits typed events on the
 *    EventBus so that feature modules can react without coupling to transports.
 *  • Exposes `send(peerId, message)` which selects the best available
 *    transport per-peer and handles fallback.
 *  • Drives PeerRegistry updates from scan results and connection events.
 */

import {
  addEventListener,
  getCapabilities,
  sendMultipeerMessage,
  startMultipeerSession,
  stopMultipeerSession,
} from 'munim-bluetooth'

import {
  MESH_SERVICE_UUID,
  MULTIPEER_SERVICE_TYPE
} from '../constants/ble'
import type {
  MeshEngineConfig,
  MeshMessage,
  PeerCapability,
  PeerInfoPayload,
  Transport,
} from '../types/ble'
import { BLEEngine } from './BLEEngine'
import type { EventBus } from './EventBus'
import { MessageProtocol } from './MessageProtocol'
import { PeerRegistry } from './PeerRegistry'

// ─── TransportManager ─────────────────────────────────────────────────────────

export class TransportManager {
  private config: MeshEngineConfig
  private bus: EventBus
  private registry: PeerRegistry
  private protocol: MessageProtocol
  private ble: BLEEngine
  private multipeerAvailable = false
  private unsubscribers: Array<() => void> = []

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
        onFrameReceived: (hex, bleDeviceId) => this.handleInboundFrame(hex, bleDeviceId, 'ble-gatt'),
        onPeerInfoRead: (info, bleDeviceId) => this.handlePeerInfoRead(info, bleDeviceId),
        onDeviceConnected: (bleDeviceId) => this.handleBleConnected(bleDeviceId),
        onDeviceDisconnected: (bleDeviceId) => this.handleBleDisconnected(bleDeviceId),
        onRssiUpdated: (bleDeviceId, rssi) => this.handleRssiUpdated(bleDeviceId, rssi),
        onError: (err, ctx) => this.bus.emit('engine:error', { error: err, context: ctx }),
      },
      selfInfo,
    )
  }

  // ── Initialisation ────────────────────────────────────────────────────────

  async init(): Promise<void> {
    await this.ble.init()
    this.ble.onDeviceFound((device: any) => this.handleScanResult(device))

    // Check if Multipeer is available (iOS only).
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
        androidNotificationText: this.config.androidNotificationText,
      })
      this.bus.emit('background:started', undefined as any)
    }

    this.bus.emit('engine:ready', {
      selfId: this.config.selfId,
      displayName: this.config.displayName,
    })
  }

  // ── Multipeer ─────────────────────────────────────────────────────────────

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
          // discoveryInfo carries the peer's stable ID as `pid`.
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

  // ── Send ──────────────────────────────────────────────────────────────────

  /**
   * Encode and send a logical MeshMessage to a specific peer (or broadcast).
   * Selects best transport, falls back to BLE GATT if Multipeer unavailable.
   */
  async send(msg: MeshMessage): Promise<void> {
    const hexFrames = this.protocol.encode(msg, this.config.selfId)

    // Broadcast (no recipient).
    if (!('to' in msg) && !('groupId' in msg)) {
      await this.broadcast(hexFrames)
      return
    }

    const recipientId =
      'to' in msg ? msg.to : 'groupId' in msg ? (msg as any).groupId : null

    if (!recipientId) {
      await this.broadcast(hexFrames)
      return
    }

    const peer = this.registry.get(recipientId)
    if (!peer) {
      // For group messages, resolve member IDs and fan out.
      throw new Error(`Unknown peer: ${recipientId}`)
    }

    await this.sendToPeer(peer.id, hexFrames, peer.preferredTransport)
  }

  /**
   * Send to multiple peers (used by GroupChat fan-out).
   */
  async sendToMany(msg: MeshMessage, peerIds: string[]): Promise<void> {
    const hexFrames = this.protocol.encode(msg, this.config.selfId)
    await Promise.allSettled(
      peerIds.map((id) => {
        const peer = this.registry.get(id)
        return peer
          ? this.sendToPeer(id, hexFrames, peer.preferredTransport)
          : Promise.resolve()
      }),
    )
  }

  /** Broadcast to all known peers (presence heartbeats, locate requests, etc.). */
  async broadcast(hexFrames: string[]): Promise<void> {
    const peers = this.registry.getNearby()
    await Promise.allSettled(
      peers.map((peer) => this.sendToPeer(peer.id, hexFrames, peer.preferredTransport)),
    )
  }

  private async sendToPeer(
    peerId: string,
    hexFrames: string[],
    preferredTransport: Transport,
  ): Promise<void> {
    const peer = this.registry.get(peerId)
    if (!peer) return

    // Try Multipeer first for iOS-to-iOS.
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

    // BLE GATT path.
    if (!peer.bleDeviceId) {
      throw new Error(`No BLE device ID for peer ${peerId}`)
    }
    await this.ble.send(peer.bleDeviceId, hexFrames)
  }

  // ── Inbound frame handling ─────────────────────────────────────────────────

  private handleInboundFrame(
    hexFrame: string,
    _sourceDeviceId: string,
    transport: Transport,
  ): void {
    this.protocol.receive(hexFrame, transport, (msg) => this.dispatchMessage(msg, transport))
  }

  private dispatchMessage(msg: MeshMessage, transport: Transport): void {
    // Emit a typed EventBus event based on kind.
    switch (msg.kind) {
      case 'dm':           this.bus.emit('message:dm', msg as any); break
      case 'dm_ack':       this.bus.emit('message:dm_ack', msg as any); break
      case 'group':        this.bus.emit('message:group', msg as any); break
      case 'group_invite': this.bus.emit('message:group_invite', msg as any); break
      case 'group_meta':   this.bus.emit('message:group_meta', msg as any); break
      case 'presence':     this.bus.emit('message:presence', msg as any); break
      case 'ping':         this.bus.emit('message:ping', msg as any); break
      case 'pong':         this.bus.emit('message:pong', msg as any); break
      case 'locate_req':   this.bus.emit('message:locate_req', msg as any); break
      case 'locate_res':   this.bus.emit('message:locate_res', msg as any); break
    }
  }

  // ── Peer discovery callbacks ──────────────────────────────────────────────

  private handleScanResult(device: any): void {
    // Only process devices advertising MESH_SERVICE_UUID.
    if (!device.serviceUUIDs?.some((u: string) => u.toLowerCase() === MESH_SERVICE_UUID.toLowerCase())) {
      return
    }
    // We'll fill in the stable peerId after connecting and reading PEER_INFO_CHAR.
    // For now, use bleDeviceId as a provisional identifier.
    this.registry.upsertFromScan({
      peerId: device.id,              // updated when we read peer info
      displayName: device.localName ?? device.name ?? device.id,
      bleDeviceId: device.id,
      rssi: device.rssi ?? null,
      capabilities: ['dm', 'group', 'locate', 'presence'],
    })
  }

  private handlePeerInfoRead(info: PeerInfoPayload, bleDeviceId: string): void {
    // Update the registry with the stable peer ID (replacing the provisional BLE device ID).
    const provisional = this.registry.getByBleDeviceId(bleDeviceId)
    if (provisional && provisional.id !== info.id) {
      // Re-register under the stable peer ID.
      this.registry.upsertFromScan({
        peerId: info.id,
        displayName: info.name,
        bleDeviceId,
        rssi: provisional.rssi,
        capabilities: info.caps,
      })
    } else {
      this.registry.upsertFromScan({
        peerId: info.id,
        displayName: info.name,
        bleDeviceId,
        rssi: null,
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

  // ── Teardown ──────────────────────────────────────────────────────────────

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
    this.bus.emit('engine:stopped', undefined as any)
  }

  // ── Accessors ─────────────────────────────────────────────────────────────

  get bleEngine(): BLEEngine { return this.ble }
  get peerRegistry(): PeerRegistry { return this.registry }
  get isMultipeerAvailable(): boolean { return this.multipeerAvailable }
}