/**
 * MeshDemoScreen.tsx
 *
 * Drop this file anywhere in your React Native project and register it
 * as a screen in your navigator. It imports from the mesh core and runs
 * the real BLE engine on-device.
 *
 * Prerequisites (install these if you haven't):
 *   npm install tweetnacl tweetnacl-util @noble/hashes
 *
 * Usage in your navigator:
 *   import MeshDemoScreen from './MeshDemoScreen'
 *   <Stack.Screen name="MeshDemo" component={MeshDemoScreen} />
 *
 * IMPORTANT: Requires a physical device or a simulator with BLE support.
 * Will NOT work in Expo Go — you need a development build.
 * Run: npx expo run:ios  OR  npx expo run:android
 */
import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react';
import {
  Animated,
  Easing,
  Platform,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';

// ─── Import from your mesh core ─────────────────────────────────────────────
// Adjust this path to wherever you placed the src/mesh/ folder.
import {
  ContentType,
  generateIdentityKeyPair,
  MeshMessage,
  NearbyPeer,
  useMesh,
  utf8Decode,
  utf8Encode
} from '../mesh/index';

// ─── One-time key pair ──────────────────────────────────────────────────────
// In a real app, persist this securely (e.g. react-native-keychain).
// For the demo we generate a fresh one on each app launch.
// const DEMO_KEY_PAIR = generateIdentityKeyPair()

// ─── Types ───────────────────────────────────────────────────────────────────

interface DemoMessage {
  id:      string
  text:    string
  mine:    boolean
  hops?:   number
  parentIds: string[]
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

const toHex = (bytes: Uint8Array) =>
  Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('')

const shortHex = (hex: string, n = 8) => hex.slice(0, n)

const rssiToBars = (rssi: number) =>
  Math.max(1, Math.min(5, Math.round((rssi + 95) / 9) + 1))

const tStr = () =>
  new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })

// ─── Sub-components ──────────────────────────────────────────────────────────

/** 5-bar RSSI indicator */
function RSSIBars({ rssi }: { rssi: number }) {
  const bars = rssiToBars(rssi)
  return (
    <View style={styles.rssiBars}>
      {[1, 2, 3, 4, 5].map(i => (
        <View
          key={i}
          style={[
            styles.rssiBar,
            { height: 3 + i * 3, backgroundColor: i <= bars ? '#1D9E75' : '#ccc' },
          ]}
        />
      ))}
    </View>
  )
}

/** Animated pulsing dot for "active" states */
function PulseDot({ color }: { color: string }) {
  const scale = useRef(new Animated.Value(1)).current
  useEffect(() => {
    Animated.loop(
      Animated.sequence([
        Animated.timing(scale, { toValue: 1.5, duration: 600, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
        Animated.timing(scale, { toValue: 1,   duration: 600, easing: Easing.inOut(Easing.ease), useNativeDriver: true }),
      ])
    ).start()
  }, [scale])
  return (
    <Animated.View style={[styles.pulseDot, { backgroundColor: color, transform: [{ scale }] }]} />
  )
}

/** Horizontal progress bar */
function ProgressBar({ progress, color }: { progress: number; color: string }) {
  return (
    <View style={styles.progressTrack}>
      <View style={[styles.progressFill, { width: `${Math.round(progress * 100)}%`, backgroundColor: color }]} />
    </View>
  )
}

/** Packet type badge */
function PktBadge({ type }: { type: string }) {
  const map: Record<string, { bg: string; fg: string }> = {
    DATA:      { bg: '#E6F1FB', fg: '#185FA5' },
    ACK:       { bg: '#E1F5EE', fg: '#0F6E56' },
    RELAY:     { bg: '#FAECE7', fg: '#993C1D' },
    HANDSHAKE: { bg: '#EEEDFE', fg: '#3C3489' },
    BEACON:    { bg: '#F1EFE8', fg: '#5F5E5A' },
  }
  const c = map[type] ?? { bg: '#eee', fg: '#555' }
  return (
    <View style={[styles.badge, { backgroundColor: c.bg }]}>
      <Text style={[styles.badgeText, { color: c.fg }]}>{type.slice(0, 4)}</Text>
    </View>
  )
}

/** Log row (events + packet log) */
function LogRow({ ts, text, dim }: { ts: string; text: string; dim?: boolean }) {
  return (
    <View style={styles.logRow}>
      <Text style={styles.logTs}>{ts}</Text>
      <Text style={[styles.logText, dim && { color: '#999' }]} numberOfLines={1}>{text}</Text>
    </View>
  )
}

// ─── Main Screen ─────────────────────────────────────────────────────────────

export default function MeshDemoScreen() {
  const DEMO_KEY_PAIR = generateIdentityKeyPair()
  // ── Engine hook ─────────────────────────────────────────────────────────────
  const { engine, status, nearbyPeers, nearbyContacts } = useMesh(DEMO_KEY_PAIR, {
    defaultTTL:         7,
    enableBackground:   false,
    enableMultipeer:    Platform.OS === 'ios',
    advertisingPhaseMs: 800,
    scanningPhaseMs:    1200,
  })

  // ── Local demo state ────────────────────────────────────────────────────────
  const [activeTab,   setActiveTab]   = useState<'radio' | 'messages' | 'debug'>('radio')
  const [compose,     setCompose]     = useState('')
  const [messages,    setMessages]    = useState<DemoMessage[]>([])
  const [eventLog,    setEventLog]    = useState<{ ts: string; text: string }[]>([])
  const [packetLog,   setPacketLog]   = useState<{ ts: string; type: string; id: string; ttl: number; hops: number }[]>([])
  const [showDAG,     setShowDAG]     = useState(false)
  const [stats, setStats] = useState({ tx: 0, rx: 0, relay: 0, ack: 0 })

  // ── Derived identity ─────────────────────────────────────────────────────────
  const ownPubKeyHex = toHex(DEMO_KEY_PAIR.publicKey)
  const ownFingerprint = shortHex(ownPubKeyHex, 16)

  // ── Event log helper ─────────────────────────────────────────────────────────
  const logEvent = useCallback((text: string) => {
    setEventLog(prev => [{ ts: tStr(), text }, ...prev].slice(0, 20))
  }, [])

  const logPacket = useCallback((type: string, ttl: number, hops: number) => {
    const id = Math.random().toString(36).slice(2, 10)
    setPacketLog(prev => [{ ts: tStr(), type, id, ttl, hops }, ...prev].slice(0, 15))
  }, [])

  // ── Wire engine events ────────────────────────────────────────────────────────
  useEffect(() => {
    if (!engine) return
    const subs: (() => void)[] = []

    subs.push(engine.on('message', (msg: MeshMessage) => {
      let text = ''
      try { text = utf8Decode(msg.content) } catch { text = '[binary]' }
      setMessages(prev => [
        ...prev,
        { id: msg.id, text, mine: false, hops: msg.hopCount, parentIds: msg.parentIds },
      ])
      setStats(s => ({ ...s, rx: s.rx + 1 }))
      logEvent(`Message from ${msg.senderId.slice(0, 8)} (${msg.hopCount} hops)`)
      logPacket('DATA', 7 - msg.hopCount, msg.hopCount)
    }))

    subs.push(engine.on('peer:discovered', (peer: NearbyPeer) => {
      logEvent(`Peer found: ${peer.deviceId.slice(0, 8)} via ${peer.transport}`)
    }))

    subs.push(engine.on('peer:connected', ({ deviceId, transport }) => {
      logEvent(`Connected: ${deviceId.slice(0, 8)} [${transport}]`)
      logPacket('HANDSHAKE', 0, 0)
    }))

    subs.push(engine.on('peer:disconnected', ({ deviceId }) => {
      logEvent(`Disconnected: ${deviceId.slice(0, 8)}`)
    }))

    subs.push(engine.on('contact:nearby', ({ contact }) => {
      logEvent(`Contact nearby: ${contact.alias ?? contact.id.slice(0, 8)}`)
    }))

    subs.push(engine.on('ack:received', ({ packetId }) => {
      setStats(s => ({ ...s, ack: s.ack + 1 }))
      logEvent(`ACK: ${packetId.slice(0, 8)} ✓`)
      logPacket('ACK', 0, 0)
    }))

    subs.push(engine.on('packet:relayed', ({ packetId, toPeer }) => {
      setStats(s => ({ ...s, relay: s.relay + 1 }))
      logEvent(`Relayed ${packetId.slice(0, 6)} → ${toPeer.slice(0, 8)}`)
      logPacket('RELAY', 0, 0)
    }))

    subs.push(engine.on('handshake:completed', ({ contactId }) => {
      logEvent(`Handshake: ${contactId.slice(0, 8)}`)
    }))

    subs.push(engine.on('radio:phase', ({ phase }) => {
      logEvent(`Radio → ${phase}`)
      if (phase === 'advertising') logPacket('BEACON', 0, 0)
    }))

    subs.push(engine.on('error', ({ code, message }) => {
      logEvent(`Error [${code}]: ${message}`)
    }))

    return () => { subs.forEach(fn => fn()) }
  }, [engine, logEvent, logPacket])

  // ── Send message ─────────────────────────────────────────────────────────────
  const handleSend = useCallback(async () => {
    if (!engine || !compose.trim() || !status.running) return

    const contacts = engine.getContacts()
    if (contacts.length === 0) {
      logEvent('No contacts — add one via addContactFromPublicKey()')
      return
    }

    // Send to all contacts for the demo
    const text = compose.trim()
    const parentIds = messages.length > 0 ? [messages[messages.length - 1].id] : []
    const msgId = Math.random().toString(36).slice(2, 14)

    setMessages(prev => [...prev, { id: msgId, text, mine: true, hops: 0, parentIds }])
    setCompose('')
    setStats(s => ({ ...s, tx: s.tx + 1 }))
    logPacket('DATA', 7, 0)
    logEvent(`Sent: "${text.slice(0, 30)}"`)

    for (const contact of contacts) {
      try {
        await engine.sendMessage(contact.id, ContentType.TEXT, utf8Encode(text))
      } catch (err) {
        logEvent(`Send failed to ${contact.id.slice(0, 8)}: ${String(err)}`)
      }
    }
  }, [engine, compose, messages, status.running, logEvent, logPacket])

  // ── Add demo contact ──────────────────────────────────────────────────────────
  const addDemoContact = useCallback(() => {
    if (!engine) return
    // Generate a fake remote key pair to simulate adding a contact
    const theirKP = generateIdentityKeyPair()
    const contact = engine.addContactFromPublicKey(theirKP.publicKey, 'Test Contact')
    logEvent(`Contact added: ${contact.alias} (${contact.id.slice(0, 8)})`)
  }, [engine, logEvent])

  // ── Causal DAG: compute parent lines ─────────────────────────────────────────
  const idxMap: Record<string, number> = {}
  messages.forEach((m, i) => { idxMap[m.id] = i })

  // ─── Render ────────────────────────────────────────────────────────────────

  const phaseColor = status.radioPhase === 'advertising' ? '#EF9F27'
                   : status.radioPhase === 'scanning'    ? '#378ADD'
                   : '#999'

  return (
    <View style={styles.screen}>

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <View style={styles.header}>
        <View style={styles.headerLeft}>
          <Text style={styles.headerTitle}>Mesh demo</Text>
          <Text style={styles.headerSub} numberOfLines={1}>fp:{ownFingerprint}</Text>
        </View>
        <TouchableOpacity
          onPress={() => {}}
          style={[styles.engineBtn, status.running ? styles.engineBtnStop : styles.engineBtnStart]}
          disabled  // engine is managed by useMesh hook — start/stop via init() / stop()
        >
          <View style={styles.engineBtnInner}>
            {status.running && <PulseDot color="#0F6E56" />}
            <Text style={[styles.engineBtnText, { color: status.running ? '#0F6E56' : '#185FA5' }]}>
              {status.running ? 'Running' : status.error ? 'Error' : 'Starting…'}
            </Text>
          </View>
        </TouchableOpacity>
      </View>

      {/* ── Error banner ───────────────────────────────────────────────── */}
      {status.error && (
        <View style={styles.errorBanner}>
          <Text style={styles.errorText}>⚠ {status.error}</Text>
        </View>
      )}

      {/* ── Tab bar ────────────────────────────────────────────────────── */}
      <View style={styles.tabBar}>
        {(['radio', 'messages', 'debug'] as const).map(tab => (
          <TouchableOpacity
            key={tab}
            style={[styles.tab, activeTab === tab && styles.tabActive]}
            onPress={() => setActiveTab(tab)}
          >
            <Text style={[styles.tabText, activeTab === tab && styles.tabTextActive]}>
              {tab.charAt(0).toUpperCase() + tab.slice(1)}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* ══ TAB: RADIO ═══════════════════════════════════════════════════ */}
      {activeTab === 'radio' && (
        <ScrollView style={styles.content} contentContainerStyle={styles.contentInner}>

          {/* Radio state */}
          <View style={styles.card}>
            <Text style={styles.cardLabel}>Radio phase</Text>
            <View style={styles.phaseRow}>
              {status.running && <PulseDot color={phaseColor} />}
              <Text style={[styles.phaseText, { color: phaseColor }]}>
                {status.radioPhase?.toUpperCase() ?? 'IDLE'}
              </Text>
            </View>
            <Text style={styles.cardSub}>
              Advertising (800ms) → Scanning (1200ms) → repeat{'\n'}
              Tie-breaker: device with higher token hash acts as Central
            </Text>
          </View>

          {/* Own token */}
          <View style={styles.card}>
            <Text style={styles.cardLabel}>Rotating token</Text>
            <View style={styles.tokenRow}>
              <Text style={styles.mono}>SHA-256(token)[0:8] in all headers</Text>
            </View>
            <Text style={styles.cardSub}>
              Rotates every 15 min. Contacts derive your expected token from{'\n'}
              HMAC-SHA256(sharedRootKey, window). Strangers see random noise.
            </Text>
          </View>

          {/* Stats row */}
          <View style={styles.statsRow}>
            {[
              { label: 'Sent',    value: stats.tx,    color: '#185FA5' },
              { label: 'Received', value: stats.rx,   color: '#0F6E56' },
              { label: 'Relayed', value: stats.relay, color: '#993C1D' },
              { label: 'ACKs',    value: stats.ack,   color: '#3C3489' },
            ].map(s => (
              <View key={s.label} style={styles.statChip}>
                <Text style={styles.statLabel}>{s.label}</Text>
                <Text style={[styles.statValue, { color: s.color }]}>{s.value}</Text>
              </View>
            ))}
          </View>

          {/* Nearby peers */}
          <View style={styles.card}>
            <Text style={styles.cardLabel}>Nearby peers ({nearbyPeers.length})</Text>
            {nearbyPeers.length === 0 ? (
              <Text style={styles.empty}>Scanning for peers…</Text>
            ) : nearbyPeers.map(peer => {
              const knownContact = nearbyContacts.find(nc => nc.deviceId === peer.deviceId)
              return (
                <View key={peer.deviceId} style={styles.peerRow}>
                  <View style={styles.peerInfo}>
                    <Text style={[styles.peerName, knownContact && { color: '#1D9E75' }]}>
                      {knownContact ? `● ${knownContact.contact.alias ?? knownContact.contact.id.slice(0, 8)}` : `○ ${peer.deviceId.slice(0, 12)}`}
                    </Text>
                    <Text style={styles.peerMeta}>
                      {peer.transport}  {peer.rssi != null ? `${peer.rssi} dBm` : '—'}
                      {peer.tokenHash ? `  tok:${shortHex(peer.tokenHash, 8)}` : ''}
                    </Text>
                  </View>
                  {peer.rssi != null && <RSSIBars rssi={peer.rssi} />}
                </View>
              )
            })}
          </View>

          {/* Contacts */}
          <View style={styles.card}>
            <View style={styles.cardTitleRow}>
              <Text style={styles.cardLabel}>Contacts ({engine?.getContacts().length ?? 0})</Text>
              <TouchableOpacity onPress={addDemoContact} style={styles.smallBtn}>
                <Text style={styles.smallBtnText}>+ Add demo contact</Text>
              </TouchableOpacity>
            </View>
            {(engine?.getContacts() ?? []).length === 0 ? (
              <Text style={styles.cardSub}>
                No contacts yet. In production, add contacts via QR code:{'\n'}
                engine.addContactFromPublicKey(theirPublicKey, 'Alice')
              </Text>
            ) : (engine?.getContacts() ?? []).map(c => (
              <View key={c.id} style={styles.contactRow}>
                <Text style={styles.contactName}>{c.alias ?? c.id.slice(0, 8)}</Text>
                <Text style={styles.contactFp} numberOfLines={1}>fp:{c.id}</Text>
              </View>
            ))}
          </View>

        </ScrollView>
      )}

      {/* ══ TAB: MESSAGES ════════════════════════════════════════════════ */}
      {activeTab === 'messages' && (
        <View style={styles.content}>

          {/* DAG / Bubbles toggle */}
          <View style={styles.dagToggleRow}>
            <Text style={styles.dagToggleLabel}>Causal DAG view</Text>
            <Switch value={showDAG} onValueChange={setShowDAG} />
          </View>

          <ScrollView style={styles.messageList} contentContainerStyle={styles.messageListInner}>
            {messages.length === 0 ? (
              <Text style={styles.empty}>No messages yet.{'\n'}Add a contact and tap Send.</Text>
            ) : showDAG ? (
              /* ── DAG View ──────────────────────────────────────── */
              <View>
                <Text style={styles.dagLabel}>
                  Each node = one message. Arrows = causal parent references.{'\n'}
                  Nodes in the same column are concurrent (no causal link).
                </Text>
                {messages.map((msg, i) => (
                  <View key={msg.id} style={styles.dagNode}>
                    <View style={[styles.dagCircle, { backgroundColor: msg.mine ? '#378ADD' : '#F1EFE8' }]}>
                      <Text style={[styles.dagCircleText, { color: msg.mine ? '#fff' : '#444' }]}>
                        {msg.id.slice(0, 5)}
                      </Text>
                    </View>
                    <View style={styles.dagMeta}>
                      <Text style={styles.dagMsgText} numberOfLines={1}>{msg.text}</Text>
                      {msg.parentIds.length > 0 && (
                        <Text style={styles.dagParent}>
                          ⤴ parent: {msg.parentIds[0].slice(0, 8)}
                          {idxMap[msg.parentIds[0]] !== undefined ? ` (msg ${idxMap[msg.parentIds[0]] + 1})` : ' (unseen)'}
                        </Text>
                      )}
                      {msg.hops != null && msg.hops > 0 && (
                        <Text style={styles.dagParent}>{msg.hops} relay hop{msg.hops !== 1 ? 's' : ''}</Text>
                      )}
                    </View>
                  </View>
                ))}
              </View>
            ) : (
              /* ── Bubble View ───────────────────────────────────── */
              messages.map(msg => (
                <View key={msg.id} style={[styles.bubble, msg.mine ? styles.bubbleMine : styles.bubbleTheirs]}>
                  <Text style={[styles.bubbleText, msg.mine && styles.bubbleTextMine]}>
                    {msg.text}
                  </Text>
                  <View style={styles.bubbleMeta}>
                    {msg.hops != null && msg.hops > 0 && (
                      <Text style={styles.bubbleMetaText}>{msg.hops} hop{msg.hops !== 1 ? 's' : ''}</Text>
                    )}
                    {msg.parentIds.length > 0 && (
                      <Text style={styles.bubbleMetaText}>⤴ {msg.parentIds[0].slice(0, 6)}</Text>
                    )}
                  </View>
                </View>
              ))
            )}
          </ScrollView>

          {/* Encryption preview while composing */}
          {compose.length > 0 && (
            <View style={styles.encryptPreview}>
              <Text style={styles.encryptLabel}>Header (plaintext):</Text>
              <Text style={styles.encryptMono}>ver=01  type=DATA  ttl=7  hops=0</Text>
              <Text style={styles.encryptLabel}>Payload (XSalsa20-Poly1305):</Text>
              <Text style={styles.encryptMono} numberOfLines={1}>
                [nonce:24B][ciphertext:encrypted]…
              </Text>
            </View>
          )}

          {/* Compose bar */}
          <View style={styles.composeBar}>
            <TextInput
              style={styles.composeInput}
              value={compose}
              onChangeText={setCompose}
              placeholder={status.running ? 'Message…' : 'Start engine first'}
              editable={status.running}
              returnKeyType="send"
              onSubmitEditing={handleSend}
            />
            <TouchableOpacity
              onPress={handleSend}
              disabled={!status.running || !compose.trim()}
              style={[styles.sendBtn, (!status.running || !compose.trim()) && styles.sendBtnDisabled]}
            >
              <Text style={styles.sendBtnText}>Send</Text>
            </TouchableOpacity>
          </View>
        </View>
      )}

      {/* ══ TAB: DEBUG ═══════════════════════════════════════════════════ */}
      {activeTab === 'debug' && (
        <ScrollView style={styles.content} contentContainerStyle={styles.contentInner}>

          {/* Packet log */}
          <View style={styles.card}>
            <Text style={styles.cardLabel}>Packet log</Text>
            {packetLog.length === 0 ? (
              <Text style={styles.empty}>No packets yet</Text>
            ) : packetLog.map((p, i) => (
              <View key={i} style={styles.pktRow}>
                <PktBadge type={p.type} />
                <View style={styles.pktMeta}>
                  <Text style={styles.pktId}>{p.id}</Text>
                  <Text style={styles.pktDetail}>ttl:{p.ttl}  hops:{p.hops}</Text>
                </View>
                <Text style={styles.pktTs}>{p.ts}</Text>
              </View>
            ))}
          </View>

          {/* Event log */}
          <View style={styles.card}>
            <Text style={styles.cardLabel}>Engine events</Text>
            {eventLog.length === 0 ? (
              <Text style={styles.empty}>No events yet</Text>
            ) : eventLog.map((ev, i) => (
              <LogRow key={i} ts={ev.ts} text={ev.text} />
            ))}
          </View>

          {/* Identity info */}
          <View style={styles.card}>
            <Text style={styles.cardLabel}>Identity</Text>
            <Text style={styles.mono}>Public key (share via QR):</Text>
            <Text style={[styles.mono, styles.monoSmall]} numberOfLines={2}>{ownPubKeyHex}</Text>
            <View style={styles.divider} />
            <Text style={styles.mono}>Fingerprint:</Text>
            <Text style={[styles.mono, { color: '#185FA5' }]}>{ownFingerprint}</Text>
          </View>

          {/* Protocol summary */}
          <View style={styles.card}>
            <Text style={styles.cardLabel}>Protocol summary</Text>
            {[
              ['Service UUID',       'a1b2c3d4-e5f6-7890-abcd-ef1234567890'],
              ['CHAR_ANNOUNCE',      '…0001…  read + notify (16 B)'],
              ['CHAR_INBOX',         '…0002…  write (chunked DATA)'],
              ['CHAR_RELAY',         '…0003…  write (epidemic relay)'],
              ['CHAR_ACK',           '…0004…  notify (delivery ACK)'],
              ['Routing header',     '36 bytes, always plaintext'],
              ['Encryption',         'X25519 + XSalsa20-Poly1305'],
              ['Token rotation',     '15 min windows, HMAC-SHA256'],
              ['MTU (Android)',      '247 bytes, negotiated on connect'],
              ['Chunk payload',      '241 bytes / BLE write'],
              ['Routing',            'Epidemic + spray-and-wait (N=4)'],
              ['Causal ordering',    'DAG, Kahn topological sort'],
            ].map(([k, v]) => (
              <View key={k} style={styles.infoRow}>
                <Text style={styles.infoKey}>{k}</Text>
                <Text style={styles.infoVal} numberOfLines={1}>{v}</Text>
              </View>
            ))}
          </View>

        </ScrollView>
      )}
    </View>
  )
}

// ─── Styles ──────────────────────────────────────────────────────────────────

const CLR = {
  bg:      '#fff',
  surface: '#f7f7f5',
  border:  '#e8e6e0',
  text:    '#1a1a1a',
  sub:     '#666',
  dim:     '#999',
}

const styles = StyleSheet.create({
  screen:          { flex: 1, backgroundColor: CLR.bg },

  // Header
  header:          { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', padding: 16, borderBottomWidth: 0.5, borderBottomColor: CLR.border },
  headerLeft:      { flex: 1, marginRight: 12 },
  headerTitle:     { fontSize: 17, fontWeight: '500', color: CLR.text },
  headerSub:       { fontSize: 11, color: CLR.sub, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', marginTop: 2 },
  engineBtn:       { borderRadius: 8, paddingVertical: 6, paddingHorizontal: 14, borderWidth: 0.5 },
  engineBtnStart:  { backgroundColor: '#E6F1FB', borderColor: '#B5D4F4' },
  engineBtnStop:   { backgroundColor: '#E1F5EE', borderColor: '#9FE1CB' },
  engineBtnInner:  { flexDirection: 'row', alignItems: 'center', gap: 6 },
  engineBtnText:   { fontSize: 13, fontWeight: '500' },

  errorBanner:     { backgroundColor: '#FCEBEB', borderBottomWidth: 0.5, borderBottomColor: '#F7C1C1', padding: 10 },
  errorText:       { color: '#A32D2D', fontSize: 12 },

  // Tab bar
  tabBar:          { flexDirection: 'row', borderBottomWidth: 0.5, borderBottomColor: CLR.border },
  tab:             { flex: 1, paddingVertical: 10, alignItems: 'center' },
  tabActive:       { borderBottomWidth: 2, borderBottomColor: '#378ADD' },
  tabText:         { fontSize: 13, color: CLR.sub },
  tabTextActive:   { color: '#185FA5', fontWeight: '500' },

  // Content
  content:         { flex: 1 },
  contentInner:    { padding: 14, gap: 12 },

  // Cards
  card:            { backgroundColor: CLR.bg, borderRadius: 12, borderWidth: 0.5, borderColor: CLR.border, padding: 14 },
  cardLabel:       { fontSize: 11, fontWeight: '500', color: CLR.sub, textTransform: 'uppercase', letterSpacing: 0.6, marginBottom: 10 },
  cardSub:         { fontSize: 12, color: CLR.sub, lineHeight: 18, marginTop: 8 },
  cardTitleRow:    { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 },
  empty:           { fontSize: 12, color: CLR.dim, textAlign: 'center', paddingVertical: 12 },
  divider:         { height: 0.5, backgroundColor: CLR.border, marginVertical: 10 },

  // Phase
  phaseRow:        { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 4 },
  phaseText:       { fontSize: 18, fontWeight: '500', fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },

  // Token
  tokenRow:        { marginBottom: 4 },
  mono:            { fontSize: 12, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', color: CLR.text },
  monoSmall:       { fontSize: 10, color: CLR.sub, marginTop: 4, lineHeight: 16 },

  // Stats
  statsRow:        { flexDirection: 'row', gap: 8 },
  statChip:        { flex: 1, backgroundColor: CLR.surface, borderRadius: 8, padding: 10, alignItems: 'center' },
  statLabel:       { fontSize: 10, color: CLR.sub, marginBottom: 4 },
  statValue:       { fontSize: 22, fontWeight: '500', fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },

  // Peers
  peerRow:         { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingVertical: 8, borderBottomWidth: 0.5, borderBottomColor: CLR.border },
  peerInfo:        { flex: 1 },
  peerName:        { fontSize: 13, fontWeight: '500', color: CLR.text, marginBottom: 2 },
  peerMeta:        { fontSize: 10, color: CLR.dim, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },

  // RSSI bars
  rssiBars:        { flexDirection: 'row', alignItems: 'flex-end', gap: 2, height: 16 },
  rssiBar:         { width: 4, borderRadius: 1 },

  // Contacts
  contactRow:      { paddingVertical: 8, borderBottomWidth: 0.5, borderBottomColor: CLR.border },
  contactName:     { fontSize: 13, fontWeight: '500', color: CLR.text, marginBottom: 2 },
  contactFp:       { fontSize: 10, color: CLR.dim, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },

  // Buttons
  smallBtn:        { backgroundColor: CLR.surface, borderRadius: 6, paddingVertical: 4, paddingHorizontal: 10, borderWidth: 0.5, borderColor: CLR.border },
  smallBtnText:    { fontSize: 12, color: '#185FA5' },

  // Messages
  dagToggleRow:    { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', padding: 12, borderBottomWidth: 0.5, borderBottomColor: CLR.border },
  dagToggleLabel:  { fontSize: 13, color: CLR.text },
  messageList:     { flex: 1 },
  messageListInner:{ padding: 14, gap: 10 },

  // Bubbles
  bubble:          { maxWidth: '80%', padding: 10, borderRadius: 12 },
  bubbleMine:      { alignSelf: 'flex-end', backgroundColor: '#378ADD', borderBottomRightRadius: 4 },
  bubbleTheirs:    { alignSelf: 'flex-start', backgroundColor: CLR.surface, borderBottomLeftRadius: 4 },
  bubbleText:      { fontSize: 14, color: CLR.text, lineHeight: 20 },
  bubbleTextMine:  { color: '#fff' },
  bubbleMeta:      { flexDirection: 'row', gap: 8, marginTop: 3 },
  bubbleMetaText:  { fontSize: 10, color: 'rgba(255,255,255,0.7)', fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },

  // DAG
  dagLabel:        { fontSize: 11, color: CLR.sub, lineHeight: 17, marginBottom: 12 },
  dagNode:         { flexDirection: 'row', alignItems: 'center', gap: 12, marginBottom: 12 },
  dagCircle:       { width: 48, height: 48, borderRadius: 24, alignItems: 'center', justifyContent: 'center', flexShrink: 0 },
  dagCircleText:   { fontSize: 10, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', fontWeight: '500' },
  dagMeta:         { flex: 1 },
  dagMsgText:      { fontSize: 13, color: CLR.text },
  dagParent:       { fontSize: 10, color: CLR.dim, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', marginTop: 2 },

  // Encryption preview
  encryptPreview:  { margin: 10, padding: 10, backgroundColor: CLR.surface, borderRadius: 8 },
  encryptLabel:    { fontSize: 10, color: CLR.sub, marginBottom: 2 },
  encryptMono:     { fontSize: 10, color: CLR.dim, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', marginBottom: 6 },

  // Compose
  composeBar:      { flexDirection: 'row', gap: 8, padding: 10, borderTopWidth: 0.5, borderTopColor: CLR.border },
  composeInput:    { flex: 1, borderWidth: 0.5, borderColor: CLR.border, borderRadius: 8, paddingHorizontal: 12, paddingVertical: 8, fontSize: 14 },
  sendBtn:         { backgroundColor: '#378ADD', borderRadius: 8, paddingHorizontal: 16, alignItems: 'center', justifyContent: 'center' },
  sendBtnDisabled: { backgroundColor: '#ccc' },
  sendBtnText:     { color: '#fff', fontSize: 14, fontWeight: '500' },

  // Packet log
  pktRow:          { flexDirection: 'row', alignItems: 'center', gap: 8, paddingVertical: 7, borderBottomWidth: 0.5, borderBottomColor: CLR.border },
  pktMeta:         { flex: 1 },
  pktId:           { fontSize: 11, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', color: CLR.text },
  pktDetail:       { fontSize: 10, color: CLR.dim, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },
  pktTs:           { fontSize: 10, color: CLR.dim },

  // Badge
  badge:           { borderRadius: 4, paddingVertical: 2, paddingHorizontal: 5 },
  badgeText:       { fontSize: 10, fontWeight: '500', fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },

  // Log rows
  logRow:          { flexDirection: 'row', gap: 8, paddingVertical: 5, borderBottomWidth: 0.5, borderBottomColor: CLR.border },
  logTs:           { fontSize: 10, color: CLR.dim, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace', flexShrink: 0 },
  logText:         { flex: 1, fontSize: 12, color: CLR.text },

  // Info rows
  infoRow:         { flexDirection: 'row', paddingVertical: 5, borderBottomWidth: 0.5, borderBottomColor: CLR.border, gap: 8 },
  infoKey:         { fontSize: 11, color: CLR.sub, width: 110, flexShrink: 0 },
  infoVal:         { flex: 1, fontSize: 11, color: CLR.text, fontFamily: Platform.OS === 'ios' ? 'Menlo' : 'monospace' },

  // Pulse dot
  pulseDot:        { width: 8, height: 8, borderRadius: 4 },

  // Progress bar
  progressTrack:   { height: 4, backgroundColor: '#eee', borderRadius: 2, overflow: 'hidden' },
  progressFill:    { height: '100%', borderRadius: 2 },
})