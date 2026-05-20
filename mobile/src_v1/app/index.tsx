/**
 * Example: Full Integration
 *
 * A self-contained example showing how to wire up the BLE mesh engine in a
 * real React Native app. This file is NOT production code — it demonstrates
 * every major API surface in a single, readable file.
 *
 * Structure:
 *   1. Engine bootstrap (App.tsx)
 *   2. Peer list screen (PeersScreen)
 *   3. Direct message screen (ChatScreen)
 *   4. Group creation screen (NewGroupScreen)
 *   5. Group chat screen (GroupChatScreen)
 *   6. Device locator screen (RadarScreen)
 */

import React, {
  createContext,
  useContext,
  useEffect,
  useState
} from 'react'
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Platform,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native'

// Swap this for the real path in your project:
import type { Group, Peer, StoredMessage } from '../'
import {
  MeshEngine,
  bootstrapPeerIdentity,
  useDeviceLocator,
  useDirectMessage,
  useGroupChat,
  useMeshNetwork,
  usePresence,
} from '../'

// ─── Engine Context ───────────────────────────────────────────────────────────

const EngineContext = createContext<MeshEngine | null>(null)
const useEngine = () => useContext(EngineContext)

// ─── 1. App Bootstrap ─────────────────────────────────────────────────────────

/**
 * Root component.
 *
 * Replace the simple AsyncStorage shim below with the real package:
 *   import AsyncStorage from '@react-native-async-storage/async-storage'
 */
export default function App() {
  const [engine, setEngine] = useState<MeshEngine | null>(null)
  const [initError, setInitError] = useState<Error | null>(null)

  useEffect(() => {
    let eng: MeshEngine | null = null

    ;(async () => {
      try {
        // In a real app: import AsyncStorage from '@react-native-async-storage/async-storage'
        const AsyncStorage = makeInMemoryAsyncStorage()

        const { selfId, displayName } = await bootstrapPeerIdentity(
          AsyncStorage,
          `${Platform.OS} Device`,
        )

        eng = await MeshEngine.create(
          {
            selfId,
            displayName,
            background: true,
            androidNotificationTitle: 'Nearby messaging',
            androidNotificationText: 'Keeping Bluetooth alive',
            features: ['dm', 'group', 'locate', 'presence'],
          },
          AsyncStorage,
        )
        setEngine(eng)
      } catch (err) {
        setInitError(err as Error)
      }
    })()

    return () => { eng?.destroy() }
  }, [])

  if (initError) {
    return (
      <View style={styles.center}>
        <Text style={styles.error}>Failed to start Bluetooth: {initError.message}</Text>
      </View>
    )
  }

  if (!engine) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" />
        <Text style={styles.hint}>Starting Bluetooth…</Text>
      </View>
    )
  }

  return (
    <EngineContext.Provider value={engine}>
      {/* In a real app, mount your navigator here. For this example we show all screens. */}
      <PeersScreen />
    </EngineContext.Provider>
  )
}

// ─── 2. Peers Screen ──────────────────────────────────────────────────────────

function PeersScreen() {
  const engine = useEngine()
  const { ready, peers, selfId, displayName } = useMeshNetwork(engine)
  const { selfStatus, setStatus } = usePresence(engine)
  const [selectedPeer, setSelectedPeer] = useState<Peer | null>(null)
  const [showGroup, setShowGroup] = useState(false)

  if (selectedPeer) {
    return <ChatScreen peer={selectedPeer} onBack={() => setSelectedPeer(null)} />
  }

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <Text style={styles.title}>Nearby Peers</Text>
        <Text style={styles.subtitle}>
          {ready ? `${displayName} · ${peers.length} found` : 'Starting…'}
        </Text>
      </View>

      <View style={styles.presenceRow}>
        {(['online', 'away', 'busy'] as const).map((s) => (
          <TouchableOpacity
            key={s}
            style={[styles.statusBtn, selfStatus === s && styles.statusBtnActive]}
            onPress={() => setStatus(s)}
          >
            <Text style={styles.statusBtnText}>{s}</Text>
          </TouchableOpacity>
        ))}
      </View>

      {peers.length === 0 ? (
        <View style={styles.center}>
          <Text style={styles.hint}>Scanning for nearby devices…</Text>
        </View>
      ) : (
        <FlatList
          data={peers}
          keyExtractor={(p) => p.id}
          renderItem={({ item }) => (
            <PeerRow peer={item} onPress={() => setSelectedPeer(item)} />
          )}
        />
      )}

      <TouchableOpacity style={styles.fab} onPress={() => setShowGroup(true)}>
        <Text style={styles.fabText}>+ Group</Text>
      </TouchableOpacity>
    </View>
  )
}

function PeerRow({ peer, onPress }: { peer: Peer; onPress: () => void }) {
  const engine = useEngine()
  const { range } = useDeviceLocator(engine, peer.id)

  const stateColor = {
    discovered: '#aaa',
    connecting: '#f90',
    connected: '#4c4',
    subscribed: '#0c0',
    disconnected: '#c44',
    unreachable: '#c44',
  }[peer.connectionState]

  const presenceEmoji = {
    online: '🟢',
    away: '🟡',
    busy: '🔴',
    offline: '⚫️',
  }[peer.presenceStatus]

  return (
    <TouchableOpacity style={styles.peerRow} onPress={onPress}>
      <View style={[styles.dot, { backgroundColor: stateColor }]} />
      <View style={styles.peerInfo}>
        <Text style={styles.peerName}>{peer.displayName} {presenceEmoji}</Text>
        <Text style={styles.peerMeta}>
          {peer.connectionState}
          {range?.estimatedDistanceM != null
            ? ` · ~${range.estimatedDistanceM.toFixed(1)} m`
            : peer.rssiSmoothed != null
            ? ` · ${peer.rssiSmoothed.toFixed(0)} dBm`
            : ''}
        </Text>
      </View>
    </TouchableOpacity>
  )
}

// ─── 3. Direct Message Screen ─────────────────────────────────────────────────

function ChatScreen({ peer, onBack }: { peer: Peer; onBack: () => void }) {
  const engine = useEngine()
  const { messages, send, markRead, peerReachable, peerPresence } =
    useDirectMessage(engine, peer.id)
  const [text, setText] = useState('')
  const selfId = engine?.selfId ?? ''

  useEffect(() => { markRead() }, [])

  const handleSend = async () => {
    const trimmed = text.trim()
    if (!trimmed) return
    setText('')
    try {
      await send(trimmed)
    } catch (err) {
      Alert.alert('Send failed', (err as Error).message)
    }
  }

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity onPress={onBack}>
          <Text style={styles.back}>← Back</Text>
        </TouchableOpacity>
        <Text style={styles.title}>{peer.displayName}</Text>
        <Text style={styles.subtitle}>
          {peerReachable ? peerPresence : 'unreachable'}
        </Text>
      </View>

      <FlatList
        data={messages}
        keyExtractor={(m) => m.msgId}
        renderItem={({ item }) => (
          <MessageBubble msg={item} isMine={item.from === selfId} />
        )}
        contentContainerStyle={{ padding: 12 }}
      />

      <View style={styles.inputRow}>
        <TextInput
          style={styles.input}
          value={text}
          onChangeText={setText}
          placeholder={peerReachable ? 'Message…' : 'Peer unreachable'}
          placeholderTextColor="#999"
          onSubmitEditing={handleSend}
          returnKeyType="send"
          editable={peerReachable}
        />
        <TouchableOpacity
          style={[styles.sendBtn, !peerReachable && styles.sendBtnDisabled]}
          onPress={handleSend}
          disabled={!peerReachable}
        >
          <Text style={styles.sendBtnText}>Send</Text>
        </TouchableOpacity>
      </View>
    </View>
  )
}

function MessageBubble({ msg, isMine }: { msg: StoredMessage; isMine: boolean }) {
  return (
    <View style={[styles.bubble, isMine ? styles.bubbleMine : styles.bubbleTheirs]}>
      <Text style={styles.bubbleText}>{msg.text}</Text>
      <Text style={styles.bubbleMeta}>
        {new Date(msg.timestamp).toLocaleTimeString()}
        {isMine && (msg.delivered ? ' ✓✓' : ' ✓')}
      </Text>
    </View>
  )
}

// ─── 4. New Group Screen ──────────────────────────────────────────────────────

function NewGroupScreen({ peers, onCreated, onCancel }: {
  peers: Peer[]
  onCreated: (g: Group) => void
  onCancel: () => void
}) {
  const engine = useEngine()
  const [name, setName] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const toggle = (id: string) => {
    const next = new Set(selected)
    next.has(id) ? next.delete(id) : next.add(id)
    setSelected(next)
  }

  const create = async () => {
    if (!engine || !name.trim() || selected.size === 0) return
    const group = await engine.groupChat.createGroup(name.trim(), Array.from(selected))
    onCreated(group)
  }

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>New Group</Text>
      <TextInput
        style={styles.input}
        placeholder="Group name"
        value={name}
        onChangeText={setName}
      />
      <Text style={styles.hint}>Select members:</Text>
      {peers.map((p) => (
        <TouchableOpacity key={p.id} style={styles.peerRow} onPress={() => toggle(p.id)}>
          <Text>{selected.has(p.id) ? '✅' : '⬜'} {p.displayName}</Text>
        </TouchableOpacity>
      ))}
      <TouchableOpacity style={styles.sendBtn} onPress={create}>
        <Text style={styles.sendBtnText}>Create</Text>
      </TouchableOpacity>
      <TouchableOpacity onPress={onCancel}>
        <Text style={styles.back}>Cancel</Text>
      </TouchableOpacity>
    </View>
  )
}

// ─── 5. Group Chat Screen ─────────────────────────────────────────────────────

function GroupChatScreen({ group, onBack }: { group: Group; onBack: () => void }) {
  const engine = useEngine()
  const { messages, send, markRead, leave, rename } = useGroupChat(engine, group.id)
  const [text, setText] = useState('')

  useEffect(() => { markRead() }, [])

  const handleSend = async () => {
    const trimmed = text.trim()
    if (!trimmed) return
    setText('')
    try { await send(trimmed) } catch (err) {
      Alert.alert('Send failed', (err as Error).message)
    }
  }

  return (
    <View style={styles.screen}>
      <View style={styles.header}>
        <TouchableOpacity onPress={onBack}>
          <Text style={styles.back}>← Back</Text>
        </TouchableOpacity>
        <Text style={styles.title}>{group.name}</Text>
        <Text style={styles.subtitle}>{group.members.length} members</Text>
      </View>
      <FlatList
        data={messages}
        keyExtractor={(m) => m.msgId}
        renderItem={({ item }) => (
          <MessageBubble msg={item} isMine={item.from === engine?.selfId} />
        )}
        contentContainerStyle={{ padding: 12 }}
      />
      <View style={styles.inputRow}>
        <TextInput
          style={styles.input}
          value={text}
          onChangeText={setText}
          placeholder="Group message…"
          onSubmitEditing={handleSend}
          returnKeyType="send"
        />
        <TouchableOpacity style={styles.sendBtn} onPress={handleSend}>
          <Text style={styles.sendBtnText}>Send</Text>
        </TouchableOpacity>
      </View>
    </View>
  )
}

// ─── 6. Radar Screen ─────────────────────────────────────────────────────────

function RadarScreen({ peer }: { peer: Peer }) {
  const engine = useEngine()
  const { range, locating, rttMs, locate, ping } = useDeviceLocator(engine, peer.id)

  return (
    <View style={styles.screen}>
      <Text style={styles.title}>Device Locator</Text>
      <Text style={styles.subtitle}>{peer.displayName}</Text>

      <View style={styles.rangeCard}>
        <Text style={styles.rangeValue}>
          {range?.estimatedDistanceM != null
            ? `~${range.estimatedDistanceM.toFixed(2)} m`
            : 'Unknown'}
        </Text>
        <Text style={styles.rangeLabel}>Estimated distance</Text>

        <Text style={styles.rangeValue}>
          {range?.localRssi != null ? `${range.localRssi.toFixed(0)} dBm` : '--'}
        </Text>
        <Text style={styles.rangeLabel}>Local RSSI</Text>

        <Text style={styles.rangeValue}>
          {range?.peerRssi != null ? `${range.peerRssi.toFixed(0)} dBm` : '--'}
        </Text>
        <Text style={styles.rangeLabel}>Peer RSSI</Text>

        <Text style={styles.rangeValue}>
          {rttMs != null ? `${rttMs} ms` : '--'}
        </Text>
        <Text style={styles.rangeLabel}>Round-trip time</Text>
      </View>

      <TouchableOpacity style={styles.sendBtn} onPress={locate} disabled={locating}>
        <Text style={styles.sendBtnText}>{locating ? 'Ranging…' : 'Active Range'}</Text>
      </TouchableOpacity>

      <TouchableOpacity style={[styles.sendBtn, { marginTop: 8 }]} onPress={ping}>
        <Text style={styles.sendBtnText}>Ping</Text>
      </TouchableOpacity>
    </View>
  )
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** In-memory AsyncStorage shim for environments without the real package. */
function makeInMemoryAsyncStorage() {
  const store = new Map<string, string>()
  return {
    async getItem(key: string) { return store.get(key) ?? null },
    async setItem(key: string, value: string) { store.set(key, value) },
    async removeItem(key: string) { store.delete(key) },
  }
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: '#fff' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  header: { padding: 16, borderBottomWidth: 1, borderBottomColor: '#eee' },
  title: { fontSize: 20, fontWeight: '700' },
  subtitle: { fontSize: 13, color: '#888', marginTop: 2 },
  hint: { fontSize: 14, color: '#999', textAlign: 'center', marginTop: 8 },
  error: { fontSize: 14, color: '#c44', textAlign: 'center', padding: 20 },
  back: { fontSize: 15, color: '#007AFF', marginBottom: 4 },
  peerRow: { flexDirection: 'row', alignItems: 'center', padding: 14, borderBottomWidth: StyleSheet.hairlineWidth, borderBottomColor: '#eee' },
  peerInfo: { flex: 1 },
  peerName: { fontSize: 16, fontWeight: '500' },
  peerMeta: { fontSize: 12, color: '#888', marginTop: 2 },
  dot: { width: 10, height: 10, borderRadius: 5, marginRight: 12 },
  presenceRow: { flexDirection: 'row', padding: 8, gap: 8 },
  statusBtn: { paddingHorizontal: 14, paddingVertical: 6, borderRadius: 16, backgroundColor: '#eee' },
  statusBtnActive: { backgroundColor: '#007AFF' },
  statusBtnText: { fontSize: 13, color: '#000' },
  bubble: { maxWidth: '75%', borderRadius: 16, padding: 10, marginVertical: 4 },
  bubbleMine: { alignSelf: 'flex-end', backgroundColor: '#007AFF' },
  bubbleTheirs: { alignSelf: 'flex-start', backgroundColor: '#eee' },
  bubbleText: { fontSize: 15, color: '#fff' },
  bubbleMeta: { fontSize: 10, color: 'rgba(255,255,255,0.7)', marginTop: 2, textAlign: 'right' },
  inputRow: { flexDirection: 'row', padding: 8, borderTopWidth: 1, borderTopColor: '#eee' },
  input: { flex: 1, borderRadius: 20, backgroundColor: '#f5f5f5', paddingHorizontal: 14, paddingVertical: 8, marginRight: 8 },
  sendBtn: { backgroundColor: '#007AFF', borderRadius: 20, paddingHorizontal: 18, paddingVertical: 10, justifyContent: 'center' },
  sendBtnDisabled: { backgroundColor: '#aaa' },
  sendBtnText: { color: '#fff', fontWeight: '600' },
  fab: { position: 'absolute', bottom: 24, right: 24, backgroundColor: '#007AFF', borderRadius: 28, paddingHorizontal: 20, paddingVertical: 12 },
  fabText: { color: '#fff', fontWeight: '700', fontSize: 15 },
  rangeCard: { margin: 20, padding: 20, borderRadius: 16, backgroundColor: '#f5f5f5' },
  rangeValue: { fontSize: 32, fontWeight: '700', textAlign: 'center', marginTop: 12 },
  rangeLabel: { fontSize: 12, color: '#888', textAlign: 'center', marginBottom: 4 },
})