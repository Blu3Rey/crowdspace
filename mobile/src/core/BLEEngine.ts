/**
 * BLEEngine
 *
 * A thin, reliable wrapper over munim-bluetooth that:
 *  - Manages peripheral advertising + GATT service setup (dual-role node).
 *  - Manages central scanning + connection lifecycle with retry/back-off.
 *  - Reads PEER_INFO_CHAR_UUID on connection to identify the remote peer.
 *  - Subscribes to MSG_NOTIFY_CHAR_UUID for incoming notification frames.
 *  - Serialises writes through a per-device queue to prevent fragment
 *    interleaving when multiple callers target the same device concurrently.
 *  - Adapts scan mode to app state (balanced in foreground, lowPower in bg).
 *
 * This module MUST NOT contain business logic. All it knows about is hex
 * strings and BLE operations.
 */

import {
  startAdvertising,
  stopAdvertising,
  setServices,
  updateCharacteristicValue,
  startScan,
  stopScan,
  connect,
  disconnect,
  discoverServices,
  readCharacteristic,
  writeCharacteristic,
  subscribeToCharacteristic,
  readRSSI,
  requestMTU,
  startBackgroundSession,
  stopBackgroundSession,
  isBluetoothEnabled,
  requestBluetoothPermission,
  getCapabilities,
  addEventListener,
  addDeviceFoundListener,
} from 'munim-bluetooth'

import { Platform, AppState } from 'react-native'
import {
  MESH_SERVICE_UUID,
  MSG_WRITE_CHAR_UUID,
  MSG_NOTIFY_CHAR_UUID,
  PEER_INFO_CHAR_UUID,
  CONNECT_TIMEOUT_MS,
  RECONNECT_BASE_DELAY_MS,
  MAX_RECONNECT_DELAY_MS,
  MAX_RECONNECT_ATTEMPTS,
  EPHEMERAL_IDLE_TIMEOUT_MS,
  RSSI_POLL_INTERVAL_MS,
  SCAN_CYCLE_MS,
} from '../constants/ble'
import { MessageProtocol } from './MessageProtocol'
import type { PeerInfoPayload } from '../types/ble'

// --- Types -------------------------------------------------------------------

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

interface ConnectWaiter {
  resolve: () => void
  reject: (e: Error) => void
}

// --- BLEEngine ---------------------------------------------------------------

export class BLEEngine {
  private callbacks: BLEEngineCallbacks
  private selfPeerInfo: PeerInfoPayload
  private connections = new Map<string, ConnectionEntry>()
  private unsubscribers: Array<() => void> = []
  private scanTimer: ReturnType<typeof setInterval> | null = null
  private protocol = new MessageProtocol()
  private capabilities: Awaited<ReturnType<typeof getCapabilities>> | null = null

  /**
   * FIX: Per-device write queue.
   * Each device gets a promise chain that serialises all outbound writes.
   * Concurrent send() calls on the same bleDeviceId wait their turn instead
   * of racing ahead and interleaving fragments on the wire — which would
   * corrupt multi-frame message reassembly on the remote peer.
   */
  private writeQueues = new Map<string, Promise<void>>()

  /**
   * FIX: Promise-based connection waiters.
   * Replaces the previous 50 ms busy-poll loop. Zero CPU overhead while a
   * concurrent connect attempt is in flight; resolved/rejected atomically
   * when connectDevice() settles.
   */
  private pendingConnects = new Map<string, ConnectWaiter[]>()

  /**
   * FIX: Adaptive scan mode.
   * Switches to lowPower when the app goes to background, back to balanced
   * when it returns to foreground. On Android, lowPower uses roughly 10x
   * less radio time than balanced.
   */
  private currentScanMode: 'balanced' | 'lowPower' = 'balanced'
  private appStateSub: ReturnType<typeof AppState.addEventListener> | null = null
  private running = false

  constructor(callbacks: BLEEngineCallbacks, selfPeerInfo: PeerInfoPayload) {
    this.callbacks = callbacks
    this.selfPeerInfo = selfPeerInfo
  }

  // -- Initialisation ---------------------------------------------------------

  async init(): Promise<void> {
    const hasPermission = await requestBluetoothPermission()
    if (!hasPermission) throw new Error('Bluetooth permission denied')

    const enabled = await isBluetoothEnabled()
    if (!enabled) throw new Error('Bluetooth is not enabled')

    this.capabilities = await getCapabilities()
    this.running = true
    this.registerListeners()
    this.setupGattServer()
    this.startAdvertising()
    this.startScanning()
    this.registerAppStateListener()
  }

  // -- GATT Server (Peripheral role) ------------------------------------------

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

  // -- Notify outbound (push to subscribed centrals) --------------------------

  /**
   * Push a hex frame to all centrals subscribed to MSG_NOTIFY_CHAR.
   * Requires no outbound connection — the peripheral role handles it.
   * Used for broadcast-style messages (presence heartbeats, locate replies).
   */
  notifyAll(hexFrame: string): void {
    updateCharacteristicValue(MESH_SERVICE_UUID, MSG_NOTIFY_CHAR_UUID, hexFrame, true)
  }

  // -- Central Scanning -------------------------------------------------------

  private startScanning(): void {
    startScan({
      serviceUUIDs: [MESH_SERVICE_UUID],
      allowDuplicates: false,
      scanMode: this.currentScanMode,
    })
    this.scanTimer = setInterval(() => {
      stopScan()
      startScan({
        serviceUUIDs: [MESH_SERVICE_UUID],
        allowDuplicates: false,
        scanMode: this.currentScanMode,
      })
    }, SCAN_CYCLE_MS)
  }

  // FIX: Update currentScanMode on app state transitions and immediately
  // restart the scan so the new mode takes effect without waiting for the
  // next scheduled cycle.
  private registerAppStateListener(): void {
    this.appStateSub = AppState.addEventListener('change', (nextState) => {
      if (!this.running) return
      const mode = nextState === 'active' ? 'balanced' : 'lowPower'
      if (mode === this.currentScanMode) return
      this.currentScanMode = mode
      stopScan()
      startScan({
        serviceUUIDs: [MESH_SERVICE_UUID],
        allowDuplicates: false,
        scanMode: this.currentScanMode,
      })
    })
  }

  // -- Outbound writes (Central -> Peripheral) --------------------------------

  /**
   * Send one or more hex frames to a specific BLE device.
   * Serialised through a per-device queue: if this device already has writes
   * in flight, this call is chained after them rather than racing ahead.
   */
  async send(bleDeviceId: string, hexFrames: string[]): Promise<void> {
    return this.enqueueWrite(bleDeviceId, async () => {
      await this.ensureConnected(bleDeviceId)
      for (const frame of hexFrames) {
        await this.writeFrame(bleDeviceId, frame)
      }
      this.scheduleIdleDisconnect(bleDeviceId)
    })
  }

  // FIX: Serial per-device write queue via promise chaining.
  // The tail of the queue for a device is stored in writeQueues. Each new
  // task appends to it with .then(() => task(), () => task()) so the next
  // task always starts after the previous one settles — whether it succeeded
  // or failed. The finally() prunes the map entry when the queue drains,
  // preventing a memory leak on long-lived sessions.
  private enqueueWrite(bleDeviceId: string, task: () => Promise<void>): Promise<void> {
    const tail = (this.writeQueues.get(bleDeviceId) ?? Promise.resolve())
      .then(() => task(), () => task())
    this.writeQueues.set(bleDeviceId, tail)
    void tail.finally(() => {
      if (this.writeQueues.get(bleDeviceId) === tail) {
        this.writeQueues.delete(bleDeviceId)
      }
    })
    return tail
  }

  // FIX: writeCharacteristic is now a top-level static import.
  // The original implementation used a dynamic import('munim-bluetooth') on
  // every single frame write, incurring module-resolution overhead on each
  // call and bypassing TypeScript type checking at that site.
  private async writeFrame(bleDeviceId: string, hexFrame: string): Promise<void> {
    try {
      await writeCharacteristic(
        bleDeviceId,
        MESH_SERVICE_UUID,
        MSG_WRITE_CHAR_UUID,
        hexFrame,
        'writeWithoutResponse',
      )
    } catch (err) {
      throw new Error(`GATT write failed: ${(err as Error).message}`)
    }
  }

  private async ensureConnected(bleDeviceId: string): Promise<void> {
    const entry = this.connections.get(bleDeviceId)
    if (entry?.state === 'connected' || entry?.state === 'subscribed') {
      this.resetIdleTimer(bleDeviceId)
      return
    }
    // FIX: If a connect attempt is already in flight from another concurrent
    // send(), subscribe to its result rather than busy-polling every 50 ms.
    if (entry?.state === 'connecting') {
      await this.waitForConnected(bleDeviceId, CONNECT_TIMEOUT_MS)
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

      // Negotiate a larger ATT MTU on Android (iOS negotiates automatically).
      if (Platform.OS === 'android') {
        try { await requestMTU(bleDeviceId, 512) } catch { /* best-effort */ }
      }

      // Read stable peer identity.
      try {
        const infoHex = await readCharacteristic(
          bleDeviceId,
          MESH_SERVICE_UUID,
          PEER_INFO_CHAR_UUID,
        )
        const info = MessageProtocol.decodePeerInfo(infoHex as unknown as string)
        if (info) this.callbacks.onPeerInfoRead(info, bleDeviceId)
      } catch { /* peer info optional */ }

      // Subscribe to inbound notification frames.
      try {
        await subscribeToCharacteristic(bleDeviceId, MESH_SERVICE_UUID, MSG_NOTIFY_CHAR_UUID)
        entry.state = 'subscribed'
      } catch {
        entry.state = 'connected'
      }

      // RSSI polling for distance estimation.
      // Interval bumped from 1500 ms to 3000 ms: adequate for passive
      // distance tracking, meaningfully less radio activity per connection.
      entry.rssiTimer = setInterval(async () => {
        try {
          const rssi = await readRSSI(bleDeviceId)
          this.callbacks.onRssiUpdated(bleDeviceId, rssi)
        } catch { /* device may have already disconnected */ }
      }, RSSI_POLL_INTERVAL_MS)

      this.callbacks.onDeviceConnected(bleDeviceId)
      this.scheduleIdleDisconnect(bleDeviceId)

      // Notify all callers that were waiting on this connection.
      this.resolveConnectWaiters(bleDeviceId)
    } catch (err) {
      const error = err as Error
      if (attempt < MAX_RECONNECT_ATTEMPTS) {
        // FIX: cap delay so high MAX_RECONNECT_ATTEMPTS values can never
        // produce waits in the minutes-to-hours range.
        const delay = Math.min(
          RECONNECT_BASE_DELAY_MS * Math.pow(2, attempt),
          MAX_RECONNECT_DELAY_MS,
        )
        await sleep(delay)
        return this.connectDevice(bleDeviceId, attempt + 1)
      }
      this.connections.delete(bleDeviceId)
      this.rejectConnectWaiters(bleDeviceId, error)
      this.callbacks.onError(error, `connect:${bleDeviceId}`)
      throw error
    }
  }

  // FIX: Promise-based connection waiters — zero CPU usage while waiting.
  // Callers that arrive while a connect is in progress register here and are
  // resolved/rejected atomically when connectDevice() settles, rather than
  // spinning in a 50 ms poll loop for up to 15 seconds.
  private waitForConnected(bleDeviceId: string, timeoutMs: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        // Remove this specific waiter before rejecting so it does not leak.
        const list = this.pendingConnects.get(bleDeviceId)
        if (list) {
          const filtered = list.filter((w) => w.resolve !== onResolve)
          filtered.length
            ? this.pendingConnects.set(bleDeviceId, filtered)
            : this.pendingConnects.delete(bleDeviceId)
        }
        reject(new Error('Connection timeout'))
      }, timeoutMs)

      const onResolve = () => { clearTimeout(timer); resolve() }
      const onReject  = (e: Error) => { clearTimeout(timer); reject(e) }

      const list = this.pendingConnects.get(bleDeviceId) ?? []
      list.push({ resolve: onResolve, reject: onReject })
      this.pendingConnects.set(bleDeviceId, list)
    })
  }

  private resolveConnectWaiters(bleDeviceId: string): void {
    const list = this.pendingConnects.get(bleDeviceId) ?? []
    this.pendingConnects.delete(bleDeviceId)
    for (const w of list) w.resolve()
  }

  private rejectConnectWaiters(bleDeviceId: string, error: Error): void {
    const list = this.pendingConnects.get(bleDeviceId) ?? []
    this.pendingConnects.delete(bleDeviceId)
    for (const w of list) w.reject(error)
  }

  // -- Idle / Ephemeral -------------------------------------------------------

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

  // -- Event Listeners --------------------------------------------------------

  private registerListeners(): void {
    // Central: inbound notification frames from peripherals we subscribed to.
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

    // Peripheral: inbound write frames from centrals.
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

    // Disconnection: clean up timers and unblock any pending waiters so they
    // fail fast rather than waiting out the full timeout.
    this.unsubscribers.push(
      addEventListener('deviceDisconnected', (event: any) => {
        const entry = this.connections.get(event.deviceId)
        if (entry) {
          if (entry.rssiTimer) clearInterval(entry.rssiTimer)
          if (entry.idleTimer) clearTimeout(entry.idleTimer)
          this.connections.delete(event.deviceId)
          this.rejectConnectWaiters(
            event.deviceId,
            new Error('Device disconnected unexpectedly'),
          )
        }
        this.callbacks.onDeviceDisconnected(event.deviceId)
      }),
    )

    // Scan results — delegated to TransportManager via onDeviceFound().
    this.unsubscribers.push(
      addDeviceFoundListener((device: any) => {
        ;(this as any)._onDeviceFound?.(device)
      }),
    )
  }

  // -- Background Session -----------------------------------------------------

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

  // -- Teardown ---------------------------------------------------------------

  async destroy(): Promise<void> {
    this.running = false
    this.appStateSub?.remove()
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
    this.writeQueues.clear()
    this.pendingConnects.clear()
    this.protocol.flush()
  }

  // -- Helpers ----------------------------------------------------------------

  get hasL2CAP(): boolean {
    return !!(this.capabilities as any)?.l2capChannels
  }

  /** Expose raw scan events for TransportManager to hook into. */
  onDeviceFound(cb: (device: any) => void): void {
    ;(this as any)._onDeviceFound = cb
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((res) => setTimeout(res, ms))
}