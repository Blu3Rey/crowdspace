/**
 * DirectMessaging Feature
 *
 * Provides send / receive semantics for point-to-point text messages with:
 *  • Optimistic local delivery (message visible immediately).
 *  • ACK-based reliability (resend if no ACK within timeout).
 *  • Ordered delivery via timestamp-sorted store queries.
 *  • Thin persistence layer through MessageStore.
 */

import type { EventBus } from '../core/EventBus'
import type { TransportManager } from '../core/TransportManager'
import type { MessageStore } from '../store/MessageStore'
import type { DirectMessage, DmAck } from '../types/ble'
import { generateUUID } from '../utils/uuid'

const ACK_TIMEOUT_MS = 12_000
const MAX_DM_RETRIES = 2

interface PendingAck {
  msg: DirectMessage
  retries: number
  timerId: ReturnType<typeof setTimeout>
}

export class DirectMessaging {
  private transport: TransportManager
  private bus: EventBus
  private store: MessageStore
  private selfId: string
  private pendingAcks = new Map<string, PendingAck>()
  private unsubscribers: Array<() => void> = []

  constructor(
    selfId: string,
    transport: TransportManager,
    bus: EventBus,
    store: MessageStore,
  ) {
    this.selfId = selfId
    this.transport = transport
    this.bus = bus
    this.store = store
    this.registerListeners()
  }

  // ── Send ──────────────────────────────────────────────────────────────────

  /**
   * Send a direct message to a peer.
   * Persists optimistically, then transmits. Schedules an ACK timeout.
   */
  async send(toPeerId: string, text: string, rawPayload?: string): Promise<DirectMessage> {
    const msg: DirectMessage = {
      msgId: generateUUID(),
      from: this.selfId,
      to: toPeerId,
      kind: 'dm',
      text,
      rawPayload,
      timestamp: Date.now(),
      transport: 'ble-gatt',
    }

    this.store.saveMessage({
      msgId: msg.msgId,
      kind: 'dm',
      from: msg.from,
      to: toPeerId,
      text,
      rawPayload,
      timestamp: msg.timestamp,
      delivered: false,
      read: false,
      transport: 'ble-gatt',
    })

    await this.transmit(msg)
    this.scheduleAckTimeout(msg)
    return msg
  }

  private async transmit(msg: DirectMessage, attempt = 0): Promise<void> {
    try {
      await this.transport.send(msg)
    } catch (err) {
      console.warn(`[DM] transmit error (attempt ${attempt}):`, err)
      if (attempt < MAX_DM_RETRIES) {
        await sleep(800 * (attempt + 1))
        return this.transmit(msg, attempt + 1)
      }
    }
  }

  private scheduleAckTimeout(msg: DirectMessage): void {
    const timerId = setTimeout(() => {
      const pending = this.pendingAcks.get(msg.msgId)
      if (!pending) return
      if (pending.retries < MAX_DM_RETRIES) {
        pending.retries++
        void this.transmit(msg, 0)
        this.scheduleAckTimeout(msg)
      } else {
        this.pendingAcks.delete(msg.msgId)
        console.warn('[DM] No ACK received for', msg.msgId)
      }
    }, ACK_TIMEOUT_MS)

    this.pendingAcks.set(msg.msgId, { msg, retries: 0, timerId })
  }

  // ── Receive ───────────────────────────────────────────────────────────────

  private registerListeners(): void {
    this.unsubscribers.push(
      this.bus.on('message:dm', (msg) => this.handleInbound(msg)),
    )
    this.unsubscribers.push(
      this.bus.on('message:dm_ack', (ack) => this.handleAck(ack)),
    )
  }

  private async handleInbound(msg: DirectMessage): Promise<void> {
    if (msg.to !== this.selfId) return  // Not addressed to us (shouldn't happen via GATT)

    this.store.saveMessage({
      msgId: msg.msgId,
      kind: 'dm',
      from: msg.from,
      to: msg.to,
      text: msg.text,
      rawPayload: msg.rawPayload,
      timestamp: msg.timestamp,
      delivered: true,
      read: false,
      transport: msg.transport,
    })

    // Send ACK back.
    const ack: DmAck = {
      msgId: generateUUID(),
      from: this.selfId,
      kind: 'dm_ack',
      timestamp: Date.now(),
      transport: msg.transport,
      ackedMsgId: msg.msgId,
    }
    try {
      await this.transport.send(ack)
    } catch (err) {
      console.warn('[DM] Failed to send ACK:', err)
    }

    this.bus.emit('ack:dm', { msgId: msg.msgId, from: msg.from })
  }

  private handleAck(ack: DmAck): void {
    const pending = this.pendingAcks.get(ack.ackedMsgId)
    if (!pending) return
    clearTimeout(pending.timerId)
    this.pendingAcks.delete(ack.ackedMsgId)
    this.store.markDelivered(ack.ackedMsgId)
    this.bus.emit('ack:dm', { msgId: ack.ackedMsgId, from: ack.from })
  }

  // ── Queries ───────────────────────────────────────────────────────────────

  getConversation(peerId: string) {
    return this.store.getMessages(peerId, 'dm')
  }

  markRead(peerId: string): void {
    this.store.markReadByConversation(peerId, 'dm')
  }

  // ── Teardown ──────────────────────────────────────────────────────────────

  destroy(): void {
    for (const { timerId } of this.pendingAcks.values()) clearTimeout(timerId)
    this.pendingAcks.clear()
    for (const unsub of this.unsubscribers) unsub()
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((res) => setTimeout(res, ms))
}