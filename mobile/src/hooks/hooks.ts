/**
 * React Hooks
 *
 * A complete set of React hooks that wrap MeshEngine and expose reactive
 * state to UI components. All hooks re-render only on relevant state changes.
 *
 * hooks/useMeshNetwork   — engine lifecycle, peer list, presence status
 * hooks/useDirectMessage — DM send/receive for a specific conversation
 * hooks/useGroupChat     — group message send/receive + membership
 * hooks/useDeviceLocator — distance/RSSI ranging for a specific peer
 */

import { useCallback, useEffect, useState } from 'react'
import type { RangeResult } from '../features/DeviceLocator'
import type { MeshEngine } from '../MeshEngine'
import type {
  DirectMessage,
  Group,
  GroupMessage,
  Peer,
  PresenceStatus,
  StoredMessage,
} from '../types/ble'

// ─── useMeshNetwork ───────────────────────────────────────────────────────────

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

// ─── useDirectMessage ─────────────────────────────────────────────────────────

export interface UseDMResult {
  /** All messages in this conversation, sorted oldest-first. */
  messages: StoredMessage[]
  /** Send a text message. Returns the optimistic message object. */
  send: (text: string) => Promise<DirectMessage>
  /** Mark all messages in this conversation as read. */
  markRead: () => void
  /** Number of unread messages. */
  unreadCount: number
  /** Whether the peer is currently reachable. */
  peerReachable: boolean
  /** Presence status of the peer. */
  peerPresence: PresenceStatus
}

/**
 * Manage a direct message conversation with a specific peer.
 * @param engine  MeshEngine instance.
 * @param peerId  The remote peer's stable ID.
 */
export function useDirectMessage(
  engine: MeshEngine | null,
  peerId: string | null,
): UseDMResult {
  const [messages, setMessages] = useState<StoredMessage[]>([])
  const [unreadCount, setUnreadCount] = useState(0)
  const [peerReachable, setPeerReachable] = useState(false)
  const [peerPresence, setPeerPresence] = useState<PresenceStatus>('offline')

  const refresh = useCallback(() => {
    if (!engine || !peerId) return
    setMessages(engine.dm.getConversation(peerId))
    setUnreadCount(engine.store.getUnreadCount(peerId))
    const peer = engine.getPeer(peerId)
    setPeerReachable(
      peer?.connectionState === 'connected' || peer?.connectionState === 'subscribed',
    )
    setPeerPresence(peer?.presenceStatus ?? 'offline')
  }, [engine, peerId])

  useEffect(() => {
    if (!engine || !peerId) return
    refresh()

    const unsubs = [
      engine.bus.on('message:dm', (msg) => {
        if (msg.from === peerId || msg.to === peerId) refresh()
      }),
      engine.bus.on('ack:dm', ({ msgId }) => refresh()),
      engine.bus.on('peer:updated', (peer) => {
        if (peer.id === peerId) refresh()
      }),
      engine.bus.on('peer:connected', (peer) => {
        if (peer.id === peerId) refresh()
      }),
      engine.bus.on('peer:disconnected', (peer) => {
        if (peer.id === peerId) refresh()
      }),
    ]
    return () => unsubs.forEach((u) => u())
  }, [engine, peerId, refresh])

  const send = useCallback(
    (text: string) => {
      if (!engine || !peerId) throw new Error('Engine or peerId not set')
      return engine.dm.send(peerId, text)
    },
    [engine, peerId],
  )

  const markRead = useCallback(() => {
    if (!engine || !peerId) return
    engine.dm.markRead(peerId)
    refresh()
  }, [engine, peerId, refresh])

  return { messages, send, markRead, unreadCount, peerReachable, peerPresence }
}

// ─── useGroupChat ─────────────────────────────────────────────────────────────

export interface UseGroupChatResult {
  /** All messages in this group, sorted oldest-first. */
  messages: StoredMessage[]
  /** The group descriptor (name, members). */
  group: Group | null
  /** Send a text message to the group. */
  send: (text: string) => Promise<GroupMessage>
  /** Mark all messages as read. */
  markRead: () => void
  /** Number of unread messages. */
  unreadCount: number
  /** Leave this group. */
  leave: () => Promise<void>
  /** Update group name. */
  rename: (newName: string) => Promise<void>
}

/**
 * Manage a group chat.
 * @param engine   MeshEngine instance.
 * @param groupId  Group UUID.
 */
export function useGroupChat(
  engine: MeshEngine | null,
  groupId: string | null,
): UseGroupChatResult {
  const [messages, setMessages] = useState<StoredMessage[]>([])
  const [group, setGroup] = useState<Group | null>(null)
  const [unreadCount, setUnreadCount] = useState(0)

  const refresh = useCallback(() => {
    if (!engine || !groupId) return
    setMessages(engine.groupChat.getMessages(groupId))
    setGroup(engine.groupChat.getGroup(groupId) ?? null)
    setUnreadCount(engine.store.getUnreadCount(groupId))
  }, [engine, groupId])

  useEffect(() => {
    if (!engine || !groupId) return
    refresh()
    const unsubs = [
      engine.bus.on('message:group', (msg) => {
        if (msg.groupId === groupId) refresh()
      }),
      engine.bus.on('message:group_meta', (meta) => {
        if (meta.groupId === groupId) refresh()
      }),
    ]
    return () => unsubs.forEach((u) => u())
  }, [engine, groupId, refresh])

  const send = useCallback(
    (text: string) => {
      if (!engine || !groupId) throw new Error('Engine or groupId not set')
      return engine.groupChat.send(groupId, text)
    },
    [engine, groupId],
  )

  const markRead = useCallback(() => {
    if (!engine || !groupId) return
    engine.groupChat.markRead(groupId)
    refresh()
  }, [engine, groupId, refresh])

  const leave = useCallback(async () => {
    if (!engine || !groupId) return
    await engine.groupChat.leaveGroup(groupId)
    refresh()
  }, [engine, groupId, refresh])

  const rename = useCallback(
    async (newName: string) => {
      if (!engine || !groupId) return
      await engine.groupChat.updateGroup(groupId, { name: newName })
      refresh()
    },
    [engine, groupId, refresh],
  )

  return { messages, group, send, markRead, unreadCount, leave, rename }
}

// ─── useDeviceLocator ─────────────────────────────────────────────────────────

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

// ─── usePresence ──────────────────────────────────────────────────────────────

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