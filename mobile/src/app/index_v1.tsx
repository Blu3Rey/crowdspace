/**
 * @file MeshTestScreen.tsx
 * Drop-in React Native screen that exercises every layer of the mesh engine:
 *   Radio orchestration, token rotation, peer discovery, GATT sessions,
 *   encryption, epidemic relay, causal DAG ordering, and ACK receipts.
 *
 * Drop this file anywhere in your React Native / Expo project and add it
 * to your navigator. It has no other UI dependencies.
 *
 * Prerequisites (already in package.json from the mesh-core setup):
 *   munim-bluetooth, react-native-nitro-modules, tweetnacl,
 *   tweetnacl-util, @noble/hashes
 *
 * Usage:
 *   import MeshTestScreen from './MeshTestScreen'
 *   // Add to your navigator:
 *   <Stack.Screen name="MeshTest" component={MeshTestScreen} />
 */

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import {
  ActivityIndicator,
  Alert,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native'

// ─── Mesh core imports ────────────────────────────────────────────────────────
// Adjust this path to wherever you place the src/mesh/ directory.
import type {
  Contact,
  KeyPair,
  MeshMessage,
  NearbyPeer,
} from '../mesh'
import {
  ContentType,
  generateIdentityKeyPair,
  useMesh,
  utf8Decode,
  utf8Encode,
} from '../mesh'

// ─── Persist the key pair across renders (generate once per session) ──────────
// In production: store this in react-native-keychain or SecureStore.
let _sessionKeyPair: KeyPair | null = null
function getOrCreateKeyPair(): KeyPair {
  if (!_sessionKeyPair) _sessionKeyPair = generateIdentityKeyPair()
  return _sessionKeyPair
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function hexSlice(hex: string, len = 8) {
  return hex.slice(0, len) + '…'
}

function bytesToHex(b: Uint8Array) {
  return Array.from(b).map(x => x.toString(16).padStart(2, '0')).join('')
}

function rssiToSignal(rssi: number): number {
  // 0–4 bars: -50 dBm = 4, -95 dBm = 0
  return Math.max(0, Math.min(4, Math.round((rssi + 95) / 11.25)))
}

function timeStr(): string {
  return new Date().toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

// ─── Sub-components ───────────────────────────────────────────────────────────

/** Row of signal-strength bars */
function SignalBars({ rssi }: { rssi: number }) {
  const bars = rssiToSignal(rssi)
  return (
    <View style={styles.signalRow}>
      {[1, 2, 3, 4].map(i => (
        <View
          key={i}
          style={[
            styles.signalBar,
            { height: 4 + i * 3 },
            i <= bars && styles.signalBarActive,
          ]}
        />
      ))}
    </View>
  )
}

/** Coloured packet type badge */
const PKT_COLORS: Record<string, [string, string]> = {
  DATA:      ['#E6F1FB', '#185FA5'],
  ACK:       ['#E1F5EE', '#0F6E56'],
  RELAY:     ['#FAECE7', '#993C1D'],
  HANDSHAKE: ['#EEEDFE', '#534AB7'],
  BEACON:    ['#F1EFE8', '#5F5E5A'],
}
function PacketBadge({ type }: { type: string }) {
  const [bg, fg] = PKT_COLORS[type] ?? ['#F1EFE8', '#5F5E5A']
  return (
    <View style={[styles.badge, { backgroundColor: bg }]}>
      <Text style={[styles.badgeText, { color: fg }]}>{type.slice(0, 4)}</Text>
    </View>
  )
}

/** Single message bubble */
function MessageBubble({ msg, mine }: { msg: MeshMessage; mine: boolean }) {
  const text = useMemo(() => {
    try { return utf8Decode(msg.content) }
    catch { return `[binary: ${msg.content.length}B]` }
  }, [msg.content])

  return (
    <View style={[styles.bubbleWrap, mine && styles.bubbleWrapMine]}>
      <View style={[styles.bubble, mine ? styles.bubbleMine : styles.bubbleTheirs]}>
        <Text style={[styles.bubbleText, mine && styles.bubbleTextMine]}>{text}</Text>
      </View>
      <View style={styles.bubbleMeta}>
        {msg.hopCount > 0 && (
          <Text style={styles.metaText}>{msg.hopCount} hop{msg.hopCount !== 1 ? 's' : ''}</Text>
        )}
        {msg.parentIds.length > 0 && (
          <Text style={styles.metaText}>⤴ {msg.parentIds[0].slice(0, 6)}</Text>
        )}
        <Text style={styles.metaText}>{hexSlice(msg.id, 6)}</Text>
      </View>
    </View>
  )
}

/** Nearby peer row */
function PeerRow({ peer, contact }: { peer: NearbyPeer; contact?: Contact }) {
  return (
    <View style={styles.peerRow}>
      <View style={styles.peerInfo}>
        <Text style={styles.peerAlias}>
          {contact ? contact.alias ?? contact.id.slice(0, 8) : 'Unknown peer'}
        </Text>
        <Text style={styles.peerSub} numberOfLines={1}>
          {peer.deviceId.slice(0, 12)}  ·  {peer.rssi ?? '?'} dBm  ·  {peer.transport}
        </Text>
        {peer.tokenHash && (
          <Text style={styles.peerSub}>token: {peer.tokenHash.slice(0, 12)}…</Text>
        )}
      </View>
      {peer.rssi != null && <SignalBars rssi={peer.rssi} />}
    </View>
  )
}

// ─── Event log item ───────────────────────────────────────────────────────────

interface LogEntry {
  id: string
  ts: string
  kind: 'info' | 'success' | 'warn' | 'error'
  text: string
}

const LOG_COLORS = {
  info:    '#185FA5',
  success: '#0F6E56',
  warn:    '#BA7517',
  error:   '#A32D2D',
}

// ─── Tabs ─────────────────────────────────────────────────────────────────────

type Tab = 'chat' | 'peers' | 'contacts' | 'engine'

// ─── Main screen ─────────────────────────────────────────────────────────────

export default function MeshTestScreen() {
  const keyPair = useMemo(() => getOrCreateKeyPair(), [])

  const { engine, status, messages, nearbyPeers, nearbyContacts } = useMesh(keyPair, {
    defaultTTL:         7,
    sprayFactor:        4,
    enableMultipeer:    Platform.OS === 'ios',
    enableBackground:   false, // set true to test background BLE
    androidNotificationText: 'Mesh test active',
  })

  const [tab, setTab] = useState<Tab>('chat')
  const [log, setLog] = useState<LogEntry[]>([])
  const [compose, setCompose] = useState('')
  const [activeConvId, setActiveConvId] = useState<string | null>(null)
  const [addKeyHex, setAddKeyHex] = useState('')

  const logRef = useRef<ScrollView>(null)
  const ownFp  = useMemo(
    () => bytesToHex(keyPair.publicKey).slice(0, 16),
    [keyPair],
  )

  // ── Log helper ────────────────────────────────────────────────────────────

  const addLog = useCallback((text: string, kind: LogEntry['kind'] = 'info') => {
    setLog(prev => [{
      id:   Date.now().toString() + Math.random(),
      ts:   timeStr(),
      kind,
      text,
    }, ...prev].slice(0, 50))
  }, [])

  // ── Wire up engine events to the log ─────────────────────────────────────

  useEffect(() => {
    if (!engine) return
    const offs = [
      engine.on('peer:discovered', p  => addLog(`Peer discovered: ${p.deviceId.slice(0, 8)} (${p.transport})`, 'info')),
      engine.on('peer:connected',  ev => addLog(`Connected: ${ev.deviceId.slice(0, 8)} via ${ev.transport}`, 'success')),
      engine.on('peer:disconnected',ev => addLog(`Disconnected: ${ev.deviceId.slice(0, 8)}`, 'warn')),
      engine.on('contact:nearby',  ev => addLog(`Contact nearby: ${ev.contact.alias ?? ev.contact.id.slice(0, 8)}`, 'success')),
      engine.on('message',         msg => addLog(`Message from ${msg.senderId.slice(0, 8)} (${msg.hopCount} hops)`, 'success')),
      engine.on('ack:received',    ev  => addLog(`ACK for ${ev.packetId.slice(0, 8)}`, 'info')),
      engine.on('packet:relayed',  ev  => addLog(`Relayed ${ev.packetId.slice(0, 8)} → ${ev.toPeer.slice(0, 8)}`, 'info')),
      engine.on('handshake:completed', ev => addLog(`Handshake: ${ev.contactId.slice(0, 8)}`, 'info')),
      engine.on('radio:phase', s   => addLog(`Radio → ${s.phase}`, 'info')),
      engine.on('error',       ev  => addLog(`Error [${ev.code}]: ${ev.message}`, 'error')),
    ]
    return () => offs.forEach(off => off())
  }, [engine, addLog])

  // ── Init log ──────────────────────────────────────────────────────────────

  useEffect(() => {
    if (status.running)      addLog('Engine running', 'success')
    if (!status.initialised && status.error) addLog(`Init failed: ${status.error}`, 'error')
  }, [status.running, status.error, addLog])

  // ── Send ──────────────────────────────────────────────────────────────────

  const send = useCallback(async () => {
    if (!engine || !compose.trim() || !activeConvId) return
    try {
      await engine.sendMessage(
        activeConvId,
        ContentType.TEXT,
        utf8Encode(compose.trim()),
      )
      setCompose('')
    } catch (err: any) {
      addLog(`Send failed: ${err?.message}`, 'error')
    }
  }, [engine, compose, activeConvId, addLog])

  // ── Add contact from public key hex ──────────────────────────────────────

  const addContact = useCallback(() => {
    if (!engine) return
    const hex = addKeyHex.trim().replace(/\s+/g, '')
    if (hex.length !== 64) {
      Alert.alert('Invalid key', 'Paste the full 64-char (32-byte) hex public key')
      return
    }
    const bytes = new Uint8Array(hex.match(/.{2}/g)!.map(b => parseInt(b, 16)))
    const c = engine.addContactFromPublicKey(bytes, `Contact ${engine.getContacts().length + 1}`)
    setActiveConvId(c.id)
    setTab('chat')
    setAddKeyHex('')
    addLog(`Contact added: ${c.id.slice(0, 8)}`, 'success')
  }, [engine, addKeyHex, addLog])

  // ── Tab bar ───────────────────────────────────────────────────────────────

  const tabs: { key: Tab; label: string }[] = [
    { key: 'chat',     label: 'Chat'     },
    { key: 'peers',    label: 'Peers'    },
    { key: 'contacts', label: 'Contacts' },
    { key: 'engine',   label: 'Engine'   },
  ]

  const contacts = engine?.getContacts() ?? []
  const convMessages: MeshMessage[] = activeConvId
    ? (engine?.getMessages(activeConvId) ?? messages.get(activeConvId) ?? [])
    : []

  // ─────────────────────────────────────────────────────────────────────────
  // RENDER
  // ─────────────────────────────────────────────────────────────────────────

  return (
    <SafeAreaView style={styles.safe}>
      <KeyboardAvoidingView
        style={styles.flex}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={88}
      >

        {/* ── Header ─────────────────────────────────────────────────────── */}
        <View style={styles.header}>
          <View>
            <Text style={styles.headerTitle}>Anon Mesh</Text>
            <Text style={styles.headerSub} numberOfLines={1}>
              fp: {ownFp}
            </Text>
          </View>
          <View style={styles.statusPill}>
            {status.running
              ? <ActivityIndicator size="small" color="#0F6E56" style={{ marginRight: 6 }} />
              : null}
            <Text style={[styles.statusText, { color: status.running ? '#0F6E56' : status.error ? '#A32D2D' : '#5F5E5A' }]}>
              {status.running
                ? (status.radioPhase ?? 'running')
                : status.error ?? 'stopped'}
            </Text>
          </View>
        </View>

        {/* ── Tab bar ─────────────────────────────────────────────────────── */}
        <View style={styles.tabBar}>
          {tabs.map(t => (
            <Pressable key={t.key} onPress={() => setTab(t.key)} style={styles.tabBtn}>
              <Text style={[styles.tabLabel, tab === t.key && styles.tabLabelActive]}>
                {t.label}
              </Text>
              {tab === t.key && <View style={styles.tabUnderline} />}
            </Pressable>
          ))}
        </View>

        {/* ── CHAT TAB ────────────────────────────────────────────────────── */}
        {tab === 'chat' && (
          <View style={styles.flex}>

            {/* Contact selector */}
            {contacts.length === 0 ? (
              <View style={styles.empty}>
                <Text style={styles.emptyText}>
                  No contacts yet. Go to Contacts tab to add one.
                </Text>
              </View>
            ) : (
              <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.convPicker}>
                {contacts.map(c => (
                  <Pressable
                    key={c.id}
                    onPress={() => setActiveConvId(c.id)}
                    style={[styles.convChip, activeConvId === c.id && styles.convChipActive]}
                  >
                    <Text style={[styles.convChipText, activeConvId === c.id && styles.convChipTextActive]}>
                      {c.alias ?? c.id.slice(0, 8)}
                    </Text>
                  </Pressable>
                ))}
              </ScrollView>
            )}

            {/* Messages */}
            {activeConvId ? (
              <FlatList
                data={convMessages}
                keyExtractor={m => m.id}
                renderItem={({ item }) => (
                  <MessageBubble
                    msg={item}
                    mine={item.senderId === 'me' || item.recipientId !== 'me'}
                  />
                )}
                contentContainerStyle={styles.messageList}
                inverted={false}
                style={styles.flex}
              />
            ) : (
              <View style={styles.empty}>
                <Text style={styles.emptyText}>Select a contact above to open a thread</Text>
              </View>
            )}

            {/* Compose */}
            {activeConvId && (
              <View style={styles.composeBar}>
                <TextInput
                  style={styles.composeInput}
                  value={compose}
                  onChangeText={setCompose}
                  placeholder="Type a message…"
                  placeholderTextColor="#888780"
                  returnKeyType="send"
                  onSubmitEditing={send}
                  editable={status.running}
                />
                <Pressable
                  onPress={send}
                  disabled={!status.running || !compose.trim()}
                  style={[styles.sendBtn, (!status.running || !compose.trim()) && styles.sendBtnDisabled]}
                >
                  <Text style={styles.sendBtnText}>Send</Text>
                </Pressable>
              </View>
            )}
          </View>
        )}

        {/* ── PEERS TAB ──────────────────────────────────────────────────── */}
        {tab === 'peers' && (
          <ScrollView contentContainerStyle={styles.section}>
            <Text style={styles.sectionLabel}>Nearby peers ({nearbyPeers.length})</Text>
            {nearbyPeers.length === 0 ? (
              <Text style={styles.emptyText}>
                {status.running ? 'Scanning… peers appear here as they\'re discovered.' : 'Start the engine to scan.'}
              </Text>
            ) : nearbyPeers.map(p => {
              const nc = nearbyContacts.find(nc => nc.deviceId === p.deviceId)
              return <PeerRow key={p.deviceId} peer={p} contact={nc?.contact} />
            })}

            <Text style={[styles.sectionLabel, { marginTop: 24 }]}>
              Nearby contacts ({nearbyContacts.length})
            </Text>
            {nearbyContacts.length === 0
              ? <Text style={styles.emptyText}>No known contacts detected yet.</Text>
              : nearbyContacts.map(nc => (
                  <View key={nc.deviceId} style={styles.peerRow}>
                    <View style={styles.peerInfo}>
                      <Text style={styles.peerAlias}>{nc.contact.alias ?? nc.contact.id.slice(0, 8)}</Text>
                      <Text style={styles.peerSub}>fp: {nc.contact.id}</Text>
                      <Text style={styles.peerSub}>device: {nc.deviceId.slice(0, 16)}</Text>
                    </View>
                  </View>
                ))}
          </ScrollView>
        )}

        {/* ── CONTACTS TAB ───────────────────────────────────────────────── */}
        {tab === 'contacts' && (
          <ScrollView contentContainerStyle={styles.section}>

            {/* Own identity */}
            <Text style={styles.sectionLabel}>Your identity</Text>
            <View style={styles.card}>
              <Text style={styles.cardLabel}>Public key (share this via QR)</Text>
              <Text style={styles.mono} selectable>
                {bytesToHex(keyPair.publicKey)}
              </Text>
              <Text style={[styles.cardLabel, { marginTop: 8 }]}>Fingerprint</Text>
              <Text style={styles.mono}>{ownFp}</Text>
            </View>

            {/* Add contact */}
            <Text style={[styles.sectionLabel, { marginTop: 24 }]}>Add contact</Text>
            <View style={styles.card}>
              <Text style={styles.cardLabel}>Paste their 64-char hex public key</Text>
              <TextInput
                style={styles.addKeyInput}
                value={addKeyHex}
                onChangeText={setAddKeyHex}
                placeholder="a1b2c3d4e5f6…"
                placeholderTextColor="#888780"
                autoCapitalize="none"
                autoCorrect={false}
              />
              <Pressable onPress={addContact} style={styles.addBtn}>
                <Text style={styles.addBtnText}>Add contact</Text>
              </Pressable>
            </View>

            {/* Contact list */}
            <Text style={[styles.sectionLabel, { marginTop: 24 }]}>
              Contacts ({contacts.length})
            </Text>
            {contacts.length === 0
              ? <Text style={styles.emptyText}>No contacts yet.</Text>
              : contacts.map(c => (
                  <Pressable
                    key={c.id}
                    style={styles.card}
                    onPress={() => { setActiveConvId(c.id); setTab('chat') }}
                  >
                    <Text style={styles.contactAlias}>{c.alias ?? 'Contact'}</Text>
                    <Text style={styles.mono}>fp: {c.id}</Text>
                    {c.lastSeenMs && (
                      <Text style={styles.cardLabel}>
                        Last seen: {new Date(c.lastSeenMs).toLocaleTimeString()}
                      </Text>
                    )}
                  </Pressable>
                ))}
          </ScrollView>
        )}

        {/* ── ENGINE TAB ─────────────────────────────────────────────────── */}
        {tab === 'engine' && (
          <View style={styles.flex}>

            {/* Stats row */}
            <View style={styles.statsRow}>
              {[
                { label: 'Status',   value: status.running ? 'Running' : 'Stopped', color: status.running ? '#0F6E56' : '#5F5E5A' },
                { label: 'Phase',    value: status.radioPhase ?? '—',                color: '#185FA5' },
                { label: 'Contacts', value: String(contacts.length),                 color: '#534AB7' },
                { label: 'Nearby',   value: String(nearbyPeers.length),              color: '#993C1D' },
              ].map(s => (
                <View key={s.label} style={styles.statCard}>
                  <Text style={styles.statLabel}>{s.label}</Text>
                  <Text style={[styles.statValue, { color: s.color }]}>{s.value}</Text>
                </View>
              ))}
            </View>

            {/* Log */}
            <Text style={styles.logHeader}>Event log</Text>
            <ScrollView ref={logRef} style={styles.logScroll} contentContainerStyle={styles.logContent}>
              {log.length === 0
                ? <Text style={styles.emptyText}>No events yet.</Text>
                : log.map(entry => (
                    <View key={entry.id} style={styles.logRow}>
                      <Text style={styles.logTs}>{entry.ts}</Text>
                      <Text style={[styles.logText, { color: LOG_COLORS[entry.kind] }]}>
                        {entry.text}
                      </Text>
                    </View>
                  ))}
            </ScrollView>
          </View>
        )}

      </KeyboardAvoidingView>
    </SafeAreaView>
  )
}

// ─── Styles ────────────────────────────────────────────────────────────────────

const C = {
  bg:          '#FFFFFF',
  surface:     '#F8F7F4',
  border:      'rgba(0,0,0,0.1)',
  text:        '#1A1A1A',
  textSub:     '#5F5E5A',
  textMuted:   '#B4B2A9',
  accent:      '#185FA5',
  accentBg:    '#E6F1FB',
  sendBg:      '#185FA5',
  mono:        Platform.OS === 'ios' ? 'Menlo' : 'monospace',
}

const styles = StyleSheet.create({
  flex:           { flex: 1 },
  safe:           { flex: 1, backgroundColor: C.bg },

  // Header
  header:         { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', paddingHorizontal: 16, paddingVertical: 12, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: C.border },
  headerTitle:    { fontSize: 17, fontWeight: '600', color: C.text },
  headerSub:      { fontSize: 11, fontFamily: C.mono, color: C.textSub, marginTop: 1 },
  statusPill:     { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 10, paddingVertical: 5, backgroundColor: C.surface, borderRadius: 20, borderWidth: StyleSheet.hairlineWidth, borderColor: C.border },
  statusText:     { fontSize: 12, fontWeight: '500' },

  // Tabs
  tabBar:         { flexDirection: 'row', borderBottomWidth: StyleSheet.hairlineWidth, borderColor: C.border },
  tabBtn:         { flex: 1, alignItems: 'center', paddingVertical: 11 },
  tabLabel:       { fontSize: 13, color: C.textSub },
  tabLabelActive: { color: C.accent, fontWeight: '500' },
  tabUnderline:   { position: 'absolute', bottom: 0, left: 8, right: 8, height: 2, backgroundColor: C.accent, borderRadius: 1 },

  // Chat — conversation picker
  convPicker:     { flexGrow: 0, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: C.border, paddingVertical: 8, paddingHorizontal: 12 },
  convChip:       { marginRight: 8, paddingHorizontal: 14, paddingVertical: 6, borderRadius: 20, backgroundColor: C.surface, borderWidth: StyleSheet.hairlineWidth, borderColor: C.border },
  convChipActive: { backgroundColor: C.accentBg, borderColor: C.accent },
  convChipText:   { fontSize: 13, color: C.textSub },
  convChipTextActive: { color: C.accent, fontWeight: '500' },

  // Messages
  messageList:    { padding: 12, paddingBottom: 24 },
  bubbleWrap:     { marginBottom: 8, alignItems: 'flex-start' },
  bubbleWrapMine: { alignItems: 'flex-end' },
  bubble:         { maxWidth: '75%', paddingHorizontal: 14, paddingVertical: 9, borderRadius: 18 },
  bubbleTheirs:   { backgroundColor: C.surface, borderBottomLeftRadius: 4 },
  bubbleMine:     { backgroundColor: C.accent, borderBottomRightRadius: 4 },
  bubbleText:     { fontSize: 15, color: C.text, lineHeight: 21 },
  bubbleTextMine: { color: '#FFFFFF' },
  bubbleMeta:     { flexDirection: 'row', gap: 8, marginTop: 3, marginHorizontal: 4 },
  metaText:       { fontSize: 10, fontFamily: C.mono, color: C.textMuted },

  // Compose
  composeBar:     { flexDirection: 'row', alignItems: 'center', padding: 10, gap: 8, borderTopWidth: StyleSheet.hairlineWidth, borderColor: C.border, backgroundColor: C.bg },
  composeInput:   { flex: 1, minHeight: 38, maxHeight: 120, backgroundColor: C.surface, borderRadius: 20, paddingHorizontal: 14, paddingVertical: 8, fontSize: 15, color: C.text, borderWidth: StyleSheet.hairlineWidth, borderColor: C.border },
  sendBtn:        { paddingHorizontal: 16, paddingVertical: 9, backgroundColor: C.sendBg, borderRadius: 20 },
  sendBtnDisabled:{ opacity: 0.4 },
  sendBtnText:    { color: '#FFF', fontSize: 14, fontWeight: '500' },

  // Peers
  section:        { padding: 16 },
  sectionLabel:   { fontSize: 12, fontWeight: '500', color: C.textSub, letterSpacing: 0.5, textTransform: 'uppercase', marginBottom: 8 },
  peerRow:        { flexDirection: 'row', alignItems: 'center', paddingVertical: 10, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: C.border },
  peerInfo:       { flex: 1 },
  peerAlias:      { fontSize: 14, fontWeight: '500', color: C.text, marginBottom: 2 },
  peerSub:        { fontSize: 11, fontFamily: C.mono, color: C.textSub },
  signalRow:      { flexDirection: 'row', alignItems: 'flex-end', gap: 2 },
  signalBar:      { width: 4, backgroundColor: C.border, borderRadius: 1 },
  signalBarActive:{ backgroundColor: '#1D9E75' },

  // Contacts
  card:           { backgroundColor: C.surface, borderRadius: 10, padding: 14, marginBottom: 10, borderWidth: StyleSheet.hairlineWidth, borderColor: C.border },
  cardLabel:      { fontSize: 11, color: C.textSub, marginBottom: 4 },
  mono:           { fontFamily: C.mono, fontSize: 11, color: C.text, lineHeight: 16 },
  contactAlias:   { fontSize: 15, fontWeight: '500', color: C.text, marginBottom: 4 },
  addKeyInput:    { backgroundColor: C.bg, borderRadius: 8, borderWidth: StyleSheet.hairlineWidth, borderColor: C.border, padding: 10, fontFamily: C.mono, fontSize: 12, color: C.text, marginBottom: 10 },
  addBtn:         { backgroundColor: C.accent, borderRadius: 8, padding: 11, alignItems: 'center' },
  addBtnText:     { color: '#FFF', fontSize: 14, fontWeight: '500' },

  // Engine
  statsRow:       { flexDirection: 'row', padding: 12, gap: 8 },
  statCard:       { flex: 1, backgroundColor: C.surface, borderRadius: 10, padding: 10, alignItems: 'center', borderWidth: StyleSheet.hairlineWidth, borderColor: C.border },
  statLabel:      { fontSize: 10, color: C.textSub, marginBottom: 4 },
  statValue:      { fontSize: 14, fontWeight: '600', fontFamily: C.mono },
  logHeader:      { fontSize: 12, fontWeight: '500', color: C.textSub, paddingHorizontal: 16, paddingBottom: 6, letterSpacing: 0.5, textTransform: 'uppercase' },
  logScroll:      { flex: 1 },
  logContent:     { padding: 12 },
  logRow:         { flexDirection: 'row', gap: 8, paddingVertical: 4, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: C.border, alignItems: 'flex-start' },
  logTs:          { fontSize: 10, fontFamily: C.mono, color: C.textMuted, flexShrink: 0, marginTop: 1 },
  logText:        { fontSize: 12, fontFamily: C.mono, flex: 1, lineHeight: 17 },

  // Shared
  empty:          { flex: 1, alignItems: 'center', justifyContent: 'center', padding: 32 },
  emptyText:      { fontSize: 14, color: C.textSub, textAlign: 'center', lineHeight: 20 },

  // Packet badge
  badge:          { paddingHorizontal: 6, paddingVertical: 2, borderRadius: 4 },
  badgeText:      { fontSize: 10, fontWeight: '500', fontFamily: C.mono, letterSpacing: 0.3 },
})