/**
 * BLEEngine
 *
 * A thin, reliable wrapper over munim-bluetooth that:
 *  • Manages peripheral advertising + GATT service setup (dual-role node).
 *  • Manages central scanning + connection lifecycle with retry/back-off.
 *  • Reads PEER_INFO_CHAR_UUID on connection to identify the remote peer.
 *  • Subscribes to MSG_NOTIFY_CHAR_UUID for incoming notification frames.
 *  • Exposes a clean `send(bleDeviceId, hexFrames[])` that queues writes
 *    and honours ephemeral connection semantics.
 *
 * This module MUST NOT contain business logic. All it knows about is hex
 * strings and BLE operations.
 */

import {
  addDeviceFoundListener,
  addEventListener,
  connect,
  disconnect,
  discoverServices,
  getCapabilities,
  isBluetoothEnabled,
  readCharacteristic,
  readRSSI,
  requestBluetoothPermission,
  requestMTU,
  setServices,
  startAdvertising,
  startBackgroundSession,
  startScan,
  stopAdvertising,
  stopBackgroundSession,
  stopScan,
  subscribeToCharacteristic,
  updateCharacteristicValue,
} from 'munim-bluetooth'

import { Platform } from 'react-native'
import {
  CONNECT_TIMEOUT_MS,
  EPHEMERAL_IDLE_TIMEOUT_MS,
  MAX_RECONNECT_ATTEMPTS,
  MESH_SERVICE_UUID,
  MSG_NOTIFY_CHAR_UUID,
  MSG_WRITE_CHAR_UUID,
  PEER_INFO_CHAR_UUID,
  RECONNECT_BASE_DELAY_MS,
  RSSI_POLL_INTERVAL_MS,
  SCAN_CYCLE_MS,
} from '../constants/ble'
import type { PeerInfoPayload } from '../types/ble'
import { MessageProtocol } from './MessageProtocol'

// ─── Types ────────────────────────────────────────────────────────────────────

export interface BLEEngineCallbacks {
  onFrameReceived: (hexFrame: string, bleDeviceId: string) => void
  onPeerInfoRead: (info: PeerInfoPayload, bleDeviceId: string) => void
  onDeviceConnected: (bleDeviceId: string) => void
  onDeviceDisconnected: (bleDeviceId: string) => void
  onRssiUpdated: (bleDeviceId: string, rssi: number) => void
  onError: (err: Error, context: string) => void
}

interface ConnectionEntry {
  bleDeviceId: string
  state: 'connecting' | 'connected' | 'subscribed'
  reconnectAttempts: number
  idleTimer: ReturnType<typeof setTimeout> | null
  rssiTimer: ReturnType<typeof setInterval> | null
}

// ─── BLEEngine ────────────────────────────────────────────────────────────────

export class BLEEngine {
  private callbacks: BLEEngineCallbacks
  private selfPeerInfo: PeerInfoPayload
  private connections = new Map<string, ConnectionEntry>()
  private unsubscribers: Array<() => void> = []
  private scanTimer: ReturnType<typeof setInterval> | null = null
  private protocol = new MessageProtocol()
  private capabilities: Awaited<ReturnType<typeof getCapabilities>> | null = null

  constructor(callbacks: BLEEngineCallbacks, selfPeerInfo: PeerInfoPayload) {
    this.callbacks = callbacks
    this.selfPeerInfo = selfPeerInfo
  }

  // ── Initialisation ────────────────────────────────────────────────────────

  async init(): Promise<void> {
    const hasPermission = await requestBluetoothPermission()
    if (!hasPermission) throw new Error('Bluetooth permission denied')

    const enabled = await isBluetoothEnabled()
    if (!enabled) throw new Error('Bluetooth is not enabled')

    this.capabilities = await getCapabilities()
    this.registerListeners()
    this.setupGattServer()
    this.startAdvertising()
    this.startScanning()
  }

  // ── GATT Server (Peripheral role) ─────────────────────────────────────────

  private setupGattServer(): void {
    const peerInfoHex = MessageProtocol.encodePeerInfo(this.selfPeerInfo)
    setServices([
      {
        uuid: MESH_SERVICE_UUID,
        characteristics: [
          {
            uuid: MSG_WRITE_CHAR_UUID,
            properties: ['write', 'writeWithoutResponse'],
            value: '00',
          },
          {
            uuid: MSG_NOTIFY_CHAR_UUID,
            properties: ['notify', 'read'],
            value: '00',
          },
          {
            uuid: PEER_INFO_CHAR_UUID,
            properties: ['read'],
            value: peerInfoHex,
          },
        ],
      },
    ])
  }

  private startAdvertising(): void {
    startAdvertising({
      serviceUUIDs: [MESH_SERVICE_UUID],
      localName: this.selfPeerInfo.name,
    })
  }

  // ── Notify outbound (push to subscribed centrals) ─────────────────────────

  /**
   * Push a hex frame to all centrals currently subscribed to MSG_NOTIFY_CHAR.
   * Used by the TransportManager to push reply/notification frames.
   */
  notifyAll(hexFrame: string): void {
    updateCharacteristicValue(MESH_SERVICE_UUID, MSG_NOTIFY_CHAR_UUID, hexFrame, true)
  }

  // ── Central Scanning ──────────────────────────────────────────────────────

  private startScanning(): void {
    startScan({
      serviceUUIDs: [MESH_SERVICE_UUID],
      allowDuplicates: false,
      scanMode: 'balanced',
    })
    this.scanTimer = setInterval(() => {
      stopScan()
      startScan({
        serviceUUIDs: [MESH_SERVICE_UUID],
        allowDuplicates: false,
        scanMode: 'balanced',
      })
    }, SCAN_CYCLE_MS)
  }

  // ── Outbound writes (Central → Peripheral) ────────────────────────────────

  /**
   * Send one or more hex frames to a specific BLE device.
   * Establishes an ephemeral connection if not already connected.
   * Each frame is written sequentially to MSG_WRITE_CHAR_UUID.
   */
  async send(bleDeviceId: string, hexFrames: string[]): Promise<void> {
    await this.ensureConnected(bleDeviceId)
    for (const frame of hexFrames) {
      await this.writeFrame(bleDeviceId, frame)
    }
    this.scheduleIdleDisconnect(bleDeviceId)
  }

  private async writeFrame(bleDeviceId: string, hexFrame: string): Promise<void> {
    try {
      // writeWithoutResponse is faster but less reliable; for important messages
      // the caller should use 'write' — we use WoR by default for throughput.
      await (import('munim-bluetooth') as any).then((m: any) =>
        m.writeCharacteristic(bleDeviceId, MESH_SERVICE_UUID, MSG_WRITE_CHAR_UUID, hexFrame, 'writeWithoutResponse'),
      )
    } catch (err) {
      throw new Error(`GATT write failed: ${(err as Error).message}`)
    }
  }

  private async ensureConnected(bleDeviceId: string): Promise<void> {
    const entry = this.connections.get(bleDeviceId)
    if (entry && (entry.state === 'connected' || entry.state === 'subscribed')) {
      this.resetIdleTimer(bleDeviceId)
      return
    }
    if (entry?.state === 'connecting') {
      // Wait for existing connect attempt (poll briefly)
      await this.waitForState(bleDeviceId, 'connected', CONNECT_TIMEOUT_MS)
      return
    }
    await this.connectDevice(bleDeviceId)
  }

  private async connectDevice(bleDeviceId: string, attempt = 0): Promise<void> {
    const entry: ConnectionEntry = {
      bleDeviceId,
      state: 'connecting',
      reconnectAttempts: attempt,
      idleTimer: null,
      rssiTimer: null,
    }
    this.connections.set(bleDeviceId, entry)

    try {
      await connect(bleDeviceId)
      await discoverServices(bleDeviceId)

      // Negotiate MTU on Android for larger writes (iOS handles internally).
      if (Platform.OS === 'android') {
        try { await requestMTU(bleDeviceId, 512) } catch { /* best-effort */ }
      }

      // Read peer info.
      try {
        const infoHex = await readCharacteristic(bleDeviceId, MESH_SERVICE_UUID, PEER_INFO_CHAR_UUID)
        const info = MessageProtocol.decodePeerInfo(infoHex as unknown as string)
        if (info) this.callbacks.onPeerInfoRead(info, bleDeviceId)
      } catch { /* peer info is optional */ }

      // Subscribe to notifications.
      try {
        await subscribeToCharacteristic(bleDeviceId, MESH_SERVICE_UUID, MSG_NOTIFY_CHAR_UUID)
        entry.state = 'subscribed'
      } catch {
        entry.state = 'connected'
      }

      // Start RSSI polling.
      entry.rssiTimer = setInterval(async () => {
        try {
          const rssi = await readRSSI(bleDeviceId)
          this.callbacks.onRssiUpdated(bleDeviceId, rssi)
        } catch { /* device may have disconnected */ }
      }, RSSI_POLL_INTERVAL_MS)

      this.callbacks.onDeviceConnected(bleDeviceId)
      this.scheduleIdleDisconnect(bleDeviceId)
    } catch (err) {
      const error = err as Error
      if (attempt < MAX_RECONNECT_ATTEMPTS) {
        const delay = RECONNECT_BASE_DELAY_MS * Math.pow(2, attempt)
        await sleep(delay)
        return this.connectDevice(bleDeviceId, attempt + 1)
      }
      this.connections.delete(bleDeviceId)
      this.callbacks.onError(error, `connect:${bleDeviceId}`)
      throw error
    }
  }

  private async waitForState(
    bleDeviceId: string,
    targetState: ConnectionEntry['state'],
    timeoutMs: number,
  ): Promise<void> {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
      const e = this.connections.get(bleDeviceId)
      if (!e) throw new Error('Connection lost while waiting')
      if (e.state === targetState || e.state === 'subscribed') return
      await sleep(50)
    }
    throw new Error('Connection timeout')
  }

  // ── Idle / Ephemeral ──────────────────────────────────────────────────────

  private scheduleIdleDisconnect(bleDeviceId: string): void {
    this.resetIdleTimer(bleDeviceId)
    const entry = this.connections.get(bleDeviceId)!
    entry.idleTimer = setTimeout(async () => {
      await this.disconnectDevice(bleDeviceId)
    }, EPHEMERAL_IDLE_TIMEOUT_MS)
  }

  private resetIdleTimer(bleDeviceId: string): void {
    const entry = this.connections.get(bleDeviceId)
    if (!entry?.idleTimer) return
    clearTimeout(entry.idleTimer)
    entry.idleTimer = null
  }

  async disconnectDevice(bleDeviceId: string): Promise<void> {
    const entry = this.connections.get(bleDeviceId)
    if (entry?.idleTimer) clearTimeout(entry.idleTimer)
    if (entry?.rssiTimer) clearInterval(entry.rssiTimer)
    this.connections.delete(bleDeviceId)
    try { await disconnect(bleDeviceId) } catch { /* already disconnected */ }
  }

  // ── Event Listeners ───────────────────────────────────────────────────────

  private registerListeners(): void {
    // Central: inbound notification frames.
    this.unsubscribers.push(
      addEventListener('characteristicValueChanged', (event: any) => {
        if (
          event.serviceUUID?.toLowerCase() === MESH_SERVICE_UUID.toLowerCase() &&
          event.characteristicUUID?.toLowerCase() === MSG_NOTIFY_CHAR_UUID.toLowerCase()
        ) {
          this.callbacks.onFrameReceived(event.value, event.deviceId)
        }
      }),
    )

    // Peripheral: inbound write frames.
    this.unsubscribers.push(
      addEventListener('peripheralWriteRequest', (event: any) => {
        if (
          event.serviceUUID?.toLowerCase() === MESH_SERVICE_UUID.toLowerCase() &&
          event.characteristicUUID?.toLowerCase() === MSG_WRITE_CHAR_UUID.toLowerCase()
        ) {
          this.callbacks.onFrameReceived(event.value, event.centralId)
        }
      }),
    )

    // Disconnection events.
    this.unsubscribers.push(
      addEventListener('deviceDisconnected', (event: any) => {
        const entry = this.connections.get(event.deviceId)
        if (entry) {
          if (entry.rssiTimer) clearInterval(entry.rssiTimer)
          if (entry.idleTimer) clearTimeout(entry.idleTimer)
          this.connections.delete(event.deviceId)
        }
        this.callbacks.onDeviceDisconnected(event.deviceId)
      }),
    )

    // Scan results — delegate to caller via addDeviceFoundListener.
    this.unsubscribers.push(
      addDeviceFoundListener((device: any) => {
        // Surfaces raw scan results; PeerRegistry is updated by TransportManager.
        ;(this as any)._onDeviceFound?.(device)
      }),
    )
  }

  // ── Background Session ────────────────────────────────────────────────────

  startBackground(params: {
    androidNotificationTitle?: string
    androidNotificationText?: string
  }): void {
    startBackgroundSession({
      serviceUUIDs: [MESH_SERVICE_UUID],
      localName: this.selfPeerInfo.name,
      scanMode: 'lowPower',
      androidNotificationTitle: params.androidNotificationTitle ?? 'Nearby messaging active',
      androidNotificationText: params.androidNotificationText ?? 'Keeping Bluetooth alive',
    })
  }

  stopBackground(): void {
    stopBackgroundSession()
  }

  // ── Teardown ──────────────────────────────────────────────────────────────

  async destroy(): Promise<void> {
    if (this.scanTimer) clearInterval(this.scanTimer)
    stopScan()
    stopAdvertising()
    for (const unsub of this.unsubscribers) unsub()
    for (const entry of this.connections.values()) {
      if (entry.idleTimer) clearTimeout(entry.idleTimer)
      if (entry.rssiTimer) clearInterval(entry.rssiTimer)
      try { await disconnect(entry.bleDeviceId) } catch { /* ignore */ }
    }
    this.connections.clear()
    this.protocol.flush()
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  get hasL2CAP(): boolean {
    return !!(this.capabilities as any)?.l2capChannels
  }

  /** Expose raw scan event for TransportManager to hook. */
  onDeviceFound(cb: (device: any) => void): void {
    ;(this as any)._onDeviceFound = cb
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((res) => setTimeout(res, ms))
}