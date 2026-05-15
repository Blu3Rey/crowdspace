"""
core/ble_manager.py
===================
Hardware abstraction layer for BLE operations.

Uses:
  • bleak  - Central/client role (scanning, connecting, writing)
  • bless  - Peripheral/server role (advertising, GATT server)

Each node simultaneously acts as:
  [Peripheral]  Advertises a GATT service, accepts writes → receive packets
  [Central]     Scans for peers, connects, writes packets → transmit packets
"""

from __future__ import annotations
import asyncio
import logging
import os
import struct
import time
from typing import Callable, Dict, List, Optional, Set, Awaitable

from bleak import BleakScanner, BleakClient, BleakError
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bless import BlessServer, BlessGATTCharacteristic, GATTCharacteristicProperties, GATTAttributePermissions

log = logging.getLogger(__name__)


# ── UUIDs ─────────────────────────────────────────────────────────────────────

MESH_SERVICE_UUID    = "12345678-1234-5678-1234-56789abcdef0"
RX_CHAR_UUID         = "12345678-1234-5678-1234-56789abcdef1"   # write here to send us data
TX_CHAR_UUID         = "12345678-1234-5678-1234-56789abcdef2"   # we notify here when we send
HEARTBEAT_CHAR_UUID  = "12345678-1234-5678-1234-56789abcdef3"   # readable node metadata

SCAN_TIMEOUT         = 5.0    # seconds per scan sweep
CONNECT_TIMEOUT      = 10.0
MAX_MTU              = 512
MAX_WRITE_SIZE       = 244    # safe BLE 5 default without negotiation
PEER_INACTIVE_SEC    = 45.0
RECONNECT_DELAY_SEC  = 5.0


# ── Callbacks type ────────────────────────────────────────────────────────────

PacketCallback   = Callable[[bytes, bytes, int], Awaitable[None]]  # data, src_addr, rssi
DetectedCallback = Callable[[bytes, str, int], Awaitable[None]]    # addr, name, rssi


# ── BLE Manager ───────────────────────────────────────────────────────────────

class BLEManager:
    """
    Manages all BLE hardware interactions.

    Lifecycle:
      await manager.start()   → begin advertising + scanning loop
      await manager.stop()    → graceful shutdown
      await manager.send(addr, data) → write to peer GATT characteristic
    """

    def __init__(
        self,
        local_addr:      bytes,
        node_name:       str,
        on_packet:       PacketCallback,
        on_peer_detected: DetectedCallback,
    ):
        self._local_addr       = local_addr
        self._node_name        = node_name
        self._on_packet        = on_packet
        self._on_peer_detected = on_peer_detected

        self._server:     Optional[BlessServer]            = None
        self._clients:    Dict[str, BleakClient]           = {}  # addr_str → client
        self._known_devs: Dict[str, BLEDevice]             = {}  # addr_str → device
        self._running     = False
        self._tasks:      List[asyncio.Task]               = []

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self):
        """Start peripheral (advertising) and central (scanning) roles."""
        self._running = True
        log.info("[BLE] Starting BLE manager for node %s", self._addr_str(self._local_addr))
        await self._start_peripheral()
        self._tasks.append(asyncio.create_task(self._scan_loop(), name="ble-scan"))
        self._tasks.append(asyncio.create_task(self._connection_housekeeping(), name="ble-housekeeping"))

    async def stop(self):
        """Gracefully stop all BLE activity."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        for client in list(self._clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass
        if self._server:
            await self._server.stop()
        log.info("[BLE] Stopped.")

    async def send(self, peer_addr: bytes, data: bytes) -> bool:
        """
        Send raw bytes to a peer's RX characteristic.
        Handles chunking for payloads exceeding MAX_WRITE_SIZE.
        Returns True on success.
        """
        addr_str = self._addr_str(peer_addr)
        client   = await self._get_or_connect(addr_str)
        if client is None:
            log.warning("[BLE] Cannot reach %s - no connection", addr_str)
            return False

        try:
            for chunk in self._chunk(data):
                await client.write_gatt_char(RX_CHAR_UUID, chunk, response=False)
            return True
        except BleakError as e:
            log.warning("[BLE] Write to %s failed: %s", addr_str, e)
            await self._drop_client(addr_str)
            return False

    async def broadcast_notify(self, data: bytes):
        """Notify all connected peers via the TX characteristic."""
        if self._server is None:
            return
        try:
            # bless update_value triggers notifications to all subscribed clients
            for chunk in self._chunk(data):
                self._server.get_characteristic(TX_CHAR_UUID).value = bytearray(chunk)
                await self._server.notify(TX_CHAR_UUID)
        except Exception as e:
            log.debug("[BLE] Notify error: %s", e)

    @property
    def connected_peers(self) -> List[str]:
        return list(self._clients.keys())

    # ── Peripheral (GATT Server) ──────────────────────────────────────────────

    async def _start_peripheral(self):
        loop   = asyncio.get_event_loop()
        server = BlessServer(name=self._node_name, loop=loop)
        server.read_request_func  = self._handle_read
        server.write_request_func = self._handle_write

        await server.add_new_service(MESH_SERVICE_UUID)

        # RX characteristic: peers write packets here
        rx_props  = (GATTCharacteristicProperties.write |
                     GATTCharacteristicProperties.write_without_response)
        rx_perms  = GATTAttributePermissions.writeable
        await server.add_new_characteristic(
            MESH_SERVICE_UUID, RX_CHAR_UUID, rx_props, None, rx_perms
        )

        # TX characteristic: we notify peers of outgoing packets
        tx_props  = (GATTCharacteristicProperties.notify |
                     GATTCharacteristicProperties.read)
        tx_perms  = GATTAttributePermissions.readable
        await server.add_new_characteristic(
            MESH_SERVICE_UUID, TX_CHAR_UUID, tx_props, None, tx_perms
        )

        # Heartbeat: readable node metadata
        hb_props  = GATTCharacteristicProperties.read
        hb_perms  = GATTAttributePermissions.readable
        hb_value  = bytearray(self._local_addr + self._node_name.encode()[:28])
        await server.add_new_characteristic(
            MESH_SERVICE_UUID, HEARTBEAT_CHAR_UUID, hb_props, hb_value, hb_perms
        )

        await server.start()
        self._server = server
        log.info("[BLE] Peripheral advertising as '%s'", self._node_name)

    def _handle_read(
        self, characteristic: BlessGATTCharacteristic, **kwargs
    ) -> bytearray:
        return characteristic.value or bytearray()

    def _handle_write(
        self,
        characteristic: BlessGATTCharacteristic,
        value: any,
        **kwargs,
    ):
        """Invoked by bless when a peer writes to our RX characteristic."""
        if characteristic.uuid.lower() == RX_CHAR_UUID.lower():
            data = bytes(value)
            # Schedule the coroutine on the event loop
            asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.ensure_future,
                self._on_packet(data, self._local_addr, 0),
            )

    # ── Central (Scanner / Client) ────────────────────────────────────────────

    # async def _scan_loop(self):
    #     """Continuously scan for mesh peers and notify the application layer."""
    #     while self._running:
    #         try:
    #             devices = await BleakScanner.discover(
    #                 timeout        = SCAN_TIMEOUT,
    #                 service_uuids  = [MESH_SERVICE_UUID],
    #             )
    #             for dev in devices:
    #                 addr = self._parse_addr(dev.address)
    #                 if addr == self._local_addr:
    #                     continue
    #                 rssi = dev.rssi or -100
    #                 self._known_devs[dev.address.upper()] = dev
    #                 await self._on_peer_detected(addr, dev.name or "", rssi)
    #         except asyncio.CancelledError:
    #             break
    #         except Exception as e:
    #             log.debug("[BLE] Scan error: %s", e)
    #         await asyncio.sleep(1)

    async def _scan_loop(self):
        """Continuously scan for mesh peers and notify the application layer."""

        def detect_cb(dev: BLEDevice, adv_data: AdvertisementData):
            # 1. Filter by UUID
            if MESH_SERVICE_UUID.lower() not in [u.lower() for u in adv_data.service_uuids]:
                return
            
            addr = self._parse_addr(dev.address)
            if addr == self._local_addr:
                return
            
            # 2. Get RSSI
            rssi = adv_data.rssi or -100
            self._known_devs[dev.address.upper()] = dev

            asyncio.create_task(self._on_peer_detected(addr, dev.name or "", rssi))
        
        while self._running:
            try:
                async with BleakScanner(detection_callback=detect_cb):
                    await asyncio.sleep(SCAN_TIMEOUT)
            except asyncio.CancelledError:
                log.debug("[BLE] Scan loop cancelled")
            except Exception as e:
                log.debug("[BLE] Scan error: %s", e)
                await asyncio.sleep(2)

    async def _get_or_connect(self, addr_str: str) -> Optional[BleakClient]:
        """Return an existing client or establish a new connection."""
        if addr_str in self._clients:
            client = self._clients[addr_str]
            if client.is_connected:
                return client
            await self._drop_client(addr_str)

        dev = self._known_devs.get(addr_str)
        if dev is None:
            return None

        try:
            client = BleakClient(dev, timeout=CONNECT_TIMEOUT)
            await client.connect()
            self._clients[addr_str] = client
            log.info("[BLE] Connected to %s", addr_str)

            # Subscribe to TX notifications
            await client.start_notify(TX_CHAR_UUID, self._notification_handler)
            return client
        except (BleakError, asyncio.TimeoutError) as e:
            log.debug("[BLE] Connect to %s failed: %s", addr_str, e)
            return None

    def _notification_handler(self, _char, data: bytearray):
        """Receive notifications from peer's TX characteristic."""
        asyncio.get_event_loop().call_soon_threadsafe(
            asyncio.ensure_future,
            self._on_packet(bytes(data), self._local_addr, 0),
        )

    async def _drop_client(self, addr_str: str):
        client = self._clients.pop(addr_str, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
            log.debug("[BLE] Disconnected %s", addr_str)

    async def _connection_housekeeping(self):
        """Periodically clean up stale connections."""
        while self._running:
            await asyncio.sleep(15)
            stale = [a for a, c in self._clients.items() if not c.is_connected]
            for a in stale:
                await self._drop_client(a)

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _addr_str(addr: bytes) -> str:
        return ":".join(f"{b:02X}" for b in addr).upper()

    @staticmethod
    def _parse_addr(addr_str: str) -> bytes:
        """Convert 'AA:BB:CC:DD:EE:FF' to 6-byte bytes."""
        parts = addr_str.upper().replace("-", ":").split(":")
        return bytes(int(p, 16) for p in parts[:6])

    @staticmethod
    def _chunk(data: bytes) -> List[bytes]:
        """Split data into MAX_WRITE_SIZE-byte chunks."""
        return [data[i: i + MAX_WRITE_SIZE] for i in range(0, len(data), MAX_WRITE_SIZE)]