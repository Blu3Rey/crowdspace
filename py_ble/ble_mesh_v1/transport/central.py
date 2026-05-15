"""
transport/central.py — BLE central role (scanner + GATT client) using ``bleak``.

Scanning
--------
The scanner continuously looks for BLE devices advertising our
:data:`MESH_SERVICE_UUID`.  New devices trigger a connection attempt.

Connections
-----------
For each mesh peer discovered we:
  1. Connect with :class:`bleak.BleakClient`.
  2. Read INFO_CHAR to learn the peer's ``node_id`` and ``name``.
  3. Subscribe to TX_CHAR notifications (inbound data from the peer).
  4. Register the connection in the :class:`~ble_mesh.core.neighbor.NeighborTable`.

Connection teardown is handled gracefully; the entry is removed from the
neighbor table and routes through that peer are invalidated.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, Optional

from bleak import BleakClient, BleakScanner  # type: ignore
from bleak.backends.device import BLEDevice  # type: ignore

from ..core.protocol import (
    INFO_CHAR_UUID,
    MESH_SERVICE_UUID,
    RX_CHAR_UUID,
    TX_CHAR_UUID,
)
from ..core.neighbor import Neighbor, NeighborTable
from ..utils.logger import log

# Callable(raw_bytes, peer_address) → Awaitable[None]
RxHandler = Callable[[bytes, str], Awaitable[None]]
# Callable(neighbor) → Awaitable[None]
EventHandler = Callable[[Neighbor], Awaitable[None]]


class BLECentral:
    """BLE scanner and GATT client manager.

    Parameters
    ----------
    node_id : bytes
        Our own node ID — used to avoid connecting to ourselves.
    neighbors : NeighborTable
        Shared table updated when peers are found / lost.
    max_connections : int
        Cap on simultaneous BLE central connections.
    connection_timeout : float
        Seconds before a connection attempt is aborted.
    """

    def __init__(
        self,
        node_id: bytes,
        neighbors: NeighborTable,
        max_connections: int = 7,
        connection_timeout: float = 10.0,
    ) -> None:
        self._own_id            = node_id
        self._neighbors         = neighbors
        self._max_conn          = max_connections
        self._conn_timeout      = connection_timeout

        # address → asyncio.Task (connection coroutine in flight)
        self._pending: Dict[str, asyncio.Task]    = {}
        # address → BleakClient (active connections)
        self._clients: Dict[str, BleakClient]     = {}

        self._rx_handler:        Optional[RxHandler]    = None
        self._on_connected:      Optional[EventHandler] = None
        self._on_disconnected:   Optional[EventHandler] = None

        self._scanner: Optional[BleakScanner] = None
        self._running = False

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def set_rx_handler(self, handler: RxHandler) -> None:
        self._rx_handler = handler

    def set_connected_handler(self, handler: EventHandler) -> None:
        self._on_connected = handler

    def set_disconnected_handler(self, handler: EventHandler) -> None:
        self._on_disconnected = handler

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start_scanning(self, scan_duration: float, scan_interval: float) -> None:
        """Begin periodic scanning for mesh peers."""
        self._running = True
        log.info("[Central] Scanning for mesh peers (%.1fs every %.1fs).",
                 scan_duration, scan_interval)
        while self._running:
            await self._scan_once(scan_duration)
            await asyncio.sleep(scan_interval)

    async def stop(self) -> None:
        self._running = False
        if self._scanner:
            await self._scanner.stop()
        # Disconnect all active clients
        for addr in list(self._clients.keys()):
            await self._disconnect(addr)
        log.info("[Central] Stopped.")

    # ── Send ──────────────────────────────────────────────────────────────────

    async def send_to(self, address: str, data: bytes) -> bool:
        """Write *data* to the RX characteristic of the peer at *address*."""
        client = self._clients.get(address)
        if client is None or not client.is_connected:
            return False
        try:
            # write_without_response for speed; use response=True for reliability
            await client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except Exception as exc:
            log.warning("[Central] send_to %s failed: %s", address, exc)
            await self._disconnect(address)
            return False

    async def send_to_all(self, data: bytes) -> int:
        """Broadcast *data* to every connected peer.  Returns success count."""
        sent = 0
        for addr in list(self._clients.keys()):
            if await self.send_to(addr, data):
                sent += 1
        return sent

    # ── Internal scanning ─────────────────────────────────────────────────────

    async def _scan_once(self, duration: float) -> None:
        """Perform one scan burst and initiate connections to new mesh nodes."""
        try:
            devices = await BleakScanner.discover(
                timeout=duration,
                return_adv=True,
                service_uuids=[MESH_SERVICE_UUID],
            )
        except Exception as exc:
            log.warning("[Central] Scan error: %s", exc)
            return

        for device, adv_data in (devices.values() if isinstance(devices, dict) else []):
            # Skip ourselves and already-connected peers
            if device.address in self._clients or device.address in self._pending:
                continue
            # Update RSSI in neighbour table (even before connecting)
            n = self._neighbors.get_by_address(device.address)
            if n:
                n.update(adv_data.rssi or -100, device.name)
            # Respect connection cap
            if len(self._clients) >= self._max_conn:
                continue
            # Kick off connection in background
            task = asyncio.create_task(
                self._connect(device, adv_data.rssi or -100),
                name=f"connect-{device.address}",
            )
            self._pending[device.address] = task

    # ── Connection management ─────────────────────────────────────────────────

    async def _connect(self, device: BLEDevice, rssi: int) -> None:
        addr = device.address
        log.info("[Central] Connecting to %s (%s, %ddBm)…", device.name, addr, rssi)
        try:
            client = BleakClient(
                device,
                disconnected_callback=lambda c: asyncio.ensure_future(
                    self._on_disconnect_event(c.address)
                ),
                timeout=self._conn_timeout,
            )
            await client.connect()
        except Exception as exc:
            log.warning("[Central] Connection to %s failed: %s", addr, exc)
            self._pending.pop(addr, None)
            return

        # ── Read INFO_CHAR to learn node_id + name ────────────────────────────
        try:
            info_raw = bytes(await client.read_gatt_char(INFO_CHAR_UUID))
            peer_id  = info_raw[:16]
            peer_name = info_raw[16:].decode("utf-8", errors="replace").rstrip("\x00")
        except Exception as exc:
            log.warning("[Central] Could not read INFO from %s: %s — disconnecting.", addr, exc)
            await client.disconnect()
            self._pending.pop(addr, None)
            return

        # Avoid connecting to ourselves
        if peer_id == self._own_id:
            await client.disconnect()
            self._pending.pop(addr, None)
            return

        # ── Subscribe to TX_CHAR notifications ────────────────────────────────
        def _notify_cb(_, data: bytearray) -> None:
            raw = bytes(data)
            if self._rx_handler:
                loop = asyncio.get_event_loop()
                asyncio.run_coroutine_threadsafe(
                    self._rx_handler(raw, addr), loop
                )

        try:
            await client.start_notify(TX_CHAR_UUID, _notify_cb)
        except Exception as exc:
            log.warning("[Central] TX_CHAR subscribe on %s failed: %s", addr, exc)
            await client.disconnect()
            self._pending.pop(addr, None)
            return

        # ── Register ──────────────────────────────────────────────────────────
        self._clients[addr] = client
        self._pending.pop(addr, None)

        neighbor = self._neighbors.upsert(
            peer_id, addr, name=peer_name, rssi=rssi, is_connected=True, client=client
        )
        log.info("[Central] Connected: %s (%s)", peer_name, peer_id.hex()[:8])

        if self._on_connected:
            asyncio.ensure_future(self._on_connected(neighbor))

    async def _disconnect(self, address: str) -> None:
        client = self._clients.pop(address, None)
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _on_disconnect_event(self, address: str) -> None:
        self._clients.pop(address, None)
        neighbor = self._neighbors.get_by_address(address)
        if neighbor:
            neighbor.is_connected = False
            neighbor.client = None
            log.info("[Central] Disconnected: %s (%s)", neighbor.name, address)
            if self._on_disconnected:
                asyncio.ensure_future(self._on_disconnected(neighbor))

    @property
    def connection_count(self) -> int:
        return len(self._clients)

    @property
    def connected_addresses(self):
        return list(self._clients.keys())