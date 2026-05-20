/**
 * @file MeshEngine.ts
 * Top-level orchestrator and public API surface of the Anon Mesh core.
 *
 * MeshEngine wires together:
 *   - BleTransport + MultipeerTransport  (radio I/O)
 *   - RadioOrchestrator                  (Central/Peripheral time-slicing)
 *   - PeerRegistry                       (contact + nearby peer state)
 *   - Router                             (epidemic relay decisions)
 *   - PacketCodec                        (build/decode packets)
 *   - CausalMessageStore                 (per-thread DAG ordering)
 *
 * Feature layers (DM, group chat, device locating, etc.) sit ABOVE this engine
 * and communicate with it through the typed MeshEventMap and the send*() methods.
 * They never import munim-bluetooth directly.
 *
 * Usage:
 *   const engine = new MeshEngine(keyPair, options)
 *   await engine.init()          // permissions + BT check
 *   engine.start()               // starts radio + advertising
 *   engine.on('message', msg => { ... })
 *   await engine.sendMessage(recipientId, ContentType.TEXT, utf8('Hello'))
 *   engine.stop()
 */

import {
    isBluetoothEnabled,
    requestBluetoothPermission,
    startBackgroundSession,
    stopBackgroundSession,
} from 'munim-bluetooth'
import { Platform } from 'react-native'

import {
    DEFAULT_ADVERTISING_PHASE_MS,
    DEFAULT_SCANNING_PHASE_MS,
    DEFAULT_TTL,
    MESH_SERVICE_UUID,
    MULTIPEER_SERVICE_TYPE,
    TOKEN_ROTATION_INTERVAL_MS,
} from './core/constants'
import {
    computeFingerprint,
    deriveSharedRootKey,
    generateIdentityKeyPair,
    hashToFingerprint,
    shouldActAsCentral,
} from './core/crypto'
import { bytesToHex, hexToBytes, packAnnounce, packPacket } from './core/encoding'
import { EventBus } from './core/eventBus'
import {
    Contact,
    ContentType,
    KeyPair,
    MeshEngineOptions,
    MeshEventMap,
    MeshMessage,
    NearbyPeer
} from './core/types'
import {
    CausalMessageStore,
    buildAckPacket,
    buildDataPacket,
    buildHandshakePacket,
    handleInboundRaw,
} from './messaging/packetCodec'
import { RadioOrchestrator } from './radio/radioOrchestrator'
import { PeerRegistry } from './routing/peerRegistry'
import { Router } from './routing/router'
import { BleTransport } from './transport/bleTransport'
import { MultipeerTransport } from './transport/multipeerTransport'

export { generateIdentityKeyPair }
export type { Contact, ContentType, KeyPair, MeshEventMap, MeshMessage }

export class MeshEngine {
  // ── Configuration ───────────────────────────────────────────────────────────
  private readonly _opts: Required<MeshEngineOptions>

  // ── Modules ─────────────────────────────────────────────────────────────────
  private readonly _bus:            EventBus<MeshEventMap>
  private readonly _peerRegistry:   PeerRegistry
  private readonly _router:         Router
  private readonly _bleTransport:   BleTransport
  private readonly _radioOrch:      RadioOrchestrator
  private readonly _multipeer:      MultipeerTransport | null

  // ── Causal stores per conversation ──────────────────────────────────────────
  /** Key: conversationId (recipientId for DMs, groupId for groups) */
  private readonly _causalStores = new Map<string, CausalMessageStore>()

  // ── Token rotation ──────────────────────────────────────────────────────────
  private _tokenRotationTimer: ReturnType<typeof setInterval> | null = null

  // ── State ────────────────────────────────────────────────────────────────────
  private _running = false

  constructor(identityKeyPair: KeyPair, opts: MeshEngineOptions = {}) {
    this._opts = {
      defaultTTL:              opts.defaultTTL              ?? DEFAULT_TTL,
      sprayFactor:             opts.sprayFactor             ?? 4,
      tokenRotationIntervalMs: opts.tokenRotationIntervalMs ?? TOKEN_ROTATION_INTERVAL_MS,
      advertisingPhaseMs:      opts.advertisingPhaseMs      ?? DEFAULT_ADVERTISING_PHASE_MS,
      scanningPhaseMs:         opts.scanningPhaseMs         ?? DEFAULT_SCANNING_PHASE_MS,
      enableBackground:        opts.enableBackground        ?? false,
      androidNotificationText: opts.androidNotificationText ?? 'Mesh network active',
      enableMultipeer:         opts.enableMultipeer         ?? (Platform.OS === 'ios'),
      multipeerServiceType:    opts.multipeerServiceType    ?? MULTIPEER_SERVICE_TYPE,
    }

    this._bus          = new EventBus<MeshEventMap>()
    this._peerRegistry = new PeerRegistry(identityKeyPair)
    this._router       = new Router({
      peerRegistry: this._peerRegistry,
      defaultTTL:   this._opts.defaultTTL,
      sprayFactor:  this._opts.sprayFactor,
    })

    // ── BLE Transport ──────────────────────────────────────────────────────────
    this._bleTransport = new BleTransport({
      onPacket:          this._handleBlePacket.bind(this),
      onPeerConnected:   this._handlePeerConnected.bind(this),
      onPeerDisconnected: this._handlePeerDisconnected.bind(this),
      onAck:             this._handleAck.bind(this),
    })

    // ── Radio Orchestrator ─────────────────────────────────────────────────────
    this._radioOrch = new RadioOrchestrator({
      advertisingPhaseMs: this._opts.advertisingPhaseMs,
      scanningPhaseMs:    this._opts.scanningPhaseMs,
      bus:                this._bus,
      onPeerFound:        this._handlePeerFound.bind(this),
      onPhaseChange:      (status) => this._bus.emit('radio:phase', status),
    })

    // ── Apple Multipeer ────────────────────────────────────────────────────────
    if (this._opts.enableMultipeer && MultipeerTransport.isSupported) {
      this._multipeer = new MultipeerTransport(
        {
          onPacket:           this._handleMultipeerPacket.bind(this),
          onPeerConnected:    (id, name) => this._bus.emit('peer:connected', { deviceId: id, transport: 'multipeer' }),
          onPeerDisconnected: (id)       => this._bus.emit('peer:disconnected', { deviceId: id }),
        },
        {
          serviceType: this._opts.multipeerServiceType,
          displayName: bytesToHex(hashToFingerprint(identityKeyPair.publicKey)).slice(0, 8),
        }
      )
    } else {
      this._multipeer = null
    }
  }

  // ─── Initialisation ──────────────────────────────────────────────────────────

  /**
   * Check Bluetooth availability and permissions.
   * Must be called before start().
   */
  async init(): Promise<{ ok: boolean; reason?: string }> {
    const hasPermission = await requestBluetoothPermission()
    if (!hasPermission) return { ok: false, reason: 'PERMISSION_DENIED' }

    const enabled = await isBluetoothEnabled()
    if (!enabled) return { ok: false, reason: 'BLE_UNAVAILABLE' }

    return { ok: true }
  }

  // ─── Lifecycle ───────────────────────────────────────────────────────────────

  start(): void {
    if (this._running) return
    this._running = true

    this._router.start()

    // Build initial ANNOUNCE payload
    const announce = this._buildAnnounce()

    // Start BLE transport (sets GATT services, starts advertising)
    this._bleTransport.start({
      announcePayload: announce,
      serviceUUIDs:    [MESH_SERVICE_UUID],
    })

    // Start radio time-slicing
    this._radioOrch.start()

    // Start Multipeer if available
    this._multipeer?.start()

    // Start background session if requested
    if (this._opts.enableBackground) {
      startBackgroundSession({
        serviceUUIDs:            [MESH_SERVICE_UUID],
        scanMode:                'balanced',
        androidNotificationTitle: 'Anon Mesh',
        androidNotificationText:  this._opts.androidNotificationText,
      })
    }

    // Schedule token rotation
    this._tokenRotationTimer = setInterval(
      () => this._rotateToken(),
      this._opts.tokenRotationIntervalMs,
    )
  }

  stop(): void {
    if (!this._running) return
    this._running = false

    if (this._tokenRotationTimer) {
      clearInterval(this._tokenRotationTimer)
      this._tokenRotationTimer = null
    }

    this._router.stop()
    this._radioOrch.stop()
    this._bleTransport.stop()
    this._multipeer?.stop()

    if (this._opts.enableBackground) stopBackgroundSession()
  }

  // ─── Public API: event subscriptions ────────────────────────────────────────

  on<K extends keyof MeshEventMap>(event: K, listener: (data: MeshEventMap[K]) => void): () => void {
    return this._bus.on(event, listener)
  }

  off<K extends keyof MeshEventMap>(event: K, listener: (data: MeshEventMap[K]) => void): void {
    this._bus.off(event, listener)
  }

  // ─── Public API: messaging ───────────────────────────────────────────────────

  /**
   * Send an encrypted message to a known contact.
   * Throws if the contact is not found or encryption fails.
   *
   * @param recipientId   Contact.id (stable fingerprint)
   * @param contentType   ContentType enum value
   * @param content       Raw content bytes
   * @param conversationId  Optional: group ID for group messages (defaults to recipientId)
   */
  async sendMessage(
    recipientId:    string,
    contentType:    ContentType,
    content:        Uint8Array,
    conversationId?: string,
  ): Promise<string> {
    const convId   = conversationId ?? recipientId
    const store    = this._getOrCreateStore(convId)
    const parentIds = store.getFrontierIds()

    const { packet, raw } = buildDataPacket({
      recipientId,
      contentType,
      content,
      parentIds,
      ttl:          this._opts.defaultTTL,
      peerRegistry: this._peerRegistry,
    })

    this._router.markSent(packet.header.packetId)

    // Deliver via all connected peers (they will relay if needed)
    const connectedPeers = this._bleTransport.getConnectedPeerIds()
    let sent = false

    for (const peerId of connectedPeers) {
      try {
        await this._bleTransport.sendTo(peerId, raw, false)
        sent = true
        break  // Sent to at least one — router will handle spray-and-wait
      } catch (err) {
        console.warn(`[MeshEngine] Failed to send to ${peerId}:`, err)
      }
    }

    if (!sent && connectedPeers.length === 0) {
      this._bus.emit('error', {
        code:    'SEND_FAILED',
        message: 'No connected peers available for delivery',
      })
    }

    return packet.header.packetId
  }

  // ─── Public API: contacts ────────────────────────────────────────────────────

  /**
   * Add a contact from a QR code or other out-of-band exchange.
   * Derives the shared root key via X25519 DH.
   *
   * @param theirPublicKey  Their identity public key (32 bytes)
   * @param alias           Optional display name
   */
  addContactFromPublicKey(theirPublicKey: Uint8Array, alias?: string): Contact {
    const sharedRootKey = deriveSharedRootKey(
      this._peerRegistry.getOwnSecretKey(),
      theirPublicKey,
    )
    const contact: Contact = {
      id:               computeFingerprint(theirPublicKey),
      identityPublicKey: theirPublicKey,
      sharedRootKey,
      alias,
      addedAtMs:        Date.now(),
    }
    this._peerRegistry.addContact(contact)
    return contact
  }

  removeContact(contactId: string): void {
    this._peerRegistry.removeContact(contactId)
  }

  getContacts(): Contact[] {
    return this._peerRegistry.getAllContacts()
  }

  /** Own identity public key — share this (e.g. via QR) to let others add you */
  getOwnPublicKey(): Uint8Array {
    return this._peerRegistry.getOwnPublicKey()
  }

  getOwnFingerprint(): string {
    return this._peerRegistry.getOwnFingerprint()
  }

  // ─── Public API: causal message history ─────────────────────────────────────

  /**
   * Get all messages in a conversation in causally-consistent order.
   * Safe to call at any time; the DAG merges new arrivals automatically.
   */
  getMessages(conversationId: string): MeshMessage[] {
    return this._getOrCreateStore(conversationId).getSorted()
  }

  // ─── Private: inbound handlers ───────────────────────────────────────────────

  private _handleBlePacket(peerId: string, raw: Uint8Array, isRelay: boolean): void {
    const ownToken  = this._peerRegistry.getOwnToken()
    const result    = handleInboundRaw(raw, peerId, this._peerRegistry, ownToken.tokenHash)

    switch (result.type) {
      case 'message':
        if (result.message) this._deliverMessage(result.message, peerId)
        if (result.packet) {
          // Send an ACK back via BLE
          const { raw: ackRaw } = buildAckPacket(
            result.packet.header.packetId,
            this._peerRegistry,
            result.packet.header.senderTokenHash,
          )
          this._bleTransport.notifyAck(result.packet.header.packetId)
        }
        break

      case 'relay':
        if (result.packet) this._relayPacket(result.packet)
        break

      case 'handshake':
        if (result.remotePublicKey) {
          this._handleHandshake(result.remotePublicKey, result.remoteFingerprint, peerId)
        }
        break

      case 'ack':
        if (result.acknowledgedPacketId) {
          this._bus.emit('ack:received', { packetId: result.acknowledgedPacketId })
        }
        break
    }
  }

  private _handleMultipeerPacket(peerId: string, raw: Uint8Array): void {
    // Multipeer is iOS-to-iOS only; same processing pipeline as BLE
    this._handleBlePacket(peerId, raw, false)
  }

  private _handlePeerFound(peer: NearbyPeer): void {
    this._peerRegistry.updateNearbyPeer(peer)
    this._bus.emit('peer:discovered', peer)

    // Check if this is a known contact
    if (peer.tokenHash) {
      const contact = this._peerRegistry.resolveContact(peer.tokenHash)
      if (contact) {
        contact.lastSeenMs = Date.now()
        contact.lastRssi   = peer.rssi
        this._bus.emit('contact:nearby', { contact, deviceId: peer.deviceId, rssi: peer.rssi })
      }
    }

    // Apply tie-breaker before attempting to connect
    // Design choice 3: the device with the higher token hash connects
    const ownToken  = this._peerRegistry.getOwnToken()
    const ownHash   = bytesToHex(ownToken.tokenHash)
    const theirHash = peer.tokenHash || ''

    if (theirHash && shouldActAsCentral(ownHash, theirHash)) {
      this._radioOrch.pauseForConnection()
      this._bleTransport.sendTo(peer.deviceId, packPacket(buildHandshakePacket(this._peerRegistry).packet), false)
        .catch(() => {/* ignore connection failures */})
        .finally(() => this._radioOrch.resumeFromConnection())
    }
  }

  private _handlePeerConnected(peerId: string, tokenHash: string, pubkeyFingerprint: string): void {
    const peer: NearbyPeer = {
      deviceId:       peerId,
      tokenHash,
      transport:      'ble-gatt',
      discoveredAtMs: Date.now(),
    }
    this._peerRegistry.updateNearbyPeer(peer)
    this._bus.emit('peer:connected', { deviceId: peerId, transport: 'ble-gatt' })

    const contact = this._peerRegistry.resolveContact(tokenHash)
    if (contact) {
      this._bus.emit('contact:nearby', { contact, deviceId: peerId })
    }
  }

  private _handlePeerDisconnected(peerId: string): void {
    this._peerRegistry.removeNearbyPeer(peerId)
    this._bus.emit('peer:disconnected', { deviceId: peerId })
  }

  private _handleAck(peerId: string, acknowledgedPacketIdHex: string): void {
    this._bus.emit('ack:received', { packetId: acknowledgedPacketIdHex })
  }

  private _handleHandshake(
    remotePublicKey:   Uint8Array,
    remoteFingerprint: string | undefined,
    fromPeerId:        string,
  ): void {
    // If we already have this contact, update last-seen
    const fingerprint = computeFingerprint(remotePublicKey)
    const existing    = this._peerRegistry.getContact(fingerprint)

    if (existing) {
      existing.lastSeenMs = Date.now()
      this._bus.emit('contact:nearby', { contact: existing, deviceId: fromPeerId })
      return
    }

    // Unknown peer — emit event so the app layer can prompt the user
    // to add them as a contact (e.g. show "Add contact?" UI)
    this._bus.emit('handshake:completed', { contactId: fingerprint })

    // Auto-response: send our own handshake so they can do the same
    const { raw } = buildHandshakePacket(this._peerRegistry)
    this._bleTransport.sendTo(fromPeerId, raw, false).catch(() => {})
  }

  // ─── Private: relay ──────────────────────────────────────────────────────────

  private async _relayPacket(packet: typeof packet): Promise<void> {
    if (!packet) return
    const decremented = this._router.decrementTTL(packet)
    const raw         = packPacket(decremented)
    const targets     = this._router.selectRelayTargets(
      decremented,
      this._bleTransport.getConnectedPeerIds(),
    )

    for (const peerId of targets) {
      try {
        await this._bleTransport.sendTo(peerId, raw, true /* relay char */)
        this._bus.emit('packet:relayed', { packetId: packet.header.packetId, toPeer: peerId })
      } catch { /* non-fatal */ }
    }
  }

  // ─── Private: message delivery ───────────────────────────────────────────────

  private _deliverMessage(message: MeshMessage, fromPeerId: string): void {
    // Insert into causal DAG for the conversation
    const store = this._getOrCreateStore(message.senderId)
    store.insert(message)

    this._bus.emit('message', message)
  }

  // ─── Private: token rotation ─────────────────────────────────────────────────

  /**
   * Design choice 2: rotate our own advertisement token every
   * tokenRotationIntervalMs to prevent passive BLE tracking.
   */
  private _rotateToken(): void {
    const announce = this._buildAnnounce()
    this._bleTransport.updateAnnounce(announce)
  }

  private _buildAnnounce(): Uint8Array {
    const ownToken     = this._peerRegistry.getOwnToken()
    const fingerprint  = hexToBytes(this._peerRegistry.getOwnFingerprint())
    return packAnnounce(ownToken.tokenHash, fingerprint)
  }

  // ─── Private: utilities ──────────────────────────────────────────────────────

  private _getOrCreateStore(conversationId: string): CausalMessageStore {
    let store = this._causalStores.get(conversationId)
    if (!store) {
      store = new CausalMessageStore()
      this._causalStores.set(conversationId, store)
    }
    return store
  }
}