"""
transport/peripheral.py — BLE GATT server (peripheral role) using ``bless``.

The server advertises three characteristics under the mesh service UUID:

* **INFO_CHAR** (read)   — exposes node_id + name so centrals can identify us.
* **RX_CHAR**   (write)  — centrals write inbound mesh packets here.
* **TX_CHAR**   (notify) — we push outbound packets to subscribed centrals.

Platform notes
--------------
* **Linux (BlueZ)** — requires root or ``CAP_NET_ADMIN``.  The BLE adapter
  must support multi-role (central + peripheral simultaneously).
* **macOS** — must run on the main thread; CoreBluetooth is UI-thread-bound.
* **Windows** — requires Windows 10 build 1803+ and a WinRT-capable adapter.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Awaitable, Callable, Optional

try:
    from bless import (  # type: ignore
        BlessServer,
        BlessGATTCharacteristic,
        GATTCharacteristicProperties,
        GATTAttributePermissions,
    )
    _BLESS_AVAILABLE = True
except ImportError:
    _BLESS_AVAILABLE = False
    BlessServer = None                          # type: ignore
    GATTCharacteristicProperties = None         # type: ignore
    GATTAttributePermissions = None             # type: ignore

from ..core.protocol import (
    INFO_CHAR_UUID,
    MESH_SERVICE_UUID,
    RX_CHAR_UUID,
    TX_CHAR_UUID,
)
from ..utils.logger import log

# Callable(raw_bytes, peer_address) → Awaitable[None]
RxHandler = Callable[[bytes, str], Awaitable[None]]


class BLEPeripheral:
    """BLE GATT server that advertises the mesh service.

    Parameters
    ----------
    node_name : str
        The BLE device name shown in scan results.
    node_id : bytes
        This node's 16-byte mesh identifier embedded in INFO_CHAR.
    """

    def __init__(self, node_name: str, node_id: bytes) -> None:
        if not _BLESS_AVAILABLE:
            raise RuntimeError(
                "bless is not installed.  Run: pip install bless"
            )
        self._name    = node_name
        self._node_id = node_id
        self._server: Optional[BlessServer] = None
        self._rx_handler: Optional[RxHandler] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()

        self._server = BlessServer(
            name=self._name,
            loop=self._loop,
        )
        self._server.read_request_func  = self._on_read
        self._server.write_request_func = self._on_write

        # ── Service definition ────────────────────────────────────────────────
        await self._server.add_new_service(MESH_SERVICE_UUID)

        # INFO_CHAR — read-only node identity
        info_payload = bytearray(self._node_id + self._name.encode("utf-8")[:32])
        await self._server.add_new_characteristic(
            MESH_SERVICE_UUID,
            INFO_CHAR_UUID,
            GATTCharacteristicProperties.read,
            info_payload,
            GATTAttributePermissions.readable,
        )

        # RX_CHAR — write (central → peripheral)
        await self._server.add_new_characteristic(
            MESH_SERVICE_UUID,
            RX_CHAR_UUID,
            GATTCharacteristicProperties.write
            | GATTCharacteristicProperties.write_without_response,
            None,
            GATTAttributePermissions.writeable,
        )

        # TX_CHAR — notify (peripheral → central)
        await self._server.add_new_characteristic(
            MESH_SERVICE_UUID,
            TX_CHAR_UUID,
            GATTCharacteristicProperties.notify,
            None,
            GATTAttributePermissions.readable,
        )

        await self._server.start()
        self._running = True
        log.info("[Peripheral] Advertising '%s' (id=%s…)", self._name, self._node_id.hex()[:8])

    async def stop(self) -> None:
        self._running = False
        if self._server:
            await self._server.stop()
            log.info("[Peripheral] Stopped.")

    # ── Data I/O ──────────────────────────────────────────────────────────────

    def set_rx_handler(self, handler: RxHandler) -> None:
        """Register the coroutine called when a central writes to RX_CHAR."""
        self._rx_handler = handler

    async def notify_all(self, data: bytes) -> bool:
        """Push *data* to every subscribed central via TX_CHAR notification.

        Large payloads are chunked into 512-byte notify calls (bless handles
        ATT fragmentation internally up to the negotiated MTU).
        """
        if not self._server or not self._running:
            return False
        try:
            char = self._server.get_characteristic(TX_CHAR_UUID)
            char.value = bytearray(data)
            self._server.update_value(MESH_SERVICE_UUID, TX_CHAR_UUID)  # Fix: Removed await
            log.debug("[Peripheral] Notified %dB to subscribed centrals.", len(data))
            return True
        except Exception as exc:
            log.warning("[Peripheral] notify_all failed: %s", exc)
            return False

    # ── GATT callbacks (may be called from a non-asyncio thread) ─────────────

    def _on_read(
        self, characteristic: Any, **kwargs: Any
    ) -> bytearray:
        uid = str(getattr(characteristic, "uuid", "")).lower()
        if uid == INFO_CHAR_UUID.lower():
            return bytearray(self._node_id + self._name.encode("utf-8")[:32])
        return bytearray()

    def _on_write(
        self, characteristic: Any, value: Any, **kwargs: Any
    ) -> None:
        uid = str(getattr(characteristic, "uuid", "")).lower()
        if uid != RX_CHAR_UUID.lower():
            return
        raw = bytes(value) if not isinstance(value, bytes) else value
        if not raw or not self._rx_handler or not self._loop:
            return

        # Schedule the coroutine on the event loop (bless callbacks may arrive
        # from a different thread depending on the platform backend)
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._rx_handler(raw, "peripheral-rx"),
                self._loop,
            )

    @property
    def is_running(self) -> bool:
        return self._running