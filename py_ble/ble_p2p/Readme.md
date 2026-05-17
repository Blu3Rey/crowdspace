# ble_p2p — BLE Decentralized Mesh Messaging

A pure-Python, fully decentralised peer-to-peer messaging platform over
Bluetooth Low Energy (BLE), using `bleak` (central) and `bless` (peripheral).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        BLEMeshNode                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Peripheral   │  │ Central      │  │ Housekeeping     │  │
│  │ (bless GATT  │  │ (bleak scan  │  │ (fragment GC,    │  │
│  │  server,     │  │  + ephemeral │  │  DB pruning,     │  │
│  │  always on)  │  │  sessions)   │  │  peer expiry)    │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────────┘  │
│         │                 │                                  │
│  ┌──────▼─────────────────▼───────────────────────────────┐ │
│  │                      Router                            │ │
│  │  dispatch by FeatureID → Feature handlers              │ │
│  └──────────────────────────┬─────────────────────────────┘ │
│                             │                                │
│  ┌──────────────────────────▼─────────────────────────────┐ │
│  │                    Feature Layer                        │ │
│  │  DirectMessage (0x01) │ GroupChat (0x02) │ Locator(0x03)│ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  MessageStore (SQLite WAL)                           │   │
│  │  • outbound queue  • message history  • peer registry│   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Hybrid P2P — Ephemeral GATT Sessions

Every node simultaneously:
- **Advertises** as a GATT peripheral (always listening for inbound writes)
- **Scans** every 15 s as a central, connects to discovered peers,
  exchanges a burst of queued frames, then disconnects

This avoids the iOS/Android limit on simultaneous GATT connections while
still achieving reliable bidirectional delivery.

### Wire Protocol

Each message is fragmented into 244-byte BLE frames:

```
┌──────┬─────────┬──────┬───────┬──────┬─────┬──────────┬──────────┐
│MAGIC │ VERSION │ TYPE │ FLAGS │ SEQ  │FRAG │  SRC_ID  │  DST_ID  │
│  2B  │   1B    │  1B  │  1B   │  2B  │ 4B  │   8B     │   8B     │
├──────┴─────────┴──────┴───────┴──────┴─────┴──────────┴──────────┤
│ TIMESTAMP_MS (8B) │ PAYLOAD_LEN (2B) │ PAYLOAD (≤205B) │ CRC16(2B)│
└───────────────────────────────────────────────────────────────────┘
```

Total overhead: **39 bytes**. Max payload per fragment: **205 bytes**.

### GATT Service

| Characteristic | UUID suffix | Properties | Purpose |
|---|---|---|---|
| Write | `…e0a9-e50e24dcca9e` `…0002` | Write / Write-No-Response | Central → Peripheral frames |
| Notify | `…0003` | Notify + Read | Peripheral → Central frames |
| Info | `…0004` | Read | JSON device metadata |

---

## Installation

```bash
pip install bleak>=0.21.0 bless>=0.2.5
# or
pip install -r requirements.txt
```

> **Linux**: requires BlueZ ≥ 5.50 and a BLE-capable adapter.  
> **macOS**: CoreBluetooth — Bluetooth must be authorised in System Preferences.  
> **Windows**: WinRT BLE stack, Windows 10 build 1809+.  
> **Raspberry Pi**: works with built-in adapter; run with `sudo` or add user to `bluetooth` group.

---

## Quick Start

### Interactive CLI

```bash
python -m ble_p2p.cli                    # auto-generated name
python -m ble_p2p.cli --name "Alice"     # custom display name
python -m ble_p2p.cli --name "Bob" --debug
```

CLI commands:

| Command | Description |
|---|---|
| `status` | Show local device ID, name, uptime, peer count |
| `peers` | List known peers with RSSI / last-seen |
| `dm <id> <text>` | Direct message (ID prefix or name prefix) |
| `broadcast <text>` | Broadcast to all known peers |
| `history <id>` | Show DM history with a peer |
| `mkgroup <name> [ids…]` | Create a group chat |
| `groups` | List groups |
| `gc <gid> <text>` | Send to group |
| `locate` | Show proximity estimates for all peers |
| `ping <id>` | Measure round-trip latency to peer |
| `beacon <label>` | Broadcast a location label |
| `help` / `quit` | Help / exit |

### Embedded API

```python
import asyncio
from ble_p2p import BLEMeshNode
from ble_p2p.features.direct_message import DirectMessageFeature
from ble_p2p.features.group_chat import GroupChatFeature
from ble_p2p.features.device_locator import DeviceLocatorFeature

async def main():
    node = BLEMeshNode(name="Alice")

    dm = DirectMessageFeature(node)
    gc = GroupChatFeature(node)
    loc = DeviceLocatorFeature(node)
    node.register_feature(dm)
    node.register_feature(gc)
    node.register_feature(loc)

    # Callbacks
    @dm.on_message
    async def on_dm(from_name, from_id_hex, text, ts_ms):
        print(f"[DM] {from_name}: {text}")

    @gc.on_message
    async def on_gc(group_id, group_name, from_name, from_id_hex, text, ts_ms):
        print(f"[{group_name}] {from_name}: {text}")

    async with node:
        # Send a direct message
        peer_id = bytes.fromhex("aabbccdd11223344")
        await dm.send(peer_id, "Hello!")

        # Create a group and send
        gid = gc.create_group("Team", [peer_id])
        await gc.send(gid, "Hey team!")

        await asyncio.sleep(3600)   # run for an hour

asyncio.run(main())
```

---

## Writing a Custom Feature

```python
import json
from ble_p2p.features.base import Feature
from ble_p2p.constants import MsgType

class FileShareFeature(Feature):
    feature_id = 0x81          # 0x80–0xFF reserved for user features

    async def handle_message(self, body: bytes, src_id: bytes, src_name: str):
        data = json.loads(body[1:])   # byte 0 is feature_id
        print(f"File offer from {src_name}: {data['filename']} ({data['size']} bytes)")

    async def offer(self, dst_id: bytes, filename: str, size: int):
        payload = self.encode_payload(
            json.dumps({"filename": filename, "size": size}).encode()
        )
        await self.node.send_message(MsgType.FEATURE, payload, dst_id)

# Register
fs = FileShareFeature(node)
node.register_feature(fs)
await fs.offer(peer_id, "photo.jpg", 204800)
```

`encode_payload()` prepends the `feature_id` byte so the router can
dispatch correctly on the receiving side.

---

## Project Structure

```
ble_p2p/
├── __init__.py              Public API (BLEMeshNode, version)
├── constants.py             UUIDs, enums (MsgType, MsgFlags, FeatureID, Capability)
├── message.py               Binary frame pack/unpack, CRC-16, build_frame/parse_frame
├── protocol.py              Fragmentation, reassembly, deduplication, FragmentBuffer
├── device.py                LocalDevice — persistent identity (~/.ble_p2p/device.json)
├── node.py                  BLEMeshNode — orchestrator, 3 async tasks
├── cli.py                   Interactive REPL with colour output
├── transport/
│   ├── peripheral.py        BLEPeripheral — bless GATT server
│   └── central.py           BLECentral — bleak scanner + ephemeral sessions
├── network/
│   ├── peer.py              Peer dataclass + PeerRegistry (thread-safe, dual-index)
│   └── router.py            Router — feature dispatch, ACK waiters, handler table
├── storage/
│   └── store.py             MessageStore — SQLite WAL, outbound queue, history
└── features/
    ├── base.py              Feature ABC
    ├── direct_message.py    DirectMessageFeature (DM + broadcast)
    ├── group_chat.py        GroupChatFeature (create/join/leave/sync)
    └── device_locator.py    DeviceLocatorFeature (RSSI, ping, beacon)
```

---

## Known Limitations & Roadmap

| Item | Status |
|---|---|
| Relay / multi-hop forwarding | Stub — `RELAY` flag detected, re-enqueue TODO |
| End-to-end encryption | Flag defined (`MsgFlags.ENCRYPTED`), NaCl secretbox planned |
| iOS peripheral mode | bless on iOS not yet supported |
| Large file transfer | Fragment reassembly works; chunked streaming feature planned |
| `__main__.py` entry point | `python -m ble_p2p.cli` works; `python -m ble_p2p` alias TODO |

---

## License

MIT