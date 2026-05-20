/**
 * @file radioOrchestrator.ts
 * Central / Peripheral dual-role time-slicing state machine.
 *
 * Design choice 3 (Central/Peripheral Dual-Role Orchestration):
 *
 *   The BLE radio cannot simultaneously scan at full power and advertise at
 *   full power. We time-slice the radio into alternating phases:
 *
 *     ┌─ ADVERTISING ─ advertisingPhaseMs ─┐
 *     │  Peripheral mode: broadcasting      │
 *     │  our rotating service UUID/name     │
 *     └──────────────────────────────────── ┘
 *               ↓
 *     ┌─ SCANNING ── scanningPhaseMs ───────┐
 *     │  Central mode: scanning for peers   │
 *     │  filtered by MESH_SERVICE_UUID      │
 *     └──────────────────────────────────── ┘
 *               ↓  (repeat)
 *
 *   Tie-breaker (Design choice 3, Race Condition Mitigation):
 *   When two devices detect each other and both want to connect, the device
 *   whose senderTokenHash is lexicographically HIGHER acts as Central and
 *   initiates the connection. The other device backs off and remains in
 *   Peripheral mode until connected to. See crypto.shouldActAsCentral().
 *
 * The orchestrator emits 'peer:discovered' events for each found device.
 * MeshEngine decides whether to connect based on tie-breaker and pool limits.
 */

import {
    addDeviceFoundListener,
    addEventListener,
    startScan,
    stopScan,
} from 'munim-bluetooth'

import {
    DEFAULT_ADVERTISING_PHASE_MS,
    DEFAULT_SCANNING_PHASE_MS,
    MESH_SERVICE_UUID,
} from '../core/constants'
import { EventBus } from '../core/eventBus'
import { MeshEventMap, NearbyPeer, RadioPhase, RadioStatus } from '../core/types'

export interface RadioOrchestratorOptions {
  advertisingPhaseMs?: number
  scanningPhaseMs?:   number
  bus:                EventBus<MeshEventMap>
  /** Called when a new peer is discovered during scan phase */
  onPeerFound(peer: NearbyPeer): void
  /** Called when the radio phase changes */
  onPhaseChange?(status: RadioStatus): void
}

export class RadioOrchestrator {
  private _running          = false
  private _phase: RadioPhase = 'idle'
  private _phaseTimer: ReturnType<typeof setTimeout> | null = null
  private _removeDeviceFound: (() => void) | null = null
  private _removeScanFailed:  (() => void) | null = null

  private readonly _advertisingPhaseMs: number
  private readonly _scanningPhaseMs: number
  private readonly _onPeerFound: (peer: NearbyPeer) => void
  private readonly _onPhaseChange?: (status: RadioStatus) => void

  constructor(opts: RadioOrchestratorOptions) {
    this._advertisingPhaseMs = opts.advertisingPhaseMs ?? DEFAULT_ADVERTISING_PHASE_MS
    this._scanningPhaseMs    = opts.scanningPhaseMs    ?? DEFAULT_SCANNING_PHASE_MS
    this._onPeerFound        = opts.onPeerFound
    this._onPhaseChange      = opts.onPhaseChange
  }

  // ─── Lifecycle ──────────────────────────────────────────────────────────────

  start(): void {
    if (this._running) return
    this._running = true
    this._enterAdvertisingPhase()
  }

  stop(): void {
    if (!this._running) return
    this._running = false
    this._clearTimer()
    this._stopScan()
    this._phase = 'idle'
    this._emitPhase()
  }

  // ─── Phase transitions ───────────────────────────────────────────────────────

  /**
   * Allow an external caller (e.g. BleTransport session opener) to temporarily
   * pause the radio loop while a GATT connection is being established.
   * This prevents scanning from interfering with the connection procedure.
   */
  pauseForConnection(): void {
    this._clearTimer()
    this._stopScan()
    this._setPhase('connecting')
  }

  /** Resume the normal advertising → scanning loop after connection is done */
  resumeFromConnection(): void {
    if (!this._running) return
    this._enterAdvertisingPhase()
  }

  // ─── Private: phase loop ─────────────────────────────────────────────────────

  private _enterAdvertisingPhase(): void {
    this._stopScan()
    this._setPhase('advertising')
    // Advertising is controlled by BleTransport (startAdvertising is called once at start).
    // We just time-slice here — munim-bluetooth keeps advertising running continuously;
    // we simply stop scanning during this phase.
    this._phaseTimer = setTimeout(() => this._enterScanningPhase(), this._advertisingPhaseMs)
  }

  private _enterScanningPhase(): void {
    this._setPhase('scanning')
    this._startScan()
    this._phaseTimer = setTimeout(() => this._enterAdvertisingPhase(), this._scanningPhaseMs)
  }

  private _setPhase(phase: RadioPhase): void {
    this._phase = phase
    this._emitPhase()
  }

  private _emitPhase(): void {
    const status: RadioStatus = { phase: this._phase, phaseStartedMs: Date.now() }
    this._onPhaseChange?.(status)
  }

  // ─── Private: scan control ───────────────────────────────────────────────────

  private _startScan(): void {
    this._removeDeviceFound = addDeviceFoundListener((device) => {
      if (!device.id) return

      // Only surface devices that advertise our mesh service UUID
      const advServiceUUIDs = (device.serviceUUIDs ?? []).map(u => u.toLowerCase())
      if (!advServiceUUIDs.includes(MESH_SERVICE_UUID.toLowerCase())) return

      // Extract a token hash hint from manufacturer data if available (Android)
      // or derive from the device ID otherwise (iOS — no arbitrary adv data)
      const tokenHint = this._extractTokenHash(device)

      const peer: NearbyPeer = {
        deviceId:       device.id,
        tokenHash:      tokenHint,
        transport:      'ble-gatt',
        discoveredAtMs: Date.now(),
        rssi:           device.rssi ?? undefined,
      }

      this._onPeerFound(peer)
    })

    this._removeScanFailed = addEventListener('scanFailed', (ev) => {
      console.warn('[RadioOrchestrator] Scan failed:', ev.message)
    })

    startScan({
      serviceUUIDs:    [MESH_SERVICE_UUID],
      allowDuplicates: false,
      scanMode:        'balanced',
    })
  }

  private _stopScan(): void {
    try { stopScan() } catch { /* ignore if not scanning */ }
    if (this._removeDeviceFound) { this._removeDeviceFound(); this._removeDeviceFound = null }
    if (this._removeScanFailed)  { this._removeScanFailed();  this._removeScanFailed  = null }
  }

  private _clearTimer(): void {
    if (this._phaseTimer) { clearTimeout(this._phaseTimer); this._phaseTimer = null }
  }

  /**
   * Attempt to extract a token hash from the advertisement payload.
   *
   * Android: manufacturer data field may carry a token hash hint.
   *          We encode it as the first 16 hex chars of manufacturerData.
   * iOS:     No arbitrary adv payload is available from public APIs.
   *          Return an empty string — the token hash will be read from
   *          CHAR_ANNOUNCE on connection.
   *
   * Design choice 2: The token hash in the advertisement payload lets known
   * contacts recognise each other without connecting. On iOS, we must
   * connect first and read CHAR_ANNOUNCE to get the token hash.
   */
  private _extractTokenHash(device: {
    manufacturerData?: string
    serviceData?:      Record<string, string>
  }): string {
    // Android: first 16 hex chars of manufacturer data = 8-byte token hash
    if (device.manufacturerData && device.manufacturerData.length >= 16) {
      return device.manufacturerData.slice(0, 16)
    }
    return ''  // Will be resolved on GATT connection via CHAR_ANNOUNCE
  }
}