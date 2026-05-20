import { useCallback, useEffect, useState } from 'react'
import type { RangeResult } from '../features/DeviceLocator'
import type { MeshEngine } from '../MeshEngine'

// distance/RSSI ranging for a specific peer
export interface UseDeviceLocatorResult {
  /** Latest range result for this peer. */
  range: RangeResult | null
  /** Whether a locate request is in progress. */
  locating: boolean
  /** Latest RTT in milliseconds. */
  rttMs: number | null
  /** Trigger an active range measurement. */
  locate: () => Promise<void>
  /** Trigger a ping/pong RTT measurement. */
  ping: () => Promise<void>
}

/**
 * RSSI-based ranging for a specific peer.
 * @param engine  MeshEngine instance.
 * @param peerId  The target peer's stable ID.
 */
export function useDeviceLocator(
  engine: MeshEngine | null,
  peerId: string | null,
): UseDeviceLocatorResult {
  const [range, setRange] = useState<RangeResult | null>(null)
  const [locating, setLocating] = useState(false)
  const [rttMs, setRttMs] = useState<number | null>(null)

  // Update range passively when peer RSSI changes.
  useEffect(() => {
    if (!engine || !peerId) return
    const unsub = engine.bus.on('peer:updated', (peer) => {
      if (peer.id !== peerId) return
      setRange((prev) => ({
        peerId,
        localRssi: peer.rssiSmoothed,
        peerRssi: prev?.peerRssi ?? null,
        estimatedDistanceM: peer.estimatedDistance,
        rttMs: prev?.rttMs ?? null,
      }))
    })
    return unsub
  }, [engine, peerId])

  const locate = useCallback(async () => {
    if (!engine || !peerId) return
    setLocating(true)
    try {
      const result = await engine.locator.locate(peerId)
      setRange(result)
    } finally {
      setLocating(false)
    }
  }, [engine, peerId])

  const ping = useCallback(async () => {
    if (!engine || !peerId) return
    try {
      const rtt = await engine.locator.ping(peerId)
      setRttMs(rtt)
    } catch { /* ignore timeout */ }
  }, [engine, peerId])

  return { range, locating, rttMs, locate, ping }
}