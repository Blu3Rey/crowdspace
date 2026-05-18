import { useEffect, useState } from 'react'
import type { MeshEngine } from '../MeshEngine'
import type { Peer } from '../types/ble'

// engine lifecycle, peer list, presence status
export interface UseMeshNetworkResult {
  /** Whether the engine is initialised and ready. */
  ready: boolean
  /** All currently visible nearby peers. */
  peers: Peer[]
  /** The local device's peer ID. */
  selfId: string | null
  /** The local device's display name. */
  displayName: string | null
  /** Last engine-level error, if any. */
  error: Error | null
}

/**
 * Subscribe to peer list and engine lifecycle events.
 * @param engine MeshEngine instance, or null if not yet initialised.
 */
export function useMeshNetwork(engine: MeshEngine | null): UseMeshNetworkResult {
  const [ready, setReady] = useState(false)
  const [peers, setPeers] = useState<Peer[]>([])
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    if (!engine) return

    setReady(true)
    setPeers(engine.getPeers())

    const unsubs = [
      engine.bus.on('engine:ready', () => setReady(true)),
      engine.bus.on('engine:stopped', () => setReady(false)),
      engine.bus.on('engine:error', ({ error: e }) => setError(e)),
      engine.bus.on('peer:discovered', () => setPeers(engine.getPeers())),
      engine.bus.on('peer:updated', () => setPeers(engine.getPeers())),
      engine.bus.on('peer:connected', () => setPeers(engine.getPeers())),
      engine.bus.on('peer:disconnected', () => setPeers(engine.getPeers())),
      engine.bus.on('peer:lost', () => setPeers(engine.getPeers())),
    ]
    return () => unsubs.forEach((u) => u())
  }, [engine])

  return {
    ready,
    peers,
    selfId: engine?.selfId ?? null,
    displayName: engine?.displayName ?? null,
    error,
  }
}