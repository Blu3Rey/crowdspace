/**
 * @file multipeerTransport.ts
 * Apple Multipeer Connectivity adapter for iOS-to-iOS communication.
 *
 * Multipeer Connectivity (MPC) uses a combination of BLE, Wi-Fi Direct, and
 * peer-to-peer Wi-Fi under the hood — it will automatically use the fastest
 * available link. This gives significantly better throughput than raw GATT
 * for iOS-to-iOS transfers (important for larger payloads).
 *
 * Limitation: Android cannot join MPC sessions. For cross-platform
 * communication, BleTransport is always used. MPC only activates when
 * the peer is also an iOS device that has joined the same Multipeer session.
 *
 * Platform: iOS/iPadOS only. On Android this module is a no-op.
 */

import {
    addEventListener,
    sendMultipeerMessage,
    startMultipeerSession,
    stopMultipeerSession,
} from 'munim-bluetooth'
import { Platform } from 'react-native'

import { MULTIPEER_SERVICE_TYPE } from '../core/constants'
import { bytesToHex, hexToBytes } from '../core/encoding'

export interface MultipeerCallbacks {
  onPacket(peerId: string, raw: Uint8Array): void
  onPeerConnected(peerId: string, displayName: string): void
  onPeerDisconnected(peerId: string): void
}

export class MultipeerTransport {
  private _running      = false
  private _cleanupFns:  (() => void)[] = []
  private _callbacks:   MultipeerCallbacks
  private _serviceType: string
  private _displayName: string

  /** True only when running on iOS */
  static readonly isSupported = Platform.OS === 'ios'

  constructor(callbacks: MultipeerCallbacks, opts?: { serviceType?: string; displayName?: string }) {
    this._callbacks   = callbacks
    this._serviceType = opts?.serviceType ?? MULTIPEER_SERVICE_TYPE
    this._displayName = opts?.displayName ?? 'AnonMeshPeer'
  }

  // ─── Lifecycle ──────────────────────────────────────────────────────────────

  start(): void {
    if (!MultipeerTransport.isSupported || this._running) return
    this._running = true

    startMultipeerSession({
      serviceType:           this._serviceType,
      displayName:           this._displayName,
      autoInvite:            true,
      autoAcceptInvitations: true,
      encryptionPreference:  'required',
    })

    this._attachEvents()
  }

  stop(): void {
    if (!this._running) return
    this._running = false

    stopMultipeerSession()
    for (const fn of this._cleanupFns) fn()
    this._cleanupFns = []
  }

  // ─── Outbound ───────────────────────────────────────────────────────────────

  async sendTo(peerId: string, raw: Uint8Array): Promise<void> {
    if (!this._running) throw new Error('MultipeerTransport not running')
    await sendMultipeerMessage(bytesToHex(raw), [peerId], true /* reliable */)
  }

  async broadcast(raw: Uint8Array): Promise<void> {
    if (!this._running) return
    await sendMultipeerMessage(bytesToHex(raw), undefined, true)
  }

  // ─── Private ────────────────────────────────────────────────────────────────

  private _attachEvents(): void {
    const removePeerState = addEventListener('multipeerPeerStateChanged', (peer) => {
      if (peer.state === 'connected') {
        this._callbacks.onPeerConnected(peer.id, peer.displayName ?? '')
      } else if (peer.state === 'notConnected') {
        this._callbacks.onPeerDisconnected(peer.id)
      }
    })

    const removeMessage = addEventListener('multipeerMessageReceived', (ev) => {
      if (!ev.value) return
      try {
        const raw = hexToBytes(ev.value)
        this._callbacks.onPacket(ev.peerId, raw)
      } catch (err) {
        console.warn('[MultipeerTransport] Failed to decode message:', err)
      }
    })

    this._cleanupFns.push(removePeerState, removeMessage)
  }
}