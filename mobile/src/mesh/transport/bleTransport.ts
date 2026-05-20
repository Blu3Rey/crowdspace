/**
 * @file bleTransport.ts
 * BLE transport layer — owns the pool of active BleGattSessions and
 * wires up all munim-bluetooth events into the session and packet pipeline.
 *
 * This module is the only place that imports from 'munim-bluetooth'.
 * All other mesh modules talk to BleTransport via its clean TypeScript API.
 */

import {
    addEventListener,
    setServices,
    startAdvertising,
    stopAdvertising,
    stopScan,
    updateCharacteristicValue,
} from 'munim-bluetooth'

import {
    CHAR_ACK,
    CHAR_ANNOUNCE,
    CHAR_INBOX,
    CHAR_RELAY,
    MAX_CONCURRENT_CONNECTIONS,
    MESH_SERVICE_UUID,
} from '../core/constants'
import { bytesToHex, hexToBytes } from '../core/encoding'
import { BleGattSession, SessionCallbacks } from './bleGattSession'

export interface BleTransportConfig {
  /** Current ANNOUNCE payload (16 bytes): tokenHash(8) + pubkeyFingerprint(8) */
  announcePayload: Uint8Array
  /** Service UUID list to advertise (just [MESH_SERVICE_UUID] normally) */
  serviceUUIDs?:  string[]
  localName?:      string
}

/**
 * Callbacks that BleTransport calls upward into PacketHandler / MeshEngine
 */
export interface BleTransportCallbacks {
  /** A fully assembled raw packet arrived from peerId over BLE */
  onPacket(peerId: string, raw: Uint8Array, isRelay: boolean): void
  /** A peer connected (session opened + ANNOUNCE read) */
  onPeerConnected(peerId: string, tokenHash: string, pubkeyFingerprint: string): void
  /** A peer disconnected */
  onPeerDisconnected(peerId: string): void
  /** ACK notification received from a peer */
  onAck(peerId: string, acknowledgedPacketId: string): void
}

export class BleTransport {
  private _sessions     = new Map<string, BleGattSession>()
  private _cleanupFns:  (() => void)[] = []
  private _callbacks:   BleTransportCallbacks
  private _announceHex  = ''
  private _running      = false

  constructor(callbacks: BleTransportCallbacks) {
    this._callbacks = callbacks
  }

  // ─── Lifecycle ──────────────────────────────────────────────────────────────

  start(config: BleTransportConfig): void {
    if (this._running) return
    this._running = true

    this._announceHex = bytesToHex(config.announcePayload)

    // Register our GATT service with all four characteristics
    setServices([
      {
        uuid: MESH_SERVICE_UUID,
        characteristics: [
          {
            uuid:       CHAR_ANNOUNCE,
            properties: ['read', 'notify'],
            value:      this._announceHex,
          },
          {
            uuid:       CHAR_INBOX,
            properties: ['write', 'writeWithoutResponse'],
            value:      '',
          },
          {
            uuid:       CHAR_RELAY,
            properties: ['write', 'writeWithoutResponse'],
            value:      '',
          },
          {
            uuid:       CHAR_ACK,
            properties: ['notify'],
            value:      '',
          },
        ],
      },
    ])

    // Start advertising
    startAdvertising({
      serviceUUIDs: config.serviceUUIDs ?? [MESH_SERVICE_UUID],
      localName:    config.localName,
    })

    this._attachEvents()
  }

  stop(): void {
    if (!this._running) return
    this._running = false

    stopAdvertising()
    stopScan()

    for (const session of this._sessions.values()) session.close()
    this._sessions.clear()

    for (const fn of this._cleanupFns) fn()
    this._cleanupFns = []
  }

  // ─── Outbound API ────────────────────────────────────────────────────────────

  /**
   * Send raw packet bytes to a specific peer via GATT.
   * Opens a new session if one doesn't exist and the pool has room.
   */
  async sendTo(peerId: string, raw: Uint8Array, relay = false): Promise<void> {
    let session = this._sessions.get(peerId)

    if (!session) {
      if (this._sessions.size >= MAX_CONCURRENT_CONNECTIONS) {
        throw new Error('Connection pool full — cannot open another session')
      }
      session = this._openSession(peerId)
      await session.open()
    }

    await session.sendPacket(raw, relay)
  }

  /** Update the ANNOUNCE characteristic value and notify subscribers */
  updateAnnounce(announcePayload: Uint8Array): void {
    this._announceHex = bytesToHex(announcePayload)
    updateCharacteristicValue(MESH_SERVICE_UUID, CHAR_ANNOUNCE, this._announceHex, true)
  }

  /** Push an ACK to all subscribed centrals */
  notifyAck(acknowledgedPacketIdHex: string): void {
    updateCharacteristicValue(MESH_SERVICE_UUID, CHAR_ACK, acknowledgedPacketIdHex, true)
  }

  getConnectedPeerIds(): string[] {
    return Array.from(this._sessions.keys())
  }

  // ─── Private: session pool ───────────────────────────────────────────────────

  private _openSession(deviceId: string): BleGattSession {
    const callbacks: SessionCallbacks = {
      onAnnounce: (id, tokenHash, fingerprint) => {
        this._callbacks.onPeerConnected(id, tokenHash, fingerprint)
      },
      onPacketAssembled: (id, raw, charUUID) => {
        const isRelay = charUUID.toLowerCase() === CHAR_RELAY.toLowerCase()
        this._callbacks.onPacket(id, raw, isRelay)
      },
      onDisconnected: (id) => {
        this._sessions.delete(id)
        this._callbacks.onPeerDisconnected(id)
      },
    }

    const session = new BleGattSession(deviceId, callbacks)
    this._sessions.set(deviceId, session)
    return session
  }

  // ─── Private: munim-bluetooth event wiring ──────────────────────────────────

  private _attachEvents(): void {
    // Peripheral side: a central wrote to CHAR_INBOX or CHAR_RELAY
    const removeWriteListener = addEventListener('peripheralWriteRequest', (ev) => {
      const charUUID = ev.characteristicUUID.toLowerCase()
      if (charUUID !== CHAR_INBOX.toLowerCase() && charUUID !== CHAR_RELAY.toLowerCase()) return

      const frame = hexToBytes(ev.value)
      const session = this._getOrCreatePeripheralSession(ev.centralId)
      session.handleInboundFrame(ev.characteristicUUID, frame)
    })

    // Central side: a peripheral pushed a notification to CHAR_ACK
    const removeCharListener = addEventListener('characteristicValueChanged', (ev) => {
      const charUUID = ev.characteristicUUID.toLowerCase()

      if (charUUID === CHAR_ACK.toLowerCase()) {
        // ACK notification from a peripheral we connected to
        this._callbacks.onAck(ev.deviceId, ev.value)
        return
      }

      // If the peripheral sent us data via notify on some other char, route to session
      const session = this._sessions.get(ev.deviceId)
      if (session && ev.value) {
        session.handleInboundFrame(ev.characteristicUUID, hexToBytes(ev.value))
      }
    })

    // Device disconnected
    const removeDisconnectListener = addEventListener('deviceDisconnected', (ev) => {
      const session = this._sessions.get(ev.deviceId)
      if (session) {
        session.close()
        this._sessions.delete(ev.deviceId)
        this._callbacks.onPeerDisconnected(ev.deviceId)
      }
    })

    this._cleanupFns.push(removeWriteListener, removeCharListener, removeDisconnectListener)
  }

  /**
   * When acting as Peripheral, incoming centrals don't have sessions yet.
   * We lazily create a lightweight session to track their chunk state.
   */
  private _getOrCreatePeripheralSession(centralId: string): BleGattSession {
    let session = this._sessions.get(centralId)
    if (!session) {
      session = this._openSession(centralId)
      // No need to call session.open() — they connected to us
    }
    return session
  }
}