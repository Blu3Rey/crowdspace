from constants import TX_CHAR, RX_CHAR, CHAT_SERV, INTER_PKT_GAP, DEVICE_NAME
import asyncio
from typing import Any, Callable, Optional
from bless import (
    BlessServer,
    BlessGATTCharacteristic as BlessChar,
    GATTCharacteristicProperties as Props,
    GATTAttributePermissions as Perms,
)

class PeripheralNode:
    """
    Hosts the GATT server
    and advertises service uuid.

    Outbound (peripheral -> central):
        Writes data into TX_CHAR.value and calls server.update_value() to
        trigger a BLE NOTIFY to every subscribed central.
    
    Inbound (central -> peripheral):
        bless fires write_request_func when the central writes to RX_CHAR.
        The callback dispatches the raw bytes to the event loop via
        call_soon_threadsafe (bless may fire from a non-asyncio thread).
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        on_raw: Callable[[bytes], None],
    ):
        self._loop = loop
        self._on_raw = on_raw   # thread-safe, scheduled on loop
        self._server: Optional[BlessServer] = None
        self._tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1024)
        self._running = True
    
    # --- bless callbacks -----------------------------------------------------------

    def _read_handler(self, char: BlessChar, **_) -> bytearray:
        return bytearray()
    
    def _write_handler(self, char: BlessChar, value: Any, **_):
        """Called by the BLE stack - may be a foreign thread; use call_soon_threadsafe."""
        if char.uuid.lower() == RX_CHAR.lower():
            self._loop.call_soon_threadsafe(self._on_raw, bytes(value))
    
    # --- outbound ------------------------------------------------------------------

    async def enqueue(self, pkt: bytes):
        await self._tx_queue.put(pkt)
    
    async def _notify_loop(self):
        """Drain the TX queue, pushing each packet as a BLE notification."""
        while self._running:
            try:
                pkt = await asyncio.wait_for(self._tx_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                char = self._server.get_characteristic(TX_CHAR)
                char.value = bytearray(pkt)
                self._server.update_value(CHAT_SERV, TX_CHAR)
                await asyncio.sleep(INTER_PKT_GAP)
            except Exception:
                pass    # silently skip if not yet connected / char unavailable
    
    # --- lifecycle ----------------------------------------------------------------

    async def start(self):
        self._server = BlessServer(name=DEVICE_NAME)
        self._server.read_request_func = self._read_handler
        self._server.write_request_func = self._write_handler

        await self._server.add_new_service(CHAT_SERV)

        # TX: central subscribes and receives notifications from us
        await self._server.add_new_characteristic(
            CHAT_SERV, TX_CHAR,
            Props.notify,
            None,
            Perms.readable
        )

        # RX: central writes to us; support both write flavours for compatibility
        await self._server.add_new_characteristic(
            CHAT_SERV, RX_CHAR,
            Props.write | Props.write_without_response,
            None,
            Perms.writeable
        )

        await self._server.start()
        asyncio.create_task(self._notify_loop())
    
    async def stop(self):
        self._running = False
        if self._server:
            await self._server.stop()