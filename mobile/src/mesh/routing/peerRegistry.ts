/**
 * @file peerRegistry.ts
 * In-memory (and optionally persisted) store of:
 *   - Contacts: approved peers with shared keys for E2EE and token derivation
 *   - Nearby peers: transiently discovered BLE/Multipeer devices
 *   - Token → Contact resolution for incoming packet routing
 *
 * This is the only module that knows about Contact.sharedRootKey.
 * All crypto operations that need it are delegated to crypto.ts.
 */

import { computeFingerprint, currentToken, ownTokenForWindow, tokenHashMatchesContact, tokenWindowIndex } from '../core/crypto'
import { Contact, KeyPair, NearbyPeer, RotatingToken } from '../core/types'

export class PeerRegistry {
  /** All approved contacts, keyed by their stable fingerprint ID */
  private _contacts   = new Map<string, Contact>()
  /** Transiently discovered nearby peers, keyed by transport device ID */
  private _nearby     = new Map<string, NearbyPeer>()
  /** Cache: tokenHash hex → Contact, for fast inbound routing */
  private _tokenCache = new Map<string, { contact: Contact; windowIndex: number }>()

  private readonly _identityKeyPair: KeyPair

  constructor(identityKeyPair: KeyPair) {
    this._identityKeyPair = identityKeyPair
  }

  // ─── Contact management ──────────────────────────────────────────────────────

  addContact(contact: Contact): void {
    this._contacts.set(contact.id, contact)
    this._invalidateTokenCache(contact.id)
  }

  removeContact(id: string): void {
    this._contacts.delete(id)
    this._invalidateTokenCache(id)
  }

  getContact(id: string): Contact | undefined {
    return this._contacts.get(id)
  }

  getAllContacts(): Contact[] {
    return Array.from(this._contacts.values())
  }

  /** Find a contact whose current rotating token matches the observed token hash */
  resolveContact(observedTokenHash: string, nowMs = Date.now()): Contact | null {
    // Fast path: check cache
    const cached = this._tokenCache.get(observedTokenHash)
    if (cached && cached.windowIndex === tokenWindowIndex(nowMs)) {
      return cached.contact
    }

    // Slow path: iterate all contacts, check ±1 window tolerance
    for (const contact of this._contacts.values()) {
      if (tokenHashMatchesContact(observedTokenHash, contact, nowMs)) {
        this._tokenCache.set(observedTokenHash, { contact, windowIndex: tokenWindowIndex(nowMs) })
        return contact
      }
    }
    return null
  }

  /** Resolve a contact by their stable public key fingerprint */
  resolveByFingerprint(fingerprint: string): Contact | undefined {
    return this._contacts.get(fingerprint) ?? 
      Array.from(this._contacts.values()).find(c => 
        computeFingerprint(c.identityPublicKey) === fingerprint
      )
  }

  // ─── Nearby peer management ──────────────────────────────────────────────────

  updateNearbyPeer(peer: NearbyPeer): void {
    this._nearby.set(peer.deviceId, peer)
  }

  removeNearbyPeer(deviceId: string): void {
    this._nearby.delete(deviceId)
  }

  getNearbyPeers(): NearbyPeer[] {
    return Array.from(this._nearby.values())
  }

  /** Try to match a nearby peer's token hash to a known contact */
  resolveNearbyContact(deviceId: string): Contact | null {
    const peer = this._nearby.get(deviceId)
    if (!peer?.tokenHash) return null
    return this.resolveContact(peer.tokenHash)
  }

  // ─── Own token ───────────────────────────────────────────────────────────────

  /**
   * Our own rotating token for the current window.
   * Used in routing headers as senderTokenHash and in ANNOUNCE.
   */
  getOwnToken(nowMs = Date.now()): RotatingToken {
    return ownTokenForWindow(this._identityKeyPair.publicKey, tokenWindowIndex(nowMs))
  }

  /**
   * Compute a per-contact rotating token for a specific contact.
   * Used when building routing headers targeting a specific contact.
   */
  getTokenForContact(contactId: string, nowMs = Date.now()): RotatingToken | null {
    const contact = this._contacts.get(contactId)
    if (!contact) return null
    return currentToken(contact, nowMs)
  }

  /** Own stable identity fingerprint */
  getOwnFingerprint(): string {
    return computeFingerprint(this._identityKeyPair.publicKey)
  }

  getOwnPublicKey(): Uint8Array {
    return this._identityKeyPair.publicKey
  }

  getOwnSecretKey(): Uint8Array {
    return this._identityKeyPair.secretKey
  }

  // ─── Private ─────────────────────────────────────────────────────────────────

  private _invalidateTokenCache(contactId: string): void {
    // Evict any cached entries for this contact
    for (const [hash, entry] of this._tokenCache) {
      if (entry.contact.id === contactId) this._tokenCache.delete(hash)
    }
  }
}