/**
 * MeshEngine
 *
 * Top-level singleton that wires every module together and provides a
 * single initialisation / teardown surface. Consumers interact with
 * individual feature modules via the engine instance.
 *
 * Usage:
 *   const engine = await MeshEngine.create(config)
 *   engine.dm.send(peerId, 'Hello!')
 *   engine.groupChat.createGroup('Team', [alice, bob])
 *   const result = await engine.locator.locate(peerId)
 *   await engine.destroy()
 */

import {
  STORAGE_KEY_DISPLAY_NAME,
  STORAGE_KEY_PEER_ID,
} from './constants/ble'
import { EventBus } from './core/EventBus'
import { PeerRegistry } from './core/PeerRegistry'
import { TransportManager } from './core/TransportManager'
import { DeviceLocator } from './features/DeviceLocator'
import { DirectMessaging } from './features/DirectMessaging'
import { GroupChat } from './features/GroupChat'
import { Presence } from './features/Presence'
import { MessageStore, setAsyncStorage } from './store/MessageStore'
import type { MeshEngineConfig, Peer } from './types/ble'
import { generateUUID } from './utils/uuid'

export class MeshEngine {
  readonly bus: EventBus
  readonly registry: PeerRegistry
  readonly store: MessageStore
  readonly transport: TransportManager
  readonly dm: DirectMessaging
  readonly groupChat: GroupChat
  readonly locator: DeviceLocator
  readonly presence: Presence
  readonly selfId: string
  readonly displayName: string

  private constructor(
    config: MeshEngineConfig,
    bus: EventBus,
    registry: PeerRegistry,
    store: MessageStore,
    transport: TransportManager,
    dm: DirectMessaging,
    groupChat: GroupChat,
    locator: DeviceLocator,
    presence: Presence,
  ) {
    this.bus = bus
    this.registry = registry
    this.store = store
    this.transport = transport
    this.dm = dm
    this.groupChat = groupChat
    this.locator = locator
    this.presence = presence
    this.selfId = config.selfId
    this.displayName = config.displayName
  }

  /**
   * Bootstrap the mesh engine.
   *
   * @param config Engine configuration.
   * @param asyncStorage Optionally pass AsyncStorage for message persistence.
   *   Example: `import AsyncStorage from '@react-native-async-storage/async-storage'`
   */
  static async create(
    config: MeshEngineConfig,
    asyncStorage?: Parameters<typeof setAsyncStorage>[0],
  ): Promise<MeshEngine> {
    if (asyncStorage) setAsyncStorage(asyncStorage)

    const bus = new EventBus()
    const registry = new PeerRegistry(bus)
    const store = new MessageStore()
    const transport = new TransportManager(config, bus, registry)
    const dm = new DirectMessaging(config.selfId, transport, bus, store)
    const groupChat = new GroupChat(config.selfId, transport, registry, bus, store)
    const locator = new DeviceLocator(config.selfId, transport, registry, bus)
    const presence = new Presence(config.selfId, config.displayName, transport, registry, bus)

    registry.start()

    // Hydrate persisted state.
    await store.hydrate([])
    groupChat.loadGroupsFromStore()

    // Boot transports (BLE + Multipeer).
    await transport.init()

    // Start presence heartbeats.
    presence.start()

    return new MeshEngine(config, bus, registry, store, transport, dm, groupChat, locator, presence)
  }

  // ── Convenience Accessors ─────────────────────────────────────────────────

  /** List all currently visible peers. */
  getPeers(): Peer[] {
    return this.registry.getNearby()
  }

  /** Get a specific peer by ID. */
  getPeer(peerId: string): Peer | undefined {
    return this.registry.get(peerId)
  }

  // ── Teardown ──────────────────────────────────────────────────────────────

  async destroy(): Promise<void> {
    this.presence.stop()
    this.dm.destroy()
    this.groupChat.destroy()
    this.locator.destroy()
    await this.transport.destroy()
    this.bus.off()
  }
}

// ─── Peer ID Bootstrap Helper ─────────────────────────────────────────────────

/**
 * Load or generate this device's stable peer ID and display name.
 * Call before MeshEngine.create() and pass the result as config.selfId.
 *
 * @param asyncStorage AsyncStorage instance from '@react-native-async-storage/async-storage'.
 * @param defaultDisplayName Fallback name if none is stored.
 */
export async function bootstrapPeerIdentity(
  asyncStorage: Parameters<typeof setAsyncStorage>[0],
  defaultDisplayName: string = 'Unknown',
): Promise<{ selfId: string; displayName: string }> {
  let selfId: string | null = null
  let displayName: string | null = null
  try {
    selfId = await asyncStorage.getItem(STORAGE_KEY_PEER_ID)
    displayName = await asyncStorage.getItem(STORAGE_KEY_DISPLAY_NAME)
  } catch { /* ignore */ }

  if (!selfId) {
    selfId = generateUUID()
    try { await asyncStorage.setItem(STORAGE_KEY_PEER_ID, selfId) } catch { /* ignore */ }
  }
  if (!displayName) {
    displayName = defaultDisplayName
  }
  return { selfId, displayName }
}

/**
 * Persist a new display name for this device.
 */
export async function setDisplayName(
  asyncStorage: Parameters<typeof setAsyncStorage>[0],
  name: string,
): Promise<void> {
  try { await asyncStorage.setItem(STORAGE_KEY_DISPLAY_NAME, name) } catch { /* ignore */ }
}