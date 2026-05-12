"""
connection/manager.py — ConnectionManager: owns all active BLE connections.
 
This is the key architectural component that enables group chat, mesh, and
ranging.  Instead of one hard-coded node, the manager maintains a dict of
Peer objects and routes outbound packets to the right ones.
 
Supports:
    • send_to(mac, packets)   — unicast to one peer
    • broadcast(packets)      — send to all connected peers
    • get_peers()             — list all connected peers
    • add_peripheral / add_central — register a new connection
 
The manager also owns the PeripheralTransport so that incoming central
connections are captured without any external coordination.
"""

import asyncio
import logging

from __future__ import annotations
from typing import Callable, Optional

from ..constants import (
    DEVICE_NAME, GATT_REGISTER_DELAY, INTER_PKT_GAP,
    RX_CHAR_UUID, SERVICE_UUID, TX_CHAR_UUID
)
from ..events import EventBus, PEER_CONNECTED, PEER_DISCONNECTED, PEER_RSSI
from ..protocol import Message
from .peer import Peer, PeerRole, PeerState

log = logging.getLogger(__name__)

# --- Transport imports (deferred to avoid circular imports) ---------------

def _make_peripheral(loop, write_cb, name=DEVICE_NAME):
    from ..transport.peripheral import PeripheralTransport
    return PeripheralTransport(loop, write_cb, name)

def _make_central(mac, notify_cb, disconnect_cb):
    from ..transport.central import CentralTransport
    return CentralTransport(mac, notify_cb, disconnect_cb)


# --- ConnectionManager ----------------------------------------------------

class ConnectionManager:
    """
    Central registry of all connected BLE peers.

    The single PeripheralTransport (bless GATT server) is started once and
    accepts any central that connects. For each outbound connection the
    manager creates a CentralTransport (bleak client).

    Thread safety: all mutation happens on the asyncio event loop.
    BLE callbacks use call_soon_threadsafe where needs (PeripheralTransport).
    """

    def __init__(
        self,
        bus: EventBus,
        on_msg: Callable[[Peer, Message], None],
        loop: asyncio.AbstractEventLoop,
    ):
        self._bus = bus
        self._on_msg = on_msg
        self._loop = loop

        self._peers: dict[str, Peer] = {}           # mac -> Peer
        self._transports: dict[str, object] = {}    # mac -> CentralTransport
        self._peripheral: Optional[object] = None   # PeripheralTransport
        self._running = True
    
    # --- Peripheral (incoming connections) ---------------------------------

    async def start_peripheral(self) -> None:
        """Start the GATT server and accept incoming central connections."""
        self._peripheral = _make_peripheral(
            self._loop,
            self._on_peripheral_write,
        )
        await self._peripheral.start()
        await asyncio.sleep(GATT_REGISTER_DELAY)
        log.info("Peripheral started, advertising...")
    
    def _on_peripheral_write(self, raw: bytes, mac: str) -> None:
        """
        Called from the peripheral transport when a central writes to RX_CHAR.
        Dispatches to the correct Peer or creates one on first contact.
        """
        peer = self._peers.get(mac.upper())
        if peer is None:
            peer = self._register_peer(mac, PeerRole.PERIPHERAL)
        peer.feed(raw)
    
    async def notify_peer(self, mac: str, pkt: bytes) -> None:
        """Send one packet to a central that is connected to our peripheral."""
        if self._peripheral:
            await self._peripheral.notify(pkt)
    
    # --- Central (outbound connections) -------------------------------------

    async def connect_central(self, mac: str) -> bool:
        """Open a new outbound BLE connection to `mac`."""
        if mac.upper() in self._peers:
            log.warning(f"Already connected to {mac}")
            return True
        
        def on_notify(raw: bytes):
            peer = self._peers.get(mac.upper())
            if peer:
                peer.feed(raw)
        
        def on_disconnect():
            self._handle_disconnect(mac.upper())
        
        transport = _make_central(mac, on_notify, on_disconnect)
        ok = await transport.connect()
        if not ok:
            return False
        
        peer = self._register_peer(mac, PeerRole.CENTRAL)
        self._transports[mac.upper()] = transport
        return True
    
    async def write_peer(self, mac: str, pkt: bytes) -> None:
        """Write one packet to a peripheral we are connected to as central."""
        transport = self._transports.get(mac.upper())
        if transport and transport.connected:
            try:
                await transport.write(pkt)
                await asyncio.sleep(INTER_PKT_GAP)
            except Exception as exc:
                log.warning(f"write_peer {mac}: {exc}")
    
    # --- Unified send API ----------------------------------------------------

    async def send_to(self, mac: str, packets: list[bytes]) -> None:
        """Unicast: send packet list to one named peer."""
        peer = self._peers.get(mac.upper())
        if peer is None:
            return
        for pkt in packets:
            if peer.role == PeerRole.CENTRAL:
                await self.write_peer(mac, pkt)
            else:
                await self.notify_peer(mac, pkt)
            peer.record_tx(len(pkt))
    
    async def broadcast(self, packets: list[bytes]) -> None:
        """Broadcast: send packet list to every connected peer."""
        for mac in list(self._peers.keys()):
            await self.send_to(mac, packets)
    
    # --- Peer registry -------------------------------------------------------

    def _register_peer(self, mac: str, role: PeerRole) -> Peer:
        peer = Peer(mac, role, self._on_msg)
        peer.state = PeerState.HANDSHAKING
        self._peers[mac.upper()] = peer
        log.info(f"Registered {peer}")
        self._bus.emit_nowait(PEER_CONNECTED, peer=peer)
        return peer
    
    def _handle_disconnect(self, mac: str) -> None:
        peer = self._peers.pop(mac.upper(), None)
        self._transports.pop(mac.upper(), None)
        if peer:
            peer.state = PeerState.DISCONNECTED
            log.info(f"Disconnected: {peer}")
            self._bus.emit_nowait(PEER_DISCONNECTED, peer=peer, reason="link lost")
    
    def record_rssi(self, mac: str, rssi: int) -> None:
        peer = self._peers.get(mac.upper())
        if peer:
            peer.record_rssi(rssi)
            self._bus.emit_nowait(PEER_RSSI, peer=peer, rssi=rssi)
    

    # --- Accessors ------------------------------------------------------------

    def get_peer(self, mac: str) -> Optional[Peer]:
        return self._peers.get(mac.upper())
    
    def get_peers(self) -> list[Peer]:
        return list(self._peers.values())
    
    def connected_count(self) -> int:
        return len(self._peers)
    

    # --- Shutdown --------------------------------------------------------------

    async def shutdown(self) -> None:
        self._running = False
        for mac in list(self._peers.keys()):
            transport = self._transports.get(mac)
            if transport:
                try:
                    await transport.disconnect()
                except Exception:
                    pass
        if self._peripheral:
            await self._peripheral.stop()