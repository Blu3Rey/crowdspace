/**
 * @file useMesh.ts
 * React hook that manages a MeshEngine instance across the component lifecycle.
 *
 * Usage:
 *   const { engine, status, messages, nearbyContacts } = useMesh(keyPair, options)
 *
 *   // Send a text message to a contact
 *   await engine?.sendMessage(contactId, ContentType.TEXT, utf8('Hello!'))
 *
 * The hook handles:
 *   - init() + start() on mount
 *   - stop() on unmount
 *   - Reactive state updates from engine events
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { MeshEngine, generateIdentityKeyPair } from './MeshEngine'
import { Contact, ContentType, KeyPair, MeshEngineOptions, MeshMessage, NearbyPeer } from './core/types'

export interface MeshStatus {
  initialised: boolean
  running:     boolean
  error?:      string
  radioPhase?: string
}

export interface UseMeshResult {
  engine:          MeshEngine | null
  status:          MeshStatus
  /** Messages indexed by conversationId */
  messages:        Map<string, MeshMessage[]>
  /** Recently discovered nearby peers */
  nearbyPeers:     NearbyPeer[]
  /** Contacts that are currently nearby */
  nearbyContacts:  Array<{ contact: Contact; deviceId: string }>
  /** Refresh messages for a conversation from the causal DAG */
  getMessages:     (conversationId: string) => MeshMessage[]
}

/**
 * @param keyPair   The device's long-term identity key pair.
 *                  Generate once with generateIdentityKeyPair() and persist securely.
 * @param options   MeshEngineOptions — see MeshEngineOptions type.
 */
export function useMesh(keyPair: KeyPair, options?: MeshEngineOptions): UseMeshResult {
  const engineRef = useRef<MeshEngine | null>(null)
  const [status, setStatus]               = useState<MeshStatus>({ initialised: false, running: false })
  const [messages, setMessages]           = useState<Map<string, MeshMessage[]>>(new Map())
  const [nearbyPeers, setNearbyPeers]     = useState<NearbyPeer[]>([])
  const [nearbyContacts, setNearbyContacts] = useState<Array<{ contact: Contact; deviceId: string }>>([])

  useEffect(() => {
    const engine = new MeshEngine(keyPair, options)
    engineRef.current = engine

    const cleanupFns: (() => void)[] = []

    // Message handler
    cleanupFns.push(engine.on('message', (msg) => {
      setMessages(prev => {
        const next = new Map(prev)
        const convId = msg.senderId  // For DMs; override for groups
        const current = next.get(convId) ?? []
        // Deduplicate by id
        if (!current.some(m => m.id === msg.id)) {
          next.set(convId, [...current, msg])
        }
        return next
      })
    }))

    // Peer discovery
    cleanupFns.push(engine.on('peer:discovered', (peer) => {
      setNearbyPeers(prev => {
        const filtered = prev.filter(p => p.deviceId !== peer.deviceId)
        return [...filtered, peer]
      })
    }))

    cleanupFns.push(engine.on('peer:disconnected', ({ deviceId }) => {
      setNearbyPeers(prev => prev.filter(p => p.deviceId !== deviceId))
      setNearbyContacts(prev => prev.filter(nc => nc.deviceId !== deviceId))
    }))

    // Known contact came nearby
    cleanupFns.push(engine.on('contact:nearby', (ev) => {
      setNearbyContacts(prev => {
        const filtered = prev.filter(nc => nc.deviceId !== ev.deviceId)
        return [...filtered, { contact: ev.contact, deviceId: ev.deviceId }]
      })
    }))

    // Radio phase updates
    cleanupFns.push(engine.on('radio:phase', (status) => {
      setStatus(s => ({ ...s, radioPhase: status.phase }))
    }))

    // Error handler
    cleanupFns.push(engine.on('error', (err) => {
      setStatus(s => ({ ...s, error: err.message }))
    }))

    // Init and start
    engine.init().then(({ ok, reason }) => {
      if (!ok) {
        setStatus({ initialised: false, running: false, error: reason })
        return
      }
      engine.start()
      setStatus({ initialised: true, running: true })
    })

    return () => {
      engine.stop()
      for (const fn of cleanupFns) fn()
      engineRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])  // Run once on mount

  const getMessages = useCallback((conversationId: string): MeshMessage[] => {
    return engineRef.current?.getMessages(conversationId) ?? []
  }, [])

  return {
    engine:         engineRef.current,
    status,
    messages,
    nearbyPeers,
    nearbyContacts,
    getMessages,
  }
}

// ─── Convenience: UTF-8 encoding helpers ─────────────────────────────────────

/** Encode a string to UTF-8 bytes for use as message content */
export function utf8Encode(text: string): Uint8Array {
  return new TextEncoder().encode(text)
}

/** Decode UTF-8 bytes back to a string */
export function utf8Decode(bytes: Uint8Array): string {
  return new TextDecoder().decode(bytes)
}

/** Re-export ContentType so feature layers don't need to import from core */
export { ContentType, generateIdentityKeyPair }
export type { Contact, KeyPair, MeshEngineOptions, MeshMessage, NearbyPeer }
