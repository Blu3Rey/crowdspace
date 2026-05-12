
import asyncio
import logging

from __future__ import annotations
from typing import Callable, Optional
from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from ..constants import INTER_PKT_GAP, RX_CHAR_UUID, TX_CHAR_UUID

log = logging.getLogger(__name__)

class CentralTransport:
    """
    Maintains one outbound BLE connection to a peripheral. Writes to RX_CHAR
    (response=True) and delivers TX_CHAR notifications to `on_notify`.
    """

    def __init__(
        self,
        peer_mac:       str,
        on_notify:      Callable[[bytes], None],
        on_disconnect:  Callable[[], None]
    ):
        self._mac = peer_mac
        self._on_notify = on_notify
        self._on_disconnect = on_disconnect
        self._client: Optional[BleakClient] = None
        self._tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        self.connected = False
        self._running = True
    
    def _notification_handler(self, _: BleakGATTCharacteristic, data: bytearray):
        self._on_notify(bytes(data))
    
    def _disconnect_handler(self, _):
        self.connected = False
        self._on_disconnect()
    
    async def _write_loop(self):
        while self._running:
            try:
                pkt = await asyncio.wait_for(self._tx_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not self.connected:
                continue
            try:
                await self._client.write_gatt_char(RX_CHAR_UUID, pkt, response=True)
                await asyncio.sleep(INTER_PKT_GAP)
            except Exception as exc:
                log.debug(f"write error: {exc}")
    
    async def write(self, pkt: bytes) -> None:
        await self._tx_queue.put(pkt)
    
    async def connect(self) -> bool:
        try:
            self._client = BleakClient(
                self._mac,
                timeout=12.0,
                disconnected_callback=self._disconnect_handler
            )
            await self._client.connect()
            await self._client.start_notify(TX_CHAR_UUID, self._notification_handler)
            self.connected = True
            asyncio.create_task(self._write_loop(), name=f"write_loop_{self._mac[-5:]}")
            log.info(f"Connected to {self._mac}")
            return True
        except Exception as exc:
            log.warning(f"Connect failed {self._mac}: {exc}")
            return False
    
    async def disconnect(self) -> None:
        self._running = False
        self.connected = False
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(TX_CHAR_UUID)
                await self._client.disconnect()
            except Exception:
                pass