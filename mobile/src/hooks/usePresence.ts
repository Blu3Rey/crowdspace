import { useCallback, useEffect, useState } from 'react'
import type { MeshEngine } from '../MeshEngine'
import type { PresenceStatus } from '../types/ble'

export interface UsePresenceResult {
  /** This device's current status. */
  selfStatus: PresenceStatus
  /** Update this device's status. */
  setStatus: (status: PresenceStatus) => Promise<void>
  /** Get a specific peer's presence status. */
  getPeerStatus: (peerId: string) => PresenceStatus
}

export function usePresence(engine: MeshEngine | null): UsePresenceResult {
  const [selfStatus, setSelfStatus] = useState<PresenceStatus>('online')

  const setStatus = useCallback(
    async (status: PresenceStatus) => {
      if (!engine) return
      await engine.presence.setStatus(status)
      setSelfStatus(status)
    },
    [engine],
  )

  const getPeerStatus = useCallback(
    (peerId: string): PresenceStatus => {
      if (!engine) return 'offline'
      return engine.presence.getPeerStatus(peerId)
    },
    [engine],
  )

  return { selfStatus, setStatus, getPeerStatus }
}