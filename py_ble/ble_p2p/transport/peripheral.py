"""
transport/peripheral.py — BLE GATT Server (Peripheral role).

Uses the `bless` library to:
  • Advertise our custom SERVICE_UUID so scanners can identify us.
  • Accept writes on CHAR_WRITE_UUID  (messages FROM remote centrals TO us).
  • Send notifications on CHAR_NOTIFY_UUID (messages FROM us TO remote centrals).
  • Expose device info on CHAR_INFO_UUID (read-only; centrals read this on first
    encounter to learn our device_id / name / capabilities without connecting).

Threading note
--------------
`bless` callbacks arrive on an internal thread (especially on macOS and Windows).
We must never mutate asyncio state from those threads directly.  Instead we use
`loop.call_soon_threadsafe(asyncio.ensure_future, coro)` to schedule work safely.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Optional

from bless import (                                        # type: ignore[import]
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

from ..constants import (
    SERVICE_UUID, CHAR_WRITE_UUID, CHAR_NOTIFY_UUID, CHAR_INFO_UUID,
)

log = logging.getLogger(__name__)

# Callback type: async (data: bytes, source: str) → None
DataCallback = Callable[[bytes, str], Coroutine]


class BLEPeripheral:
    """
    Manages the local GATT server lifetime.

    Usage
    -----
    peripheral = BLEPeripheral(name="MyNode", info_payload=device.info_payload())
    peripheral.set_data_callback(my_async_handler)
    await peripheral.start()
    ...
    await peripheral.notify(raw_frame_bytes)
    ...
    await peripheral.stop()
    """

    def __init__(self, name: str, info_payload: bytes):
        self._name         : str            = name
        self._info_payload : bytes          = info_payload
        self._server       : Optional[BlessServer] = None
        self._loop         : Optional[asyncio.AbstractEventLoop] = None
        self._data_cb      : Optional[DataCallback] = None
        self._running      : bool           = False

    # ── Public API ────────────────────────────────────────────

    def set_data_callback(self, callback: DataCallback):
        """
        Register an async callable that is invoked whenever a central writes
        data to CHAR_WRITE_UUID.

        Signature: async def handler(data: bytes, source: str) → None
        The *source* is always "peripheral" (bless does not expose the
        client's BLE address in write callbacks).
        """
        self._data_cb = callback

    async def start(self):
        """Start advertising and accepting connections."""
        if self._running:
            return

        self._loop   = asyncio.get_running_loop()
        self._server = BlessServer(
            name=self._name,
            loop=self._loop,
        )
        self._server.read_request_func  = self._on_read
        self._server.write_request_func = self._on_write

        # ── Add our service ───────────────────────────────────
        await self._server.add_new_service(SERVICE_UUID)

        # CHAR_WRITE_UUID — Write / Write-No-Response
        write_props  = (
            GATTCharacteristicProperties.write
            | GATTCharacteristicProperties.write_without_response
        )
        write_perms  = GATTAttributePermissions.writeable
        await self._server.add_new_characteristic(
            SERVICE_UUID, CHAR_WRITE_UUID,
            write_props, bytearray(), write_perms,
        )

        # CHAR_NOTIFY_UUID — Notify + Read (central subscribes via CCCD)
        notify_props = (
            GATTCharacteristicProperties.notify
            | GATTCharacteristicProperties.read
        )
        notify_perms = GATTAttributePermissions.readable
        await self._server.add_new_characteristic(
            SERVICE_UUID, CHAR_NOTIFY_UUID,
            notify_props, bytearray(), notify_perms,
        )

        # CHAR_INFO_UUID — Read-only device metadata
        info_props   = GATTCharacteristicProperties.read
        info_perms   = GATTAttributePermissions.readable
        await self._server.add_new_characteristic(
            SERVICE_UUID, CHAR_INFO_UUID,
            info_props, bytearray(self._info_payload), info_perms,
        )

        await self._server.start()
        self._running = True
        log.info("GATT server started — advertising as %r", self._name)

    async def stop(self):
        """Stop advertising and close all connections."""
        if self._server and self._running:
            try:
                await self._server.stop()
            except Exception as exc:
                log.debug("Server stop error (ignored): %s", exc)
            self._running = False
            log.info("GATT server stopped")

    async def notify(self, data: bytes) -> bool:
        """
        Push *data* to all centrals currently subscribed to CHAR_NOTIFY_UUID.

        Returns True if the notification was dispatched without error.
        Silently returns False if the server is not running or no clients are
        subscribed (bless handles the subscription list internally).
        """
        if not self._running or self._server is None:
            return False
        try:
            char = self._server.get_characteristic(CHAR_NOTIFY_UUID)
            char.value = bytearray(data)
            await self._server.notify(CHAR_NOTIFY_UUID)
            return True
        except Exception as exc:
            log.debug("Notify error (ignored): %s", exc)
            return False

    def update_info_payload(self, new_payload: bytes):
        """Hot-update the device info characteristic value."""
        self._info_payload = new_payload
        if self._running and self._server:
            try:
                char = self._server.get_characteristic(CHAR_INFO_UUID)
                char.value = bytearray(new_payload)
            except Exception:
                pass

    @property
    def is_running(self) -> bool:
        return self._running

    # ── bless Callbacks (called from bless's internal thread) ─

    def _on_read(
        self,
        characteristic: BlessGATTCharacteristic,
        **kwargs: Any,
    ) -> bytearray:
        uid = str(characteristic.uuid).lower()
        if uid == CHAR_INFO_UUID.lower():
            return bytearray(self._info_payload)
        if uid == CHAR_NOTIFY_UUID.lower():
            # Allow reads (CHAR_NOTIFY has Read property)
            return characteristic.value or bytearray()
        return bytearray()

    def _on_write(
        self,
        characteristic: BlessGATTCharacteristic,
        value: Any,
        **kwargs: Any,
    ):
        uid = str(characteristic.uuid).lower()
        if uid != CHAR_WRITE_UUID.lower():
            return

        raw = bytes(value) if value else b""
        if not raw:
            return

        if self._data_cb is None or self._loop is None:
            return

        # Schedule on the asyncio event loop — thread-safe
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                self._data_cb(raw, "peripheral"),
                loop=self._loop,
            )
        )