/**
 * @file bleGattSession.ts
 * Manages a single active GATT connection to one remote peer.
 *
 * Responsibilities:
 *  - Connect / disconnect lifecycle
 *  - Android MTU negotiation (Design choice 4)
 *  - Chunked writes to CHAR_INBOX and CHAR_RELAY
 *  - Subscribing to CHAR_ACK notifications
 *  - Reading CHAR_ANNOUNCE on connect to identify the peer
 *  - Reassembling inbound chunked streams from CHAR_INBOX / CHAR_RELAY
 *
 * One BleGattSession instance exists per connected peer. The BleTransport
 * manages the pool of sessions.
 */

import {
    connect,
    disconnect,
    discoverServices,
    readCharacteristic,
    requestMTU,
    subscribeToCharacteristic,
    unsubscribeFromCharacteristic,
    writeCharacteristic,
} from 'munim-bluetooth'
import {
    Platform,
} from 'react-native'

import {
    CHAR_ACK,
    CHAR_ANNOUNCE,
    CHAR_INBOX,
    CHAR_RELAY,
    GATT_CONNECT_TIMEOUT_MS,
    GATT_IDLE_CLOSE_MS,
    MESH_SERVICE_UUID,
    STREAM_ASSEMBLY_TIMEOUT_MS,
    TARGET_MTU
} from '../core/constants'
import { bytesToHex, chunkBytes, evictStaleStreams, feedChunk, hexToBytes, unpackAnnounce } from '../core/encoding'
import { PendingStream } from '../core/types'

export type SessionState = 'disconnected' | 'connecting' | 'connected' | 'error'

/** Callbacks provided by BleTransport to be called by the session */
export interface SessionCallbacks {
  onAnnounce(deviceId: string, tokenHash: string, pubkeyFingerprint: string): void
  onPacketAssembled(deviceId: string, raw: Uint8Array, charUUID: string): void
  onDisconnected(deviceId: string): void
}

export class BleGattSession {
  readonly deviceId: string

  private _state: SessionState = 'disconnected'
  private _streamId = 0
  private _pendingStreams = new Map<string, PendingStream>()
  private _staleEvictInterval: ReturnType<typeof setInterval> | null = null
  private _idleTimer: ReturnType<typeof setTimeout> | null = null
  private _callbacks: SessionCallbacks
  private _removeAckListener: (() => void) | null = null

  get state(): SessionState { return this._state }

  constructor(deviceId: string, callbacks: SessionCallbacks) {
    this.deviceId  = deviceId
    this._callbacks = callbacks
  }

  // ── Connect ────────────────────────────────────────────────────────────────

  async open(): Promise<void> {
    if (this._state !== 'disconnected') return
    this._state = 'connecting'

    const connectTimeout = new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error('GATT connect timeout')), GATT_CONNECT_TIMEOUT_MS)
    )

    try {
      await Promise.race([connect(this.deviceId), connectTimeout])
    } catch (err) {
      this._state = 'error'
      throw err
    }

    // Negotiate MTU on Android (iOS handles this internally)
    if (Platform.OS === 'android') {
      try {
        await requestMTU(this.deviceId, TARGET_MTU)
      } catch {
        // Non-fatal: continue with default MTU
      }
    }

    // Discover services and verify the peer supports our mesh service
    const services = await discoverServices(this.deviceId)
    const meshSvc  = services.find(s => s.uuid.toLowerCase() === MESH_SERVICE_UUID.toLowerCase())
    if (!meshSvc) {
      await disconnect(this.deviceId)
      this._state = 'error'
      throw new Error(`Peer ${this.deviceId} does not advertise mesh service`)
    }

    // Read ANNOUNCE to identify the peer
    try {
      const announced = await readCharacteristic(this.deviceId, MESH_SERVICE_UUID, CHAR_ANNOUNCE)
      if (announced?.value) {
        const { tokenHash, pubkeyFingerprint } = unpackAnnounce(hexToBytes(announced.value))
        this._callbacks.onAnnounce(
          this.deviceId,
          bytesToHex(tokenHash),
          bytesToHex(pubkeyFingerprint),
        )
      }
    } catch {
      // Non-fatal: peer may have just rotated their token
    }

    // Subscribe to ACK notifications
    subscribeToCharacteristic(this.deviceId, MESH_SERVICE_UUID, CHAR_ACK)

    this._state = 'connected'
    this._startStaleEviction()
    this._resetIdleTimer()
  }

  // ── Disconnect ─────────────────────────────────────────────────────────────

  close(): void {
    this._clearTimers()
    if (this._removeAckListener) {
      this._removeAckListener()
      this._removeAckListener = null
    }
    try {
      unsubscribeFromCharacteristic(this.deviceId, MESH_SERVICE_UUID, CHAR_ACK)
      disconnect(this.deviceId)
    } catch { /* best effort */ }
    this._state = 'disconnected'
    this._pendingStreams.clear()
  }

  // ── Send ───────────────────────────────────────────────────────────────────

  /**
   * Write a MeshPacket byte array to CHAR_INBOX (destined for this peer)
   * or CHAR_RELAY (epidemic relay), chunked to fit the ATT MTU.
   */
  async sendPacket(raw: Uint8Array, relay = false): Promise<void> {
    if (this._state !== 'connected') throw new Error(`Session not connected: ${this.deviceId}`)

    const charUUID  = relay ? CHAR_RELAY : CHAR_INBOX
    const streamId  = this._nextStreamId()
    const chunks    = chunkBytes(raw, streamId)

    for (const chunk of chunks) {
      await writeCharacteristic(
        this.deviceId,
        MESH_SERVICE_UUID,
        charUUID,
        bytesToHex(chunk),
        'writeWithoutResponse',
      )
    }

    this._resetIdleTimer()
  }

  // ── Inbound write handler (called by BleTransport event listener) ──────────

  /**
   * Called by BleTransport whenever a peripheralWriteRequest arrives for
   * CHAR_INBOX or CHAR_RELAY from this peer (peripheral-side) OR when a
   * characteristicValueChanged event arrives (central-side — peer is peripheral).
   */
  handleInboundFrame(charUUID: string, frame: Uint8Array): void {
    const assembled = feedChunk(this._pendingStreams, `${this.deviceId}:${charUUID}`, frame)
    if (assembled) {
      this._callbacks.onPacketAssembled(this.deviceId, assembled, charUUID)
    }
    this._resetIdleTimer()
  }

  // ── Private ────────────────────────────────────────────────────────────────

  private _nextStreamId(): number {
    this._streamId = (this._streamId + 1) % 256
    return this._streamId
  }

  private _startStaleEviction(): void {
    this._staleEvictInterval = setInterval(() => {
      evictStaleStreams(this._pendingStreams, STREAM_ASSEMBLY_TIMEOUT_MS)
    }, STREAM_ASSEMBLY_TIMEOUT_MS)
  }

  private _resetIdleTimer(): void {
    if (this._idleTimer) clearTimeout(this._idleTimer)
    this._idleTimer = setTimeout(() => {
      // Close idle sessions to free radio resources for other peers
      this.close()
      this._callbacks.onDisconnected(this.deviceId)
    }, GATT_IDLE_CLOSE_MS)
  }

  private _clearTimers(): void {
    if (this._staleEvictInterval) { clearInterval(this._staleEvictInterval); this._staleEvictInterval = null }
    if (this._idleTimer)          { clearTimeout(this._idleTimer);           this._idleTimer          = null }
  }
}