/**
 * @file eventBus.ts
 * Lightweight typed event bus used internally across all mesh modules.
 * External consumers use MeshEngine.on() / MeshEngine.off(), not this directly.
 */

export type EventListener<T> = (data: T) => void

export class EventBus<EventMap extends Record<string, unknown>> {
  private readonly _listeners = new Map<keyof EventMap, Set<EventListener<unknown>>>()

  on<K extends keyof EventMap>(event: K, listener: EventListener<EventMap[K]>): () => void {
    if (!this._listeners.has(event)) {
      this._listeners.set(event, new Set())
    }
    this._listeners.get(event)!.add(listener as EventListener<unknown>)
    return () => this.off(event, listener)
  }

  off<K extends keyof EventMap>(event: K, listener: EventListener<EventMap[K]>): void {
    this._listeners.get(event)?.delete(listener as EventListener<unknown>)
  }

  emit<K extends keyof EventMap>(event: K, data: EventMap[K]): void {
    const listeners = this._listeners.get(event)
    if (listeners) {
      for (const listener of listeners) {
        try {
          listener(data)
        } catch (err) {
          // Isolate listener errors — never crash the mesh engine
          console.error(`[MeshEventBus] Unhandled error in listener for "${String(event)}":`, err)
        }
      }
    }
  }

  once<K extends keyof EventMap>(event: K, listener: EventListener<EventMap[K]>): () => void {
    const off = this.on(event, (data) => {
      off()
      listener(data)
    })
    return off
  }

  removeAllListeners(event?: keyof EventMap): void {
    if (event !== undefined) {
      this._listeners.delete(event)
    } else {
      this._listeners.clear()
    }
  }
}