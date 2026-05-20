/**
 * MessageStore
 *
 * In-memory message and group storage with optional AsyncStorage persistence.
 * The store is intentionally simple: messages are keyed by conversation ID
 * (peerId for DMs, groupId for group chat), sorted by timestamp.
 *
 * Persistence: AsyncStorage is used when available (standard in React Native).
 * If unavailable (e.g. test environment), the store remains in-memory only.
 * Persistence is fire-and-forget to avoid blocking the UI thread.
 */

import { STORAGE_KEY_GROUPS, STORAGE_KEY_MESSAGES_PREFIX } from '../constants/ble'
import type { Group, StoredMessage } from '../types/ble'

// ─── Async Storage interface (avoid hard dependency) ──────────────────────────

interface AsyncStorageLike {
  getItem(key: string): Promise<string | null>
  setItem(key: string, value: string): Promise<void>
  removeItem(key: string): Promise<void>
}

let _AsyncStorage: AsyncStorageLike | null = null

/** Inject AsyncStorage (call from MeshEngine.init() after importing the package). */
export function setAsyncStorage(storage: AsyncStorageLike): void {
  _AsyncStorage = storage
}

// ─── MessageStore ─────────────────────────────────────────────────────────────

export class MessageStore {
  /** conversationId → sorted messages */
  private messages = new Map<string, StoredMessage[]>()
  private groups = new Map<string, Group>()

  // ── Messages ──────────────────────────────────────────────────────────────

  saveMessage(msg: StoredMessage): void {
    const key = msg.to   // peerId (DM) or groupId (group)
    if (!this.messages.has(key)) this.messages.set(key, [])
    const list = this.messages.get(key)!

    // Deduplicate by msgId.
    const existing = list.findIndex((m) => m.msgId === msg.msgId)
    if (existing >= 0) {
      list[existing] = msg
    } else {
      list.push(msg)
      list.sort((a, b) => a.timestamp - b.timestamp)
    }

    void this.persistMessages(key)
  }

  markDelivered(msgId: string): void {
    for (const list of this.messages.values()) {
      const idx = list.findIndex((m) => m.msgId === msgId)
      if (idx >= 0) {
        list[idx] = { ...list[idx]!, delivered: true }
        void this.persistMessages(list[idx]!.to)
        return
      }
    }
  }

  markRead(msgId: string): void {
    for (const list of this.messages.values()) {
      const idx = list.findIndex((m) => m.msgId === msgId)
      if (idx >= 0) {
        list[idx] = { ...list[idx]!, read: true }
        void this.persistMessages(list[idx]!.to)
        return
      }
    }
  }

  markReadByConversation(conversationId: string, kind: 'dm' | 'group'): void {
    const list = this.messages.get(conversationId)
    if (!list) return
    let changed = false
    for (let i = 0; i < list.length; i++) {
      if (!list[i]!.read && list[i]!.kind === kind) {
        list[i] = { ...list[i]!, read: true }
        changed = true
      }
    }
    if (changed) void this.persistMessages(conversationId)
  }

  getMessages(conversationId: string, kind: 'dm' | 'group'): StoredMessage[] {
    return (this.messages.get(conversationId) ?? []).filter((m) => m.kind === kind)
  }

  getUnreadCount(conversationId: string): number {
    return (this.messages.get(conversationId) ?? []).filter((m) => !m.read).length
  }

  getAllConversations(): { id: string; kind: 'dm' | 'group'; lastMessage: StoredMessage }[] {
    const result: { id: string; kind: 'dm' | 'group'; lastMessage: StoredMessage }[] = []
    for (const [id, list] of this.messages) {
      if (list.length === 0) continue
      const last = list[list.length - 1]!
      result.push({ id, kind: last.kind, lastMessage: last })
    }
    return result.sort((a, b) => b.lastMessage.timestamp - a.lastMessage.timestamp)
  }

  // ── Groups ────────────────────────────────────────────────────────────────

  saveGroup(group: Group): void {
    this.groups.set(group.id, group)
    void this.persistGroups()
  }

  loadGroups(): Group[] {
    return Array.from(this.groups.values())
  }

  deleteGroup(groupId: string): void {
    this.groups.delete(groupId)
    this.messages.delete(groupId)
    void this.persistGroups()
  }

  // ── Persistence ───────────────────────────────────────────────────────────

  private async persistMessages(conversationId: string): Promise<void> {
    if (!_AsyncStorage) return
    const list = this.messages.get(conversationId) ?? []
    // Keep last 500 messages per conversation to bound storage.
    const bounded = list.slice(-500)
    try {
      await _AsyncStorage.setItem(
        STORAGE_KEY_MESSAGES_PREFIX + conversationId,
        JSON.stringify(bounded),
      )
    } catch (err) {
      console.warn('[MessageStore] Failed to persist messages:', err)
    }
  }

  private async persistGroups(): Promise<void> {
    if (!_AsyncStorage) return
    try {
      await _AsyncStorage.setItem(
        STORAGE_KEY_GROUPS,
        JSON.stringify(Array.from(this.groups.values())),
      )
    } catch (err) {
      console.warn('[MessageStore] Failed to persist groups:', err)
    }
  }

  /** Load persisted state from AsyncStorage. Call once on startup. */
  async hydrate(conversationIds: string[]): Promise<void> {
    if (!_AsyncStorage) return
    try {
      // Load groups.
      const groupsJson = await _AsyncStorage.getItem(STORAGE_KEY_GROUPS)
      if (groupsJson) {
        const groups: Group[] = JSON.parse(groupsJson)
        for (const g of groups) this.groups.set(g.id, g)
      }
      // Load messages for known conversations.
      for (const id of conversationIds) {
        const json = await _AsyncStorage.getItem(STORAGE_KEY_MESSAGES_PREFIX + id)
        if (json) {
          const msgs: StoredMessage[] = JSON.parse(json)
          this.messages.set(id, msgs)
        }
      }
    } catch (err) {
      console.warn('[MessageStore] Failed to hydrate from storage:', err)
    }
  }

  /** Wipe all in-memory and persisted state. */
  async clear(): Promise<void> {
    this.messages.clear()
    this.groups.clear()
    // Persist empty groups.
    void this.persistGroups()
  }
}