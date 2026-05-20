import { useCallback, useEffect, useState } from 'react'
import type { MeshEngine } from '../MeshEngine'
import type {
  Group,
  GroupMessage,
  StoredMessage,
} from '../types/ble'

// group message send/receive + membership
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