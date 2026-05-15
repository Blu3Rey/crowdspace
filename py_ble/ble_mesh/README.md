# BLE Mesh Network

A modular, production-grade **Bluetooth Low Energy mesh network** built in Python on top of [`bleak`](https://github.com/hbldh/bleak) (central role) and [`bless`](https://github.com/kevincar/bless) (peripheral role).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Application                             │
│            DirectMessaging  GroupChat  DeviceLocator  Custom…   │
└───────────────────────────┬─────────────────────────────────────┘
                            │  Feature.handle(Packet)
┌───────────────────────────▼─────────────────────────────────────┐
│                        MeshNode                                 │
│  seq counter │ dedup cache │ routing table │ neighbour table     │
│  fragmentation ─ encryption ─ compression  │ heartbeat loop      │
└──────┬──────────────────────────────────────────┬───────────────┘
       │ send(raw)                       rx_handler│
┌──────▼──────────────────┐   ┌───────────────────▼─────────────┐
│     BLEPeripheral       │   │           BLECentral             │
│  (bless GATT server)    │   │   (bleak scanner + client)       │
│  ┌──────────────────┐   │   │  scan → connect → read INFO      │
│  │ INFO_CHAR (read) │   │   │  subscribe TX_CHAR notifications  │
│  │ RX_CHAR  (write) │◄──┼───┼── central writes mesh packets    │
│  │ TX_CHAR  (notify)│───┼──►│  peripheral notifies subscribed  │
│  └──────────────────┘   │   │  centrals                        │
└─────────────────────────┘   └──────────────────────────────────┘
          BLE GATT (Bluetooth Low Energy)
```

### Dual-role operation

Every mesh node **simultaneously**:
- Acts as a **GATT server** (peripheral) — advertising itself and accepting writes from peers
- Acts as a **GATT client** (central) — scanning for and connecting to other mesh nodes

This means a single BLE adapter must support multi-role (central + peripheral at the same time), which all modern adapters do.

### Packet wire format

```
Offset  Size  Field
──────  ────  ─────
     0     1  version      protocol version (0x01)
     1     1  msg_type     MsgType constant
     2     1  ttl          hops remaining
     3     1  flags        ACK_REQ | ENCRYPTED | FRAGMENTED | COMPRESSED | RELIABLE
     4    16  src_id       source node UUID (128-bit)
    20    16  dst_id       destination UUID (0xFF×16 = broadcast)
    36     4  seq_num      per-source uint32 sequence number
    40     2  payload_len  payload byte count
    42     2  checksum     CRC-16/CCITT-FALSE over bytes[0:42] + payload
    44     N  payload      application data
```

---

## Features

| Feature | Description |
|---------|-------------|
| **DirectMessaging** | Reliable unicast with ACK + configurable retry |
| **GroupChat** | Named-channel broadcast; tracks membership per group |
| **DeviceLocator** | RSSI-based proximity estimation; multi-hop locate |
| *Custom* | Extend `Feature` and handle any `MsgType.CUSTOM` sub-type |

### Built-in message types

```
Control:   DISCOVERY  HEARTBEAT  ACK  ROUTE_REQ  ROUTE_REPLY  FRAGMENT
Messaging: DIRECT_MSG  GROUP_JOIN  GROUP_LEAVE  GROUP_MSG
Locating:  LOC_REQ  LOC_RESP  LOC_REPORT
Extension: CUSTOM (sub_type byte in payload)
```

---

## Setup

### Prerequisites

| Platform | Requirement |
|----------|-------------|
| **Linux** | BlueZ ≥ 5.43 · Root or `CAP_NET_ADMIN` capability |
| **macOS** | macOS 10.15+ · BLE adapter supporting peripheral mode |
| **Windows** | Windows 10 build 1803+ · WinRT-compatible adapter |

The BLE adapter must support **dual-role** (peripheral + central simultaneously).  Most adapters with a Bluetooth 4.x or newer chipset qualify.

### Install

```bash
pip install bleak bless
# Optional: enable AES-256-GCM encryption
pip install cryptography
```

Or from the project root:

```bash
pip install -r ble_mesh/requirements.txt
```

---

## Quickstart

### Start a node (terminal 1)

```bash
python -m ble_mesh.cli --name Alice --join general
```

### Start a second node (terminal 2 / another device)

```bash
python -m ble_mesh.cli --name Bob --join general
```

### CLI commands

```
> nb                          # list neighbours
> msg <id_prefix> Hello!      # send a direct message (tab-complete from 'nb')
> grp join announcements      # join a group channel
> grp send general Hey all!   # broadcast to group
> loc                         # locate all peers (RSSI + estimated distance)
> loc a3f1                    # locate a specific node by ID prefix
> st                          # node status
> rt                          # routing table
> quit
```

---

## Programmatic usage

### Minimal node

```python
import asyncio
from ble_mesh import MeshNode, MeshConfig
from ble_mesh.features import DirectMessaging

async def main():
    cfg  = MeshConfig(node_name="Pi-Sensor", power_profile="low_power")
    node = MeshNode(cfg)
    msg  = DirectMessaging()

    @msg.on_message
    async def on_dm(src_id, text, msg_id):
        print(f"[{src_id.hex()[:8]}] {text}")

    node.register_feature(msg)
    await node.start()
    await node.run_forever()

asyncio.run(main())
```

### Encrypted group chat

```python
from ble_mesh import MeshNode, MeshConfig
from ble_mesh.features import GroupChat
from ble_mesh.utils.crypto import derive_key

key = derive_key("shared-passphrase")
cfg = MeshConfig(node_name="Secure-Node", enable_encryption=True, psk=key)
node = MeshNode(cfg)
chat = GroupChat()

@chat.on_message("ops")
async def on_msg(group_id, src_id, text, msg_id):
    print(f"#{group_id} {src_id.hex()[:8]}: {text}")

node.register_feature(chat)

async def main():
    await node.start()
    await chat.join("ops")
    await chat.send("ops", "Secure hello!")
    await node.run_forever()
```

### Device locating

```python
from ble_mesh import MeshNode, MeshConfig
from ble_mesh.features import DeviceLocator

node    = MeshNode(MeshConfig(node_name="Tracker"))
locator = DeviceLocator()
node.register_feature(locator)

async def main():
    await node.start()
    await asyncio.sleep(10)   # let peers connect
    report = await locator.locate(timeout=5.0)
    for e in report:
        print(f"{e['responder_name']:15} RSSI={e['rssi']:4} dBm  ~{e['distance_m']:.1f} m")
```

---

## Writing custom features

```python
import struct
from ble_mesh.features.base import Feature
from ble_mesh.core.protocol import MsgType, Flags

# Reserve a sub-type byte for your custom message
MY_SENSOR_TYPE = 0x01

class TemperatureSensor(Feature):
    """Broadcast temperature readings over the mesh."""

    # Tell the node which MsgType(s) to route to us
    handled_types = frozenset({MsgType.CUSTOM})
    name = "temperature-sensor"

    def __init__(self, interval: float = 60.0):
        super().__init__()
        self._interval = interval
        self._callbacks = []

    def on_reading(self, fn):
        self._callbacks.append(fn)
        return fn

    async def on_start(self):
        import asyncio
        asyncio.create_task(self._broadcast_loop())

    async def _broadcast_loop(self):
        import asyncio, random
        while True:
            temp = round(20.0 + random.uniform(-2, 2), 2)
            payload = bytes([MY_SENSOR_TYPE]) + struct.pack("!f", temp)
            await self.node.send(MsgType.CUSTOM, payload)
            await asyncio.sleep(self._interval)

    async def handle(self, packet):
        if not packet.payload or packet.payload[0] != MY_SENSOR_TYPE:
            return
        temp = struct.unpack("!f", packet.payload[1:5])[0]
        for cb in self._callbacks:
            await cb(packet.src_id, temp)

# Usage
sensor = TemperatureSensor(interval=30.0)

@sensor.on_reading
async def on_temp(src_id, celsius):
    print(f"Temp from {src_id.hex()[:8]}: {celsius:.2f} °C")

node.register_feature(sensor)
```

---

## Power profiles

| Profile | Scan interval | Scan duration | Heartbeat |
|---------|--------------|---------------|-----------|
| `low_power` | 20 s | 2 s | 60 s |
| `balanced` | 8 s | 4 s | 30 s |
| `high_performance` | 3 s | 8 s | 10 s |

---

## Security

- **Encryption**: AES-256-GCM via a 32-byte pre-shared key (PSK).
  All payload bytes are encrypted; the header (src/dst/seq) is authenticated
  as Additional Authenticated Data (AAD) to detect tampering.
- **Integrity**: Every packet is CRC-16/CCITT-FALSE protected regardless of
  encryption status.
- **Key distribution**: out of scope — use an out-of-band channel
  (QR code, NFC tap, provisioning app) or ECDH key agreement.

---

## Project structure

```
ble_mesh/
├── __init__.py          Public API surface
├── config.py            MeshConfig dataclass + power profiles
├── requirements.txt
├── core/
│   ├── node.py          MeshNode — main orchestrator
│   ├── protocol.py      UUIDs, MsgType, Flags, header format
│   ├── packet.py        Encode / decode + CRC + fragmentation
│   ├── neighbor.py      NeighborTable — RSSI, connection state
│   └── router.py        RoutingTable + DedupCache
├── transport/
│   ├── manager.py       Unified send + dual-role lifecycle
│   ├── peripheral.py    BLE GATT server  (bless)
│   └── central.py       BLE scanner + client  (bleak)
├── features/
│   ├── base.py          Feature abstract base class
│   ├── messaging.py     DirectMessaging  (unicast + ACK/retry)
│   ├── group_chat.py    GroupChat  (named-channel broadcast)
│   └── locating.py      DeviceLocator  (RSSI proximity)
├── utils/
│   ├── logger.py        Shared structured logger
│   └── crypto.py        AES-256-GCM wrapper (optional)
└── cli.py               Interactive CLI demo
```
