# BLE Mesh Network — Architecture & Developer Guide

A comprehensive, cross-platform Bluetooth Low Energy decentralized messaging
engine built on **munim-bluetooth** with a Hybrid P2P / Ephemeral GATT
Connection architecture.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Module Map](#module-map)
3. [Wire Protocol](#wire-protocol)
4. [GATT Layout](#gatt-layout)
5. [Transport Strategy](#transport-strategy)
6. [Connection Lifecycle](#connection-lifecycle)
7. [Installation & Setup](#installation--setup)
8. [Quick Start](#quick-start)
9. [Feature Guide](#feature-guide)
10. [Adding New Features](#adding-new-features)
11. [Platform Caveats](#platform-caveats)
12. [Performance Tuning](#performance-tuning)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                    React Components                      │
│  useMeshNetwork · useDirectMessage · useGroupChat        │
│  useDeviceLocator · usePresence                         │
└──────────────────────────┬──────────────────────────────┘
                           │ hooks (EventBus subscriptions)
┌──────────────────────────▼──────────────────────────────┐
│                    Feature Modules                       │
│   DirectMessaging · GroupChat · DeviceLocator · Presence │
└──────────────────────────┬──────────────────────────────┘
                           │ MeshMessage (typed events)
┌──────────────────────────▼──────────────────────────────┐
│                   TransportManager                       │
│  • Selects best transport (BLE GATT vs Multipeer)        │
│  • Drives MessageProtocol encode/decode                  │
│  • Updates PeerRegistry                                  │
└─────────┬──────────────────────────────┬────────────────┘
          │ BLE GATT                     │ Apple Multipeer
┌─────────▼──────────┐       ┌───────────▼────────────────┐
│     BLEEngine       │       │  munim-bluetooth            │
│  • Peripheral mode  │       │  startMultipeerSession()    │
│  • Central mode     │       │  sendMultipeerMessage()     │
│  • Retry / backoff  │       │  (iOS only)                 │
│  • RSSI polling     │       └────────────────────────────┘
└─────────┬──────────┘
          │
┌─────────▼──────────────────────────────────────────────┐
│                   munim-bluetooth                        │
│  startAdvertising · setServices · startScan · connect   │
│  writeCharacteristic · updateCharacteristicValue        │
└────────────────────────────────────────────────────────┘
```

### Core Principle: Every Node is Both Peripheral AND Central

Each device simultaneously:
- **Peripheral**: advertises `MESH_SERVICE_UUID`, hosts a GATT server with
  write / notify / read characteristics.
- **Central**: scans for `MESH_SERVICE_UUID`, connects to discovered peers
  to send messages by writing to their `MSG_WRITE_CHAR_UUID`.

This dual-role design enables true peer-to-peer bidirectionality without any
relay server.

---

## Module Map

```
src/
├── constants.ts           UUIDs, timing constants, storage keys
├── types.ts               All shared TypeScript types
├── MeshEngine.ts          Top-level singleton; wires all modules together
├── index.ts               Public barrel export
│
├── core/
│   ├── EventBus.ts        Typed internal pub/sub (MeshEventMap)
│   ├── MessageProtocol.ts Encode · decode · fragment · reassemble
│   ├── PeerRegistry.ts    Peer state, RSSI smoothing, stale eviction
│   ├── BLEEngine.ts       munim-bluetooth wrapper; retry; idle disconnect
│   └── TransportManager.ts Transport selection; BLE + Multipeer integration
│
├── features/
│   ├── DirectMessaging.ts Send/receive DMs; ACK tracking; retry queue
│   ├── GroupChat.ts       Group lifecycle; fan-out delivery
│   ├── DeviceLocator.ts   Active RSSI ranging; ping/pong RTT
│   └── Presence.ts        Heartbeat broadcasts; stale TTL tracking
│
├── store/
│   └── MessageStore.ts    In-memory store + AsyncStorage persistence
│
├── hooks.ts               React hooks: useMeshNetwork, useDM, useGroup, ...
│
└── utils/
    ├── hex.ts             Hex ↔ string ↔ Uint8Array; Base64; TextEncoder shims
    ├── uuid.ts            UUID v4 generation (crypto.getRandomValues)
    └── crypto.ts          FNV-1a checksum; HMAC-SHA-256 signing (SubtleCrypto)
```

### Dependency Rules (strict one-way flow)

```
utils  ←  core  ←  features  ←  hooks  ←  app
           ↑
       constants / types
```

`features` never import from each other. They communicate exclusively through
the `EventBus`. Adding a new feature never requires modifying existing ones.

---

## Wire Protocol

Every physical transmission (GATT write or Multipeer send) is a hex-encoded
JSON **WireFrame**:

```typescript
interface WireFrame {
  v:   number          // protocol version (always 1)
  id:  string          // 8-char msgId prefix (fragment grouping key)
  p:   number          // part index, 0-based
  n:   number          // total parts (1 = no fragmentation)
  f?:  string          // from peer ID      ← first frame only
  t?:  string | null   // to peer / group ID ← first frame only
  k?:  MessageKind     // message type       ← first frame only
  ts?: number          // unix timestamp ms  ← first frame only
  d:   string          // base64 payload chunk
}
```

The inner payload (`d`) is a Base64-encoded, kind-specific JSON object:

| Kind           | Payload fields                         |
|----------------|----------------------------------------|
| `dm`           | `{ text, raw? }`                       |
| `dm_ack`       | `{ acked: msgId }`                     |
| `group`        | `{ text, raw? }`                       |
| `group_invite` | `{ gid, gname, by, members }`          |
| `group_meta`   | `{ gid, name?, members? }`             |
| `presence`     | `{ status, name }`                     |
| `ping`/`pong`  | `{ nonce }`                            |
| `locate_req`   | `{ nonce }`                            |
| `locate_res`   | `{ nonce, rssi }`                      |

### Fragmentation

- Logical messages larger than `MAX_CHUNK_BYTES` (180 bytes) are split across
  multiple frames.
- Only the first frame carries the envelope metadata (`f`, `t`, `k`, `ts`).
- Receivers accumulate frames by `id` (short message ID) until all `n` parts
  arrive, then reassemble.
- A `FRAGMENT_REASSEMBLY_TIMEOUT_MS` (30 s) guard clears incomplete assemblies.

---

## GATT Layout

```
Service: MESH_SERVICE_UUID (c39b6354-f7e2-4a8b-92d3-5e8a1b0f2c7d)
│
├── MSG_WRITE_CHAR_UUID (a1b2c3d4-...)
│     Properties: write, writeWithoutResponse
│     Purpose:    Central → Peripheral inbound message delivery
│
├── MSG_NOTIFY_CHAR_UUID (b2c3d4e5-...)
│     Properties: notify, read
│     Purpose:    Peripheral → Central push (updateCharacteristicValue)
│
└── PEER_INFO_CHAR_UUID (c3d4e5f6-...)
      Properties: read
      Value:      hex-encoded PeerInfoPayload JSON
      Purpose:    Central reads on connect to learn stable peer ID + capabilities
```

---

## Transport Strategy

```
Sender Platform  │  Receiver Platform  │  Transport Used
─────────────────┼─────────────────────┼──────────────────────────────────
iOS              │  iOS                │  Multipeer (primary) → BLE GATT fallback
iOS              │  Android            │  BLE GATT
Android          │  iOS                │  BLE GATT
Android          │  Android            │  BLE GATT
```

`TransportManager.send()` selects the transport based on:
1. `peer.preferredTransport` (set to `multipeer` when the peer was discovered
   via Multipeer Connectivity).
2. `this.multipeerAvailable` (true on iOS after `getCapabilities()` returns
   `multipeerConnectivity: true`).
3. Falls back to BLE GATT on any Multipeer send error.

---

## Connection Lifecycle (Ephemeral Pattern)

```
[Scan result]
      │ MESH_SERVICE_UUID seen
      ▼
 PeerRegistry: 'discovered'
      │
      │ send() called for this peer
      ▼
 BLEEngine.connect() ──retry──► PeerRegistry: 'connecting'
      │
      │ success
      ▼
 discoverServices() → readPeerInfo() → requestMTU() → subscribeToNotify()
      │
      ▼
 PeerRegistry: 'connected' / 'subscribed'
      │
      │ writes queued frames to MSG_WRITE_CHAR_UUID
      ▼
 Idle timer starts (EPHEMERAL_IDLE_TIMEOUT_MS = 8 s)
      │
      │ no more writes within idle window
      ▼
 disconnect() → PeerRegistry: 'disconnected'
```

The idle timer is reset on every new write. High-frequency conversations keep
the connection alive naturally; infrequent messages re-establish as needed.

---

## Installation & Setup

### 1. Install dependencies

```bash
npm install munim-bluetooth react-native-nitro-modules \
            @react-native-async-storage/async-storage
```

### 2. iOS — Info.plist

```xml
<key>NSBluetoothAlwaysUsageDescription</key>
<string>Needed for nearby peer messaging</string>
<key>NSBluetoothPeripheralUsageDescription</key>
<string>Needed to advertise as a nearby peer</string>
<key>NSLocalNetworkUsageDescription</key>
<string>Needed for iOS-to-iOS Multipeer messaging</string>
<key>NSBonjourServices</key>
<array>
  <string>_mesh-msg._tcp</string>
</array>
<key>UIBackgroundModes</key>
<array>
  <string>bluetooth-central</string>
  <string>bluetooth-peripheral</string>
</array>
```

### 3. Android — AndroidManifest.xml

```xml
<uses-permission android:name="android.permission.BLUETOOTH" />
<uses-permission android:name="android.permission.BLUETOOTH_ADMIN" />
<uses-permission android:name="android.permission.BLUETOOTH_ADVERTISE" />
<uses-permission android:name="android.permission.BLUETOOTH_SCAN" />
<uses-permission android:name="android.permission.BLUETOOTH_CONNECT" />
<uses-permission android:name="android.permission.ACCESS_FINE_LOCATION" />
<uses-permission android:name="android.permission.ACCESS_COARSE_LOCATION" />
```

### 4. Expo (app.json)

```json
{
  "expo": {
    "plugins": [
      ["munim-bluetooth", {
        "multipeerServiceTypes": ["mesh-msg"],
        "localNetworkUsageDescription": "Peer-to-peer nearby messaging"
      }]
    ],
    "ios": {
      "infoPlist": {
        "NSBluetoothAlwaysUsageDescription": "Needed for nearby peer messaging",
        "NSBluetoothPeripheralUsageDescription": "Needed to advertise as peer",
        "NSLocalNetworkUsageDescription": "Needed for iOS Multipeer",
        "NSBonjourServices": ["_mesh-msg._tcp"]
      }
    },
    "android": {
      "permissions": [
        "android.permission.BLUETOOTH",
        "android.permission.BLUETOOTH_ADMIN",
        "android.permission.BLUETOOTH_ADVERTISE",
        "android.permission.BLUETOOTH_SCAN",
        "android.permission.BLUETOOTH_CONNECT",
        "android.permission.ACCESS_FINE_LOCATION"
      ]
    }
  }
}
```

---

## Quick Start

```typescript
import AsyncStorage from '@react-native-async-storage/async-storage'
import { MeshEngine, bootstrapPeerIdentity } from './src'

// App.tsx — initialise once
export default function App() {
  const [engine, setEngine] = useState<MeshEngine | null>(null)

  useEffect(() => {
    let mounted = true
    ;(async () => {
      const { selfId, displayName } = await bootstrapPeerIdentity(
        AsyncStorage,
        'My Device',
      )
      const eng = await MeshEngine.create(
        { selfId, displayName, background: true },
        AsyncStorage,
      )
      if (mounted) setEngine(eng)
    })()
    return () => {
      mounted = false
      engine?.destroy()
    }
  }, [])

  return <MeshContext.Provider value={engine}><Navigation /></MeshContext.Provider>
}
```

```typescript
// ChatScreen.tsx
import { useDirectMessage } from './src'

function ChatScreen({ peerId }: { peerId: string }) {
  const engine = useContext(MeshContext)
  const { messages, send, markRead } = useDirectMessage(engine, peerId)

  useEffect(() => { markRead() }, [])

  return (
    <FlatList
      data={messages}
      renderItem={({ item }) => <MessageBubble msg={item} />}
      keyExtractor={(m) => m.msgId}
    />
    <TextInput onSubmitEditing={(e) => send(e.nativeEvent.text)} />
  )
}
```

---

## Feature Guide

### Direct Messaging

```typescript
// Send
const msg = await engine.dm.send(peerId, 'Hello!')

// Get conversation history
const history = engine.dm.getConversation(peerId)

// Mark as read
engine.dm.markRead(peerId)
```

### Group Chat

```typescript
// Create
const group = await engine.groupChat.createGroup('Team Alpha', [aliceId, bobId])

// Send
await engine.groupChat.send(group.id, 'Hey team!')

// List groups
const groups = engine.groupChat.getAllGroups()

// Leave
await engine.groupChat.leaveGroup(group.id)
```

### Device Locator

```typescript
// Active range measurement (round-trip to peer + RSSI)
const result = await engine.locator.locate(peerId)
// → { localRssi, peerRssi, estimatedDistanceM, rttMs }

// RTT ping
const rttMs = await engine.locator.ping(peerId)

// Scan all nearby peers by proximity
const sorted = await engine.locator.scanNearby()
```

### Presence

```typescript
// Set own status
await engine.presence.setStatus('away')

// Get peer status (updated by heartbeats)
const status = engine.presence.getPeerStatus(peerId)
// → 'online' | 'away' | 'busy' | 'offline'
```

---

## Adding New Features

The engine is designed for extension without modification.

### Step-by-step

1. **Define new `MessageKind`(s)** in `types.ts`:
   ```typescript
   export type MessageKind = ... | 'file_transfer' | 'file_ack'
   ```

2. **Add payload types** to `types.ts`:
   ```typescript
   export interface FileTransfer extends BaseMessage {
     kind: 'file_transfer'
     filename: string
     mimeType: string
     sizeBytes: number
     chunkIndex: number
     totalChunks: number
     data: string  // base64 chunk
   }
   ```

3. **Add EventBus topics** to `MeshEventMap` in `types.ts`:
   ```typescript
   'message:file_transfer': FileTransfer
   'message:file_ack': FileAck
   ```

4. **Wire dispatch** in `TransportManager.dispatchMessage()`:
   ```typescript
   case 'file_transfer': this.bus.emit('message:file_transfer', msg as any); break
   ```

5. **Wire encode** in `MessageProtocol.messageToPayloadJson()`:
   ```typescript
   case 'file_transfer': return JSON.stringify({ fn: msg.filename, mt: msg.mimeType, ... })
   ```

6. **Wire decode** in `MessageProtocol.assembleMessage()`:
   ```typescript
   case 'file_transfer': return { ...base, kind: 'file_transfer', filename: payload.fn, ... }
   ```

7. **Create feature module** `features/FileTransfer.ts`:
   ```typescript
   export class FileTransfer {
     constructor(selfId, transport, bus) { ... }
     async send(peerId, file) { ... }
     private registerListeners() { bus.on('message:file_transfer', ...) }
   }
   ```

8. **Mount on MeshEngine**:
   ```typescript
   this.fileTransfer = new FileTransfer(config.selfId, transport, bus)
   ```

9. **Expose via React hook** in `hooks.ts`.

No existing file changes are required except the four wiring additions
(steps 4–6) in the protocol layer — intentionally minimal.

---

## Platform Caveats

| Concern | iOS | Android |
|---------|-----|---------|
| Peripheral advertising payload | Service UUIDs + local name only | Full AdvertisingDataTypes |
| MTU negotiation | Automatic (~185 bytes) | Call `requestMTU(512)` after connect |
| Background BLE | CoreBluetooth restoration (`UIBackgroundModes`) | Foreground service (`START_STICKY`) |
| Multipeer Connectivity | ✅ Native | ❌ N/A |
| Bond / pairing management | Automatic (invisible) | `getBondState()` / `createBond()` |
| Extended advertising | ❌ | ✅ Android 8+ |

### iOS background limits

On iOS, the app must declare `bluetooth-central` and `bluetooth-peripheral`
in `UIBackgroundModes`. The engine calls `startBackgroundSession()` which
uses CoreBluetooth state restoration identifiers. As of iOS 26, Apple requires
AccessorySetupKit eligibility for terminated-state BLE relaunch; general
phone-to-phone mesh apps should not rely on it.

### Android foreground service

`startBackgroundSession()` starts an Android foreground service that persists
scan, advertising, and GATT state through process recreation. A user-visible
notification is required by Android policy.

---

## Performance Tuning

| Setting | File | Default | Notes |
|---------|------|---------|-------|
| `MAX_CHUNK_BYTES` | constants.ts | 180 | Increase after MTU negotiation |
| `EPHEMERAL_IDLE_TIMEOUT_MS` | constants.ts | 8 000 | Lower for faster cleanup |
| `SCAN_CYCLE_MS` | constants.ts | 15 000 | Lower for faster discovery |
| `PRESENCE_HEARTBEAT_MS` | constants.ts | 20 000 | Raise to reduce traffic |
| `RSSI_POLL_INTERVAL_MS` | constants.ts | 1 500 | Raise to save battery |
| `RSSI_SAMPLE_COUNT` | constants.ts | 8 | More samples = smoother distance |
| `scanMode` in `startScan` | BLEEngine.ts | `balanced` | `lowPower` saves battery |

### Battery considerations

- Use `scanMode: 'lowPower'` in background sessions.
- Increase `EPHEMERAL_IDLE_TIMEOUT_MS` to avoid frequent re-connections.
- Raise `PRESENCE_HEARTBEAT_MS` in idle states (combine with `usePresence.setStatus('away')`).
- Disable the DeviceLocator RSSI poll when the locator screen is not visible.