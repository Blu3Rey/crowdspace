import asyncio
import logging

from typing import Any, Callable, Optional
from bless import (
    BlessServer,
    BlessGATTCharacteristic as BlessChar,
    GATTCharacteristicProperties as Props,
    GATTAttributePermissions as Perms,
)

from ..constants import DEVICE_NAME, INTER_PKT_GAP, RX_CHAR_UUID, SERVICE_UUID, TX_CHAR_UUID

log = logging.getLogger(__name__)

class PeripheralTransport:
    """
    Hosts the GATT server. Accepts writes on RX_CHAR and sends notifications
    on TX_CHAR. Calls `on_write(raw_bytes, mac)` for every incoming packet.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_write: Callable[[bytes, str], None],
        name: str = DEVICE_NAME
    ):
        self._loop = loop
        self._on_write = on_write
        self._name = name
        self._server: Optional[BlessServer] = None
        self._tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        self._running = True
    
    def _read_handler(self, char: BlessChar, **_) -> bytearray:
        return bytearray()
    
    def _write_handler(self, char: BlessChar, value: Any, **_):
        if char.uuid.lower() == RX_CHAR_UUID.lower():
            self._loop.call_soon_threadsafe(
                self._on_write, bytes(value), "central" # MAC unknown here
            )
    
    async def _notify_loop(self):
        while self._running:
            try:
                pkt = await asyncio.wait_for(self._tx_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                char = self._server.get_characteristic(TX_CHAR_UUID)
                char.value = bytearray(pkt)
                self._server.update_value(SERVICE_UUID, TX_CHAR_UUID)
                await asyncio.sleep(INTER_PKT_GAP)
            except Exception as exc:
                log.debug(f"notify error: {exc}")
    
    async def notify(self, pkt: bytes) -> None:
        await self._tx_queue.put(pkt)
    
    async def start(self) -> None:
        self._server = BlessServer(name=self._name)
        self._server.read_request_func = self._read_handler
        self._server.write_request_func = self._write_handler

        await self._server.add_new_service(SERVICE_UUID)
        await self._server.add_new_characteristic(
            SERVICE_UUID, TX_CHAR_UUID, Props.notify, None, Perms.readable
        )
        await self._server.add_new_characteristic(
            SERVICE_UUID, RX_CHAR_UUID, Props.write, None, Perms.writeable
        )
        await self._server.start()
        asyncio.create_task(self._notify_loop(), name="notify_loop")
        log.info(f"Peripheral advertising as '{self._name}'")
    
    async def stop(self) -> None:
        self._running = False
        if self._server:
            await self._server.stop()