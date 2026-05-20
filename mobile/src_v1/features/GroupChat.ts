/**
 * GroupChat Feature
 *
 * Manages group membership, invitations, and reliable fan-out delivery of
 * group messages to all currently-reachable members.
 *
 * Design notes:
 *  • Groups are locally created; there is no central authority.
 *  • The creator fans out the GroupInvite message to all initial members.
 *  • New messages are fan-fanned by the sender to every known member
 *    who has a reachable peer entry in PeerRegistry.
 *  • Members who were unreachable when the message was sent will not
 *    receive it (store-and-forward relay is out of scope for v1 — the mesh
 *    is ephemeral by design).
 */

import type { EventBus } from '../core/EventBus'
import type { PeerRegistry } from '../core/PeerRegistry'
import type { TransportManager } from '../core/TransportManager'
import type { MessageStore } from '../store/MessageStore'
import type { Group, GroupInvite, GroupMessage, GroupMeta } from '../types/ble'
import { generateUUID } from '../utils/uuid'

export class GroupChat {
  private transport: TransportManager
  private registry: PeerRegistry
  private bus: EventBus
  private store: MessageStore
  private selfId: string
  private groups = new Map<string, Group>()
  private unsubscribers: Array<() => void> = []

  constructor(
    selfId: string,
    transport: TransportManager,
    registry: PeerRegistry,
    bus: EventBus,
    store: MessageStore,
  ) {
    this.selfId = selfId
    this.transport = transport
    this.registry = registry
    this.bus = bus
    this.store = store
    this.registerListeners()
  }

  // ── Group Management ──────────────────────────────────────────────────────

  /** Create a new group and invite the provided member peer IDs. */
  async createGroup(name: string, memberPeerIds: string[]): Promise<Group> {
    const allMembers = Array.from(new Set([this.selfId, ...memberPeerIds]))
    const group: Group = {
      id: generateUUID(),
      name,
      members: allMembers,
      createdBy: this.selfId,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    }
    this.groups.set(group.id, group)
    this.store.saveGroup(group)

    // Fan out invitations.
    const invite: GroupInvite = {
      msgId: generateUUID(),
      from: this.selfId,
      kind: 'group_invite',
      groupId: group.id,
      groupName: group.name,
      invitedBy: this.selfId,
      members: allMembers,
      timestamp: Date.now(),
      transport: 'ble-gatt',
    }
    await this.transport.sendToMany(invite, memberPeerIds)
    return group
  }

  /** Update group metadata (name, members). Syncs changes to all members. */
  async updateGroup(groupId: string, changes: { name?: string; members?: string[] }): Promise<void> {
    const group = this.groups.get(groupId)
    if (!group) throw new Error(`Group ${groupId} not found`)

    const updated: Group = {
      ...group,
      ...changes,
      updatedAt: Date.now(),
    }
    this.groups.set(groupId, updated)
    this.store.saveGroup(updated)

    const meta: GroupMeta = {
      msgId: generateUUID(),
      from: this.selfId,
      kind: 'group_meta',
      groupId,
      name: changes.name,
      members: changes.members,
      timestamp: Date.now(),
      transport: 'ble-gatt',
    }
    const recipients = updated.members.filter((id) => id !== this.selfId)
    await this.transport.sendToMany(meta, recipients)
  }

  /** Leave a group (removes self from member list and syncs). */
  async leaveGroup(groupId: string): Promise<void> {
    const group = this.groups.get(groupId)
    if (!group) return
    await this.updateGroup(groupId, {
      members: group.members.filter((id) => id !== this.selfId),
    })
    this.groups.delete(groupId)
    this.store.deleteGroup(groupId)
  }

  // ── Messaging ─────────────────────────────────────────────────────────────

  /** Send a text message to a group. Fan-out is performed here. */
  async send(groupId: string, text: string, rawPayload?: string): Promise<GroupMessage> {
    const group = this.groups.get(groupId)
    if (!group) throw new Error(`Group ${groupId} not found`)

    const msg: GroupMessage = {
      msgId: generateUUID(),
      from: this.selfId,
      kind: 'group',
      groupId,
      text,
      rawPayload,
      timestamp: Date.now(),
      transport: 'ble-gatt',
    }

    this.store.saveMessage({
      msgId: msg.msgId,
      kind: 'group',
      from: msg.from,
      to: groupId,
      text,
      rawPayload,
      timestamp: msg.timestamp,
      delivered: false,
      read: true,  // self-authored
      transport: 'ble-gatt',
    })

    const recipients = group.members.filter((id) => id !== this.selfId)
    await this.transport.sendToMany(msg, recipients)
    return msg
  }

  // ── Inbound ───────────────────────────────────────────────────────────────

  private registerListeners(): void {
    this.unsubscribers.push(
      this.bus.on('message:group', (msg) => this.handleGroupMessage(msg)),
    )
    this.unsubscribers.push(
      this.bus.on('message:group_invite', (invite) => this.handleInvite(invite)),
    )
    this.unsubscribers.push(
      this.bus.on('message:group_meta', (meta) => this.handleMeta(meta)),
    )
  }

  private handleGroupMessage(msg: GroupMessage): void {
    const group = this.groups.get(msg.groupId)
    if (!group) return  // Not a member of this group.

    this.store.saveMessage({
      msgId: msg.msgId,
      kind: 'group',
      from: msg.from,
      to: msg.groupId,
      text: msg.text,
      rawPayload: msg.rawPayload,
      timestamp: msg.timestamp,
      delivered: true,
      read: false,
      transport: msg.transport,
    })
  }

  private handleInvite(invite: GroupInvite): void {
    if (this.groups.has(invite.groupId)) return  // Already a member.

    const group: Group = {
      id: invite.groupId,
      name: invite.groupName,
      members: invite.members,
      createdBy: invite.invitedBy,
      createdAt: invite.timestamp,
      updatedAt: invite.timestamp,
    }
    this.groups.set(group.id, group)
    this.store.saveGroup(group)
  }

  private handleMeta(meta: GroupMeta): void {
    const group = this.groups.get(meta.groupId)
    if (!group) return
    const updated: Group = {
      ...group,
      name: meta.name ?? group.name,
      members: meta.members ?? group.members,
      updatedAt: meta.timestamp,
    }
    this.groups.set(meta.groupId, updated)
    this.store.saveGroup(updated)
    // If we're no longer in the member list, leave.
    if (!updated.members.includes(this.selfId)) {
      this.groups.delete(meta.groupId)
    }
  }

  // ── Queries ───────────────────────────────────────────────────────────────

  getGroup(groupId: string): Group | undefined {
    return this.groups.get(groupId)
  }

  getAllGroups(): Group[] {
    return Array.from(this.groups.values())
  }

  getMessages(groupId: string) {
    return this.store.getMessages(groupId, 'group')
  }

  markRead(groupId: string): void {
    this.store.markReadByConversation(groupId, 'group')
  }

  loadGroupsFromStore(): void {
    const stored = this.store.loadGroups()
    for (const g of stored) this.groups.set(g.id, g)
  }

  // ── Teardown ──────────────────────────────────────────────────────────────

  destroy(): void {
    for (const unsub of this.unsubscribers) unsub()
  }
}