import { useCallback, useEffect, useState } from 'react'
import type { MeshEngine } from '../MeshEngine'
import type {
  DirectMessage,
  PresenceStatus,
  StoredMessage,
} from '../types/ble'
import { PEER_STALE_TIMEOUT_MS } from '@/constants/ble'

// DM send/receive for a specific conversation
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
    // setPeerReachable(
    //   peer?.connectionState === 'connected' || peer?.connectionState === 'subscribed',
    // )
    setPeerReachable(
      peer != null &&
      peer.connectionState !== 'unreachable' &&
      Date.now() - peer.lastSeen < PEER_STALE_TIMEOUT_MS,
    );
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