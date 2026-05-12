import os
import asyncio
from typing import Any, Optional
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .peripheral import PeripheralNode
from .central import CentralNode
from .ui import ChatUI
from .constants import PING_INTERVAL, SCAN_TIMEOUT
from .protocol import Message, Reassembler, MsgType, build_packets

class BLEMessenger:
    """
    Owns the application lifecycle:
        1. negotiate_role() - scan and decide peripheral vs central
        2. start the appropriate BLE node
        3. perform the two-way HANDSHAKE
        4. run three concurrent coroutines:
            _process_incoming   - reassemble packets -> route message
            _ping_loop          - periodic keepalive
            _input_loop         - read user input, send messages
    
    All inbound BLE data (from either node type) feeds into a single
    asyncio.Queue (_raw_queue). _process_incoming drains that queue,
    feeds the Reassembler, and dispatches complete messages.

    Thread-safety note:
        The PeripheralNode's write_handler fires from a bless-internal thread
        and deposits raw bytes via call_soon_threadsafe → _raw_queue.put_nowait.
        The CentralNode's notification_handler fires within the asyncio event
        loop and calls _raw_queue.put_nowait directly.
        Either way _process_incoming sees bytes in the queue.
    """

    def __init__(self, my_name: str, service_uuid: str, tx_char_uuid: str, rx_char_uuid: str):
        self.my_name = my_name
        self.service_uuid = service_uuid
        self.tx_char_uuid = tx_char_uuid
        self.rx_char_uuid = rx_char_uuid
        self.ui = ChatUI(my_name)
        self._peer_name = "Peer"
        self._peer_addr = "unknown"
        self._role = "?"
        self._node: Optional[PeripheralNode | CentralNode] = None
        self._running = True
        self._connected = asyncio.Event()   # set when HANDSHAKE received
        self._raw_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4096)
    
    # --- raw-byte ingestion (called from BLE nodes) -----------------------------

    def _raw_received(self, raw: bytes):
        """Deposits raw BLE bytes into the processing queue. Non-blocking."""
        try:
            self._raw_queue.put_nowait(raw)
        except asyncio.QueueFull:
            pass    # drop if consumer is lagging
    
    # --- message dispatch ------------------------------------------------------

    async def _process_incoming(self):
        """
        Continuously drains _raw_queue, feeds Reassembler, and dispatches
        complete messages. Single coroutine ensures message ordering.
        """
        def on_complete(msg: Message):
            # Schedule the async handler without blocking the reassembler
            asyncio.create_task(self._dispatch(msg))
        
        reassembler = Reassembler(on_complete)

        while self._running:
            try:
                raw = await asyncio.wait_for(self._raw_queue.get(), timeout=1.0)
                reassembler.feed(raw)
            except asyncio.TimeoutError:
                continue
    
    async def _dispatch(self, msg: Message):
        """Route a fully reassembled application message."""
        t = msg.msg_type

        if t == MsgType.HANDSHAKE:
            self._peer_name = msg.payload.decode("utf-8", errors="replace")
            self._connected.set()
        
        elif t == MsgType.CHAT:
            text = msg.payload.decode("utf-8", errors="replace")
            self.ui.add_received(self._peer_name, text, msg.msg_id)
            await self._transmit(MsgType.ACK, bytes([msg.msg_id]))
        
        elif t == MsgType.ACK:
            if msg.payload:
                self.ui.mark_acked(msg.payload[0])
        
        elif t == MsgType.PING:
            await self._transmit(MsgType.PONG, b"", msg_id=msg.msg_id)
        
        elif t == MsgType.PONG:
            self.ui.record_pong_received()
        
        elif t == MsgType.TYPING_ON:
            self.ui.show_typing(True)
        
        elif t == MsgType.TYPING_OFF:
            self.ui.show_typing(False)
        
        elif t == MsgType.GOODBYE:
            self.ui.system(f"{self._peer_name} has left the chat.", "red")
            self._running = False
    
    # --- transmission --------------------------------------------------------------

    async def _transmit(
        self,
        msg_type: MsgType,
        payload: bytes | str,
        msg_id: int = -1
    ) -> int:
        if msg_id < 0:
            msg_id = self.ui.next_msg_id()
        for pkt in build_packets(msg_type, msg_id, payload):
            if self._node:
                await self._node.enqueue(pkt)
        return msg_id
    
    async def _send_chat(self, text: str):
        msg_id = self.ui.next_msg_id()
        self.ui.add_sent(text, msg_id)
        for pkt in build_packets(MsgType.CHAT, msg_id, text):
            if self._node:
                await self._node.enqueue(pkt)
    
    # --- background loops ---------------------------------------------------------

    async def _ping_loop(self):
        """Send a PING every PING_INTERVAL seconds to verify the link is alive."""
        while self._running:
            await asyncio.sleep(PING_INTERVAL)
            if self._running:
                self.ui.record_ping_sent()
                await self._transmit(MsgType.PING, b"")
    
    async def _input_loop(self):
        """
        Read lines from stdin (non-blocking via executor) and route them.
        Sends TYPING_ON when the user starts a new line and TYPING_OFF after.
        """
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                line = await loop.run_in_executor(None, lambda: input("> "))
            except (EOFError, KeyboardInterrupt):
                break
            
            line = line.strip()
            if not line:
                continue

            # Slash commands
            if line.startswith("/"):
                await self._handle_slash(line)
                if not self._running:
                    break
                continue
            
            # Plain chat message
            await self._send_chat(line)
        
        # Graceful goodbye
        self.ui.system("Disconnecting...", "dim")
        try:
            await self._transmit(MsgType.GOODBYE, b"")
            await asyncio.sleep(0.4)
        except Exception:
            pass
        self._running = False
    
    async def _handle_slash(self, cmd: str):
        verb = cmd.split()[0].lower()

        if verb in {"/quit", "/exit", "/q"}:
            self._running = False
        
        elif verb == "/ping":
            self.ui.record_ping_sent()
            await self._transmit(MsgType.PING, b"")
        
        elif verb == "/stats":
            self.ui.print_stats()
        
        elif verb == "/help":
            self.ui.console.print(
                "  [dim]/ping    send keepalive and measure RTT[/dim]\n"
                "  [dim]/stats   show session statistics[/dim]\n"
                "  [dim]/quit    send goodbye and exit[/dim]"
            )
        
        else:
            self.ui.error(f"Unknown command: {verb} (try /help)")
    
    # --- central reconnect logic --------------------------------------------------

    def _on_central_disconnect(self):
        self._connected.clear()
        self.ui.print_disconnected()
        if self._running:
            asyncio.create_task(self._reconnect(self._peer_addr))
    
    async def _reconnect(self, addr: str):
        """Exponential-backoff reconnect loop for the central role."""
        backoff = 2.0
        while self._running:
            self.ui.system(f"Reconnecting to {addr}... (retry in {backoff:.0f}s)", "yellow")
            await asyncio.sleep(backoff)
            if not self._running:
                return
            
            node = CentralNode(self.tx_char_uuid, self.rx_char_uuid, addr, self._raw_received, self._on_central_disconnect)
            if await node.connect():
                self._node = node
                self.ui.system("Reconnected - re-handshaking...", "green")
                await self._transmit(MsgType.HANDSHAKE, self.my_name)
                try:
                    await asyncio.wait_for(self._connected.wait(), timeout=8.0)
                    self.ui.system(f"Back online with {self._peer_name}", "green")
                    return
                except asyncio.TimeoutError:
                    self.ui.error("Handshake timed out after reconnect.")
            backoff = min(backoff * 2, 60.0)
        
        self.ui.error("Could not reconnect. Exiting.")
        self._running = False
    
    # --- role negotiation -----------------------------------------------------------

    async def _negotiate_role(self) -> tuple[str, Optional[str]]:
        """
        Scan for a peer advertising service uuid.

        Returns ("central", peer_address) if a peer is found, or
        ("peripheral", None) if the scan times out with nothing found.
        """
        found: dict[str, Any] = {}

        def on_device(device: BLEDevice, adv: AdvertisementData):
            service_uuids = [u.lower() for u in adv.service_uuids]
            if self.service_uuid.lower() in service_uuids:
                found[device.address] = device
        
        async with BleakScanner(detection_callback=on_device):
            await asyncio.sleep(SCAN_TIMEOUT)
        
        if found:
            # Multiple peers? Take the first discovered.
            peer = next(iter(found.values()))
            self.ui.system(
                f"Peer found at [bold]{peer.address}[/bold] — "
                f"assuming [bold]Central[/bold] role",
                "green"
            )
            return "central", peer.address
        else:
            self.ui.system(
                "No peer found — assuming [bold]Peripheral[/bold] role, advertising…",
                "cyan"
            )
            return "peripheral", None
    
    # --- main entry ------------------------------------------------------------------

    async def run(self):
        self.ui.print_banner()
        loop = asyncio.get_running_loop()

        role, peer_addr = await self._negotiate_role()
        self._role = role

        process_task = asyncio.create_task(
            self._process_incoming(), name="process_incoming"
        )

        # --- peripheral path ------------------------------------
        if role == "peripheral":
            node = PeripheralNode(self.service_uuid, self.tx_char_uuid, self.rx_char_uuid, loop, self._raw_received)
            self._node = node
            await node.start()

            self.ui.system("Waiting for a peer to connect...")

            # Block until HANDSHAKE received (central writes it after connecting)
            await self._connected.wait()

            # Reply with our own name
            await self._transmit(MsgType.HANDSHAKE, self.my_name)
            self._peer_addr = "central"
        
        # --- central path ---------------------------------------
        else:
            self._peer_addr = peer_addr
            node = CentralNode(self.tx_char_uuid, self.rx_char_uuid, peer_addr, self._raw_received, self._on_central_disconnect)
            self._node = node

            self.ui.system(f"Connecting to {peer_addr}...")
            if not await node.connect():
                self.ui.error("Failed to connect.")
                return
            
            # Central always sends the first handshake
            await self._transmit(MsgType.HANDSHAKE, self.my_name)

            # Wait for peripheral's reply
            try:
                await asyncio.wait_for(self._connected.wait(), timeout=12.0)
            except asyncio.TimeoutError:
                self.ui.error("Handshake timed out - peer did not respond.")
                await node.disconnect()
                return
        
        self.ui.print_connected(self._peer_name, role.upper(), self._peer_addr)

        # --- Concurrent runtime tasks --------------------------
        bg = [
            process_task,
            asyncio.create_task(self._ping_loop(), name="ping_loop"),
        ]

        try:
            await self._input_loop()    # blocks until user quits or disconnects
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            self._running = False
            # Give background tasks a moment to notice _running=False, then cancel
            await asyncio.sleep(0.5)
            for t in bg:
                t.cancel()
            await asyncio.gather(*bg, return_exceptions=True)

            if isinstance(self._node, PeripheralNode):
                await self._node.stop()
            elif isinstance(self._node, CentralNode):
                await self._node.disconnect()
        
        self.ui.print_summary()