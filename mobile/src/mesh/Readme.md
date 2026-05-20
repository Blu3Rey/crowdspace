# Anon Mesh Core

Decentralised BLE P2P messaging engine for React Native, built on `munim-bluetooth`.

---

## Module Map

```
src/mesh/
├── core/
│   ├── types.ts        All shared TypeScript types (no runtime code)
│   ├── constants.ts    Single source of truth for all tunable values
│   ├── encoding.ts     Compact binary pack/unpack (no JSON on the wire)
│   ├── crypto.ts       NaCl E2EE, rotating tokens, fingerprints
│   └── eventBus.ts     Typed internal event bus
├── transport/
│   ├── bleGattSession.ts   Per-peer GATT session (connect, MTU, chunking)
│   ├── bleTransport.ts     BLE session pool + munim-bluetooth event wiring
│   └── multipeerTransport.ts  iOS Multipeer adapter (iOS-to-iOS fast path)
├── radio/
│   └── radioOrchestrator.ts  Central/Peripheral time-slicing state machine
├── routing/
│   ├── peerRegistry.ts    Contact store + nearby peer tracking
│   └── router.ts          Epidemic routing + seen-packet LRU cache
├── messaging/
│   └── packetCodec.ts     PacketBuilder + PacketHandler + CausalMessageStore
├── MeshEngine.ts          Top-level orchestrator + public API
├── useMesh.ts             React hook
└── index.ts               Public barrel export
```

---

## Design Choices Analysis

All five choices from `design_choices.txt` are implemented. Here is how each maps to the code and what constraints apply on mobile:

### 1. Multi-Hop Security & Payload Decoupling ✅ Fully Implemented

**Envelope pattern**: every packet has a plaintext `RoutingHeader` (36 bytes) that any relay can read, and an `EncryptedPayload` that only the recipient can decrypt.

**Cryptography**: uses NaCl `box` (X25519 ephemeral DH + XSalsa20-Poly1305), which is equivalent in security to Signal's Double Ratchet for single messages. Full Double Ratchet (forward secrecy per message) is a natural next step — add it by storing a ratchet state in `Contact` and using `nacl.box` with a ratcheted ephemeral key on each send.

**Key exchange without a server**: supported two ways:
- QR code / out-of-band: call `engine.addContactFromPublicKey(theirPublicKey)` after scanning their QR.
- First-contact BLE HANDSHAKE: devices auto-exchange identity public keys over `CHAR_INBOX`; the app layer receives a `handshake:completed` event and can prompt the user to approve.

**Relevant files**: `crypto.ts`, `encoding.ts`, `packetCodec.ts`

---

### 2. Ephemeral Advertisement Rotation ✅ Implemented with iOS caveat

**Token derivation**: `HMAC-SHA256(sharedRootKey, "anon-mesh:token:v1|{windowIndex}")`, truncated to 8 bytes. Windows are 15 minutes. Contacts can compute expected tokens for ±1 windows to tolerate clock drift.

**What goes in the advertisement**:
- **Android**: the 8-byte token hash is included in `manufacturerData`. Scanning centrals extract it before connecting, enabling contact recognition without a GATT connection.
- **iOS (CoreBluetooth limitation)**: iOS public APIs only allow advertising `serviceUUIDs` and `localName`. The token hash cannot go in the advertisement payload. Instead, it is read from `CHAR_ANNOUNCE` immediately after connecting. This means iOS devices must connect first to identify a peer — a necessary trade-off given Apple's API restrictions.

**Self-advertisement**: a separate self-token (derived from the identity public key, not a per-contact shared key) is used in `CHAR_ANNOUNCE` so that the device's advertisement is unlinkable across rotation windows to strangers.

**Relevant files**: `crypto.ts` (`ownTokenForWindow`, `deriveRotatingToken`), `radioOrchestrator.ts`, `MeshEngine.ts` (`_rotateToken`)

---

### 3. Central/Peripheral Dual-Role Orchestration ✅ Fully Implemented

**State machine**: `RadioOrchestrator` time-slices the radio:
```
ADVERTISING (800ms) → SCANNING (1200ms) → ADVERTISING → ...
```
Both phases run continuously (munim-bluetooth keeps advertising active; the orchestrator only starts/stops scanning).

**Tie-breaker**: when two devices discover each other during the scan phase and both want to connect, `crypto.shouldActAsCentral()` compares their token hashes lexicographically. The device with the higher hash acts as Central and initiates the connection; the other stays in Peripheral mode. This is deterministic and symmetric — both devices arrive at the same answer.

**Connection pause**: `radioOrchestrator.pauseForConnection()` stops the scan loop while a GATT connection is being established, then `resumeFromConnection()` restarts it.

**Relevant files**: `radioOrchestrator.ts`, `crypto.ts` (`shouldActAsCentral`), `MeshEngine.ts` (`_handlePeerFound`)

---

### 4. Binary Serialisation & MTU Budgeting ✅ Fully Implemented

**Wire format**: no JSON, no string keys. All structures are packed into flat `Uint8Array`s.

| Structure          | Size       |
|--------------------|-----------|
| Routing header     | 36 bytes   |
| Chunk header       | 3 bytes    |
| NaCl nonce         | 24 bytes   |
| NaCl MAC overhead  | 16 bytes   |
| **Overhead total** | **79 bytes** |
| ANNOUNCE payload   | 16 bytes   |
| HANDSHAKE payload  | 64 bytes   |

**MTU negotiation**: on Android, `requestMTU(device, 247)` is called immediately after connection. iOS negotiates internally. The chunk payload size is computed as `MTU - 3 (ATT) - 3 (chunk header) = 241 bytes`.

**Chunking**: `encoding.chunkBytes()` splits any packet into 241-byte frames. `encoding.feedChunk()` reassembles them on the receiver side. Supports up to 255 chunks per packet (~61 KB), practically capped at 16 KB.

**Relevant files**: `encoding.ts`, `bleGattSession.ts`, `constants.ts`

---

### 5. Distributed Consistency (Causal Ordering) ✅ Implemented

**CausalMessageStore**: a per-conversation DAG where each `MeshMessage` references its causal predecessors (`parentIds`). `getSorted()` runs Kahn's topological sort and uses `timestampMs` as a stable tiebreaker for concurrent branches — analogous to Git's merge commit ordering.

**Frontier tracking**: `getFrontierIds()` returns the set of "leaf" message IDs — messages that have no children yet. This is used as `parentIds` when composing the next message, growing the DAG correctly.

**Network partitions**: if two users are separated and each sends messages independently, their branches will merge correctly when they reconnect. The display order will be causally consistent, not wall-clock-sorted.

**Relevant files**: `messaging/packetCodec.ts` (`CausalMessageStore`)

---

## Quick Start

```typescript
import { generateIdentityKeyPair } from 'anon-mesh-core'
import { useMesh, utf8Encode, utf8Decode, ContentType } from 'anon-mesh-core'

// Generate once and persist securely (e.g. in react-native-keychain)
const keyPair = generateIdentityKeyPair()

function ChatScreen() {
  const { engine, status, messages } = useMesh(keyPair)

  // Add a contact from their QR code
  const addContact = (theirPublicKeyHex: string) => {
    engine?.addContactFromPublicKey(hexToBytes(theirPublicKeyHex), 'Alice')
  }

  // Send a text message
  const sendText = async (contactId: string, text: string) => {
    await engine?.sendMessage(contactId, ContentType.TEXT, utf8Encode(text))
  }

  // Display messages (causally sorted)
  const thread = messages.get(contactId) ?? []
}
```

---

## Extending with New Features

The `ContentType` enum has a `CUSTOM = 0xFF` value. New feature types should:
1. Add a new `ContentType` variant in `core/types.ts`.
2. Define a `pack*()/unpack*()` function pair in `core/encoding.ts`.
3. Handle the new type in the app layer above `MeshEngine` — the engine itself remains transport-agnostic.

The same pattern applies to group chat (use a shared group ID as `conversationId` and broadcast to all group members), device locating (`ContentType.LOCATION_HINT` is already reserved), and any other feature.