/**
 * EventBus
 *
 * A strictly-typed internal pub/sub bus that decouples the core engine modules
 * (BLEEngine, TransportManager, PeerRegistry) from the feature modules
 * (DirectMessaging, GroupChat, Presence, DeviceLocator) and the React hooks.
 *
 * Key design decisions:
 *  • Synchronous delivery — listeners are called inline during emit().
 *    If you need async fan-out, wrap in a microtask inside the listener.
 *  • Error isolation — an exception inside one listener does not prevent
 *    other listeners on the same event from running.
 *  • Typed via TypeScript generics (MeshEventMap from types.ts).
 */

import type { MeshEventMap } from '../types/ble'

type Listener<T> = (payload: T) => void
type Unsubscribe = () => void

export class EventBus {
  private listeners = new Map<string, Set<Listener<unknown>>>()

  /** Subscribe to an event. Returns an unsubscribe function. */
  on<K extends keyof MeshEventMap>(
    event: K,
    listener: Listener<MeshEventMap[K]>,
  ): Unsubscribe {
    if (!this.listeners.has(event as string)) {
      this.listeners.set(event as string, new Set())
    }
    const set = this.listeners.get(event as string)!
    set.add(listener as Listener<unknown>)
    return () => set.delete(listener as Listener<unknown>)
  }

  /** Subscribe to an event exactly once. */
  once<K extends keyof MeshEventMap>(
    event: K,
    listener: Listener<MeshEventMap[K]>,
  ): Unsubscribe {
    const unsub = this.on(event, (payload) => {
      unsub()
      listener(payload)
    })
    return unsub
  }

  /** Emit an event to all current listeners. */
  emit<K extends keyof MeshEventMap>(event: K, payload: MeshEventMap[K]): void {
    const set = this.listeners.get(event as string)
    if (!set || set.size === 0) return
    // Snapshot so that listeners added during emit don't run this cycle.
    for (const listener of Array.from(set)) {
      try {
        ;(listener as Listener<MeshEventMap[K]>)(payload)
      } catch (err) {
        console.error(`[EventBus] Unhandled error in listener for "${String(event)}":`, err)
      }
    }
  }

  /** Remove all listeners for a specific event, or all listeners if no event given. */
  off(event?: keyof MeshEventMap): void {
    if (event) {
      this.listeners.delete(event as string)
    } else {
      this.listeners.clear()
    }
  }

  /** Return the number of listeners registered for a given event. */
  listenerCount(event: keyof MeshEventMap): number {
    return this.listeners.get(event as string)?.size ?? 0
  }
}

/** Singleton bus shared across all core modules in a single MeshEngine instance. */
export const globalBus = new EventBus()