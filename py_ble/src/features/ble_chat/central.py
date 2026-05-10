import asyncio

from typing import Callable, Optional
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak import BleakClient
from .constants import RX_CHAR, TX_CHAR, INTER_PKT_GAP

class CentralNode:
    """
    Connects to the peripheral's GATT server.

    Inbound (peripheral -> central):
        Subscribes to TX_CHAR notifications; the bleak callback delivers raw
        bytes directly into the asyncio event loop.
    
    Outbound (central -> peripheral):
        Writes raw packet bytes to RX_CHAR (write_without_response for speed).
        A bounded asyncio Queue serialises concurrent senders.
    """

    def __init__(
        self,
        peer_address: str,
        on_raw: Callable[[bytes], None],
        on_disconnect: Callable[[], None]
    ):
        self._addr = peer_address
        self._on_raw = on_raw
        self._on_disconnect = on_disconnect
        self._client: Optional[BleakClient] = None
        self._tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        self._connected = False
        self._running = True
    
    # --- bleak callbacks ----------------------------------------------------------

    def _notification_handler(self, _: BleakGATTCharacteristic, data: bytearray):
        """Called inside the asyncio event loop by bleak."""
        self._on_raw(bytes(data))
    
    def _disconnect_handler(self, _: BleakClient):
        self._connected = False
        self._on_disconnect()
    
    # --- outbound -----------------------------------------------------------------

    async def enqueue(self, pkt: bytes):
        await self._tx_queue.put(pkt)
    
    async def _write_loop(self):
        """Drain the TX queue, writing each packet to RX_CHAR on the peripheral."""
        while self._running:
            try:
                pkt = await asyncio.wait_for(self._tx_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not self._connected:
                continue
            try:
                await self._client.write_gatt_char(RX_CHAR, pkt, response=False)
                await asyncio.sleep(INTER_PKT_GAP)
            except Exception:
                pass
    
    # --- lifecycle ----------------------------------------------------------------

    async def connect(self) -> bool:
        try:
            self._client = BleakClient(
                self._addr,
                timeout=12.0,
                disconnected_callback=self._disconnect_handler
            )
            await self._client.connect()
            await self._client.start_notify(TX_CHAR, self._notification_handler)
            self._connected = True
            asyncio.create_task(self._write_loop())
            return True
        except Exception as exc:
            return False
    
    async def disconnect(self):
        self._running = False
        self._connected = False
        if self._client and self._client.is_connected:
            try:
                await self._client.stop_notify(TX_CHAR)
                await self._client.disconnect()
            except Exception:
                pass