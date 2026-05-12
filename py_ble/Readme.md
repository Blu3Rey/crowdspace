# BLE Mesh Network

A highly modular, production-grade **Bluetooth Low Energy mesh networking** library for Python.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        MeshNode                              │
│  (orchestrator – owns all subsystems)                        │
│                                                              │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │ PacketFactory│  │  KeyManager  │  │  FeatureRegistry   │  │
│  │ (seq numbers)│  │ (AES-GCM /  │  │                    │  │
│  └─────────────┘  │  X25519 ECDH)│  │  ┌─────────────┐  │  │
│                   └──────────────┘  │  │  Messaging  │  │  │
│  ┌──────────────────────────────┐   │  ├─────────────┤  │  │
│  │         MeshRouter           │   │  │ Group Chat  │  │  │
│  │  • Flood  • Directed route   │   │  ├─────────────┤  │  │
│  │  • ACK/retry  • Route disco  │   │  │   Locator   │  │  │
│  │  • Fragment reassembly       │   │  ├─────────────┤  │  │
│  │  • Deduplication (seen cache)│   │  │  your feat. │  │  │
│  └──────────────────────────────┘   │  └─────────────┘  │  │
│                                     └────────────────────┘  │
│  ┌──────────────────────────────┐                           │
│  │          BLEManager          │                           │
│  │  Central (bleak)             │  Peripheral (bless)       │
│  │  • Scan for peers            │  • GATT server            │
│  │  • Connect & write           │  • Advertise service      │
│  │  • Subscribe notifications   │  • Notify subscribers     │
│  └──────────────────────────────┘                           │
└──────────────────────────────────────────────────────────────┘
```

## Packet Wire Format

```
 0       8      16      24      32      40      48
 ├──────┬───────┬───────┬──────────────────────────┤
 │Magic │ Ver   │ Type  │        Src Addr (6B)      │
 ├──────┴───────┴───────┴──────────────────────────┤
 │                   Dst Addr (6B)                  │
 ├─────────────────────────────────────────────────┤
 │              Group ID (4B)                       │
 ├─────────────────────────────────────────────────┤
 │              Seq Num (4B)                        │
 ├───────┬───────┬───────┬───────┬─────────────────┤
 │  TTL  │ Hops  │ Flags │ FIdx  │  FTotal  │PLen  │
 ├───────┴───────┴───────┴───────┴──────────┴──────┤
 │              Payload (variable)                  │
 ├─────────────────────────────────────────────────┤
 │             AES-GCM Tag (16B)                    │
 └─────────────────────────────────────────────────┘
```

## Installation

```bash
pip install -r requirements.txt
```

**Platform notes:**
- **Linux**: Full support, no additional setup. Requires BlueZ 5.43+.
- **macOS**: Requires macOS 10.15+ with Bluetooth 4.0+ hardware.
- **Windows**: Requires Windows 10 1809+ (BLE GATT server support).
- **Raspberry Pi**: Works natively with built-in BT. Run as root or add user to `bluetooth` group.

## Quick Start

### Run the CLI

```bash
# Node 1
python -m ble_mesh_network.cli --name Alice

# Node 2 (different machine, same network key for encrypted comms)
python -m ble_mesh_network.cli --name Bob --key <64-hex-char-network-key>
```

### Use as a library

```python
import asyncio
from ble_mesh_network import MeshNode, create_node

async def main():
    # Start a node with all default features
    node = await create_node(name="Alice")

    # ── Direct messaging ─────────────────────────────────────
    bob_addr = bytes.fromhex("AABBCCDDEEFF")

    # Receive messages
    async def on_msg(msg):
        print(f"From {msg.src_str}: {msg.body}")

    node.messaging.on_receive(on_msg)

    # Send a message (with delivery ACK)
    await node.messaging.send_text(bob_addr, "Hello, Bob!", reliable=True)

    # ── Group chat ───────────────────────────────────────────
    gid = await node.group_chat.create_room("Ops Channel")
    await node.group_chat.join(gid)

    async def on_group_msg(gm):
        print(f"[{gm.group_id}] {gm.src_str}: {gm.body}")

    node.group_chat.on_message(on_group_msg)
    await node.group_chat.send_message(gid, "Hello group!")

    # ── Device locating ──────────────────────────────────────
    estimate = await node.locator.locate(bob_addr, timeout=5.0)
    if estimate:
        print(f"Bob is ~{estimate.distance_from_origin:.1f}m away")
        print(f"Confidence: {estimate.confidence:.0%}")

    # Proximity alert
    async def on_proximity(addr, in_zone, dist):
        verb = "entered" if in_zone else "left"
        print(f"Bob {verb} the 5m zone (distance={dist:.1f}m)")

    node.locator.add_proximity_zone("bob-near", bob_addr, 5.0, on_proximity)

    # ── Ping / RTT ───────────────────────────────────────────
    rtt = await node.ping(bob_addr)
    print(f"RTT to Bob: {rtt:.1f}ms")

    # ── List peers ───────────────────────────────────────────
    for peer in node.peers():
        print(peer)

    await asyncio.sleep(60)
    await node.stop()

asyncio.run(main())
```

## Building Custom Features

Extend the mesh with your own feature plugin in 4 steps:

```python
# my_features/telemetry.py
import json
from ble_mesh_network.features.base import BaseFeature
from ble_mesh_network.core.packet import PacketType, PacketFlag, BROADCAST_ADDR

class TelemetryFeature(BaseFeature):
    NAME    = "telemetry"
    HANDLES = {PacketType.FEATURE_MSG}   # or define a new PacketType

    async def on_packet(self, pkt):
        data = json.loads(pkt.payload)
        if data.get("subtype") == "telemetry":
            print(f"Sensor data from {pkt.src_addr.hex()}: {data}")

    async def broadcast_reading(self, sensor_id: str, value: float):
        payload = json.dumps({
            "subtype":   "telemetry",
            "sensor_id": sensor_id,
            "value":     value,
        }).encode()
        pkt = self.make_packet(
            PacketType.FEATURE_MSG,
            payload  = payload,
            dst_addr = BROADCAST_ADDR,
            flags    = PacketFlag.ENCRYPTED,
        )
        await self.send(pkt)


# Register with a node
node = MeshNode(name="Sensor-01")
node.register_feature(TelemetryFeature(node))
```

## Packet Types

| Type            | Value | Description                            |
|-----------------|-------|----------------------------------------|
| HEARTBEAT       | 0x01  | Node alive broadcast + metadata        |
| ROUTE_REQUEST   | 0x02  | Flood: find path to destination        |
| ROUTE_REPLY     | 0x03  | Reply with known route                 |
| ACK             | 0x05  | Delivery acknowledgement               |
| PING / PONG     | 0x06/7| RTT measurement                        |
| DIRECT_MSG      | 0x10  | Unicast text/binary message            |
| GROUP_MSG       | 0x11  | Multicast group message                |
| BROADCAST_MSG   | 0x12  | Network-wide broadcast                 |
| RSSI_BEACON     | 0x20  | Location beacon                        |
| LOC_REQUEST/RSP | 0x21/2| Device location services               |
| FEATURE_MSG     | 0xF0  | Generic payload for custom features    |

## Security Model

- **Network key** (PSK): 256-bit AES-GCM key shared by all trusted nodes.
  Encrypts broadcast and group traffic.
- **Session keys**: per-peer X25519 ECDH → HKDF-derived 256-bit key.
  Encrypts unicast messages with forward secrecy.
- **Replay protection**: seen-packet cache keyed on (src_addr, seq_num).
- **Authenticated encryption**: AES-256-GCM — ciphertext integrity is verified
  before any routing or delivery decision.

## Routing

The router uses a **hybrid** strategy:

1. **Flooding** (default): packet is re-broadcast to all direct neighbours
   with TTL decremented. A seen-cache on every node prevents loops.
2. **Directed** (when route is known): packet sent only toward the best
   next-hop. Route cost = hops + RSSI penalty + loss penalty.
3. **Route discovery**: if no route is known, a ROUTE_REQUEST is flooded
   and the sender waits (≤ 3 s) for a ROUTE_REPLY before falling back
   to full flooding.
4. **Reliability**: RELIABLE-flagged packets are retransmitted (up to 3×,
   with exponential backoff) until an application-level ACK is received.

## CLI Reference

```
mesh> help

BLE Mesh Network – Commands

  status                        Node info and statistics
  peers                         List all known peers
  routes                        Show routing table

  msg <addr> <text>             Send a direct message
  bc <text>                     Broadcast to entire network
  history [addr]                Show message history

  group create <name>           Create a chat room
  group join <id>               Join a chat room by ID
  group leave <id>              Leave a chat room
  group msg <id> <text>         Send a group message
  group list                    List joined rooms
  group invite <id> <addr>      Invite a peer to a room

  locate <addr>                 Estimate peer's position
  ping <addr>                   Measure RTT to a peer

  quit                          Shutdown and exit
```