"""
transport/manager.py — Unified transport layer.

The :class:`TransportManager` owns both the :class:`BLEPeripheral` (server)
and the :class:`BLECentral` (scanner / client) and exposes a single
:meth:`send` method to the rest of the stack.

Send strategy
-------------
To reach a peer we try two channels in parallel:

1. **As central** — write to the peer's RX characteristic (direct unicast).
2. **As peripheral** — notify all subscribed centrals (the right node picks it
   up based on the *dst_id* field in the packet header).

For *broadcast* packets both channels are used unconditionally.
For *unicast* packets we first try the known direct client; if the peer is not
a current central connection we fall back to peripheral notification (letting
the TTL-flooded mesh deliver it).
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from ..core.neighbor import NeighborTable
from ..utils.logger import log
from .central import BLECentral
from .peripheral import BLEPeripheral

RxHandler    = Callable[[bytes, str], Awaitable[None]]
EventHandler = Callable[["Neighbor"], Awaitable[None]]  # type: ignore[name-defined]


class TransportManager:
    """Co-ordinates the peripheral (bless) and central (bleak) transport roles.

    Parameters
    ----------
    node_name : str
        Advertised BLE device name.
    node_id : bytes
        16-byte mesh identifier.
    neighbors : NeighborTable
        Shared neighbour registry (mutated by both peripheral and central).
    scan_duration : float
        Duration of each BLE scan burst in seconds.
    scan_interval : float
        Seconds between scan bursts.
    max_connections : int
        Maximum simultaneous central connections.
    connection_timeout : float
        Seconds to wait for a connection.
    enable_peripheral : bool
        Set False on platforms that do not support peripheral/server mode.
    """

    def __init__(
        self,
        node_name:          str,
        node_id:            bytes,
        neighbors:          NeighborTable,
        scan_duration:      float = 4.0,
        scan_interval:      float = 8.0,
        max_connections:    int   = 7,
        connection_timeout: float = 10.0,
        enable_peripheral:  bool  = True,
        passive_scan:       bool  = False,
    ) -> None:
        self._neighbors = neighbors
        self._scan_duration  = scan_duration
        self._scan_interval  = scan_interval
        self._rx_handler: Optional[RxHandler] = None

        # ── Peripheral ────────────────────────────────────────────────────────
        self.peripheral: Optional[BLEPeripheral] = None
        if enable_peripheral:
            try:
                self.peripheral = BLEPeripheral(node_name, node_id)
            except RuntimeError as exc:
                log.warning("[Transport] Peripheral unavailable: %s", exc)

        # ── Central ───────────────────────────────────────────────────────────
        self.central = BLECentral(
            node_id=node_id,
            neighbors=neighbors,
            max_connections=max_connections,
            connection_timeout=connection_timeout,
            passive_scan=passive_scan,
        )

        self._scan_task: Optional[asyncio.Task] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._rx_handler is None:
            raise RuntimeError("Call set_rx_handler() before start().")

        if self.peripheral:
            self.peripheral.set_rx_handler(self._rx_handler)
            try:
                await self.peripheral.start()
            except Exception as exc:
                log.error("[Transport] Peripheral start failed: %s", exc)
                self.peripheral = None

        self.central.set_rx_handler(self._rx_handler)
        self._scan_task = asyncio.create_task(
            self.central.start_scanning(self._scan_duration, self._scan_interval),
            name="ble-scan",
        )
        log.info("[Transport] Started (peripheral=%s).", self.peripheral is not None)

    async def stop(self) -> None:
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        await self.central.stop()
        if self.peripheral:
            await self.peripheral.stop()
        log.info("[Transport] Stopped.")

    # ── Handlers ──────────────────────────────────────────────────────────────

    def set_rx_handler(self, handler: RxHandler) -> None:
        self._rx_handler = handler

    def set_connected_handler(self, handler: EventHandler) -> None:
        self.central.set_connected_handler(handler)

    def set_disconnected_handler(self, handler: EventHandler) -> None:
        self.central.set_disconnected_handler(handler)

    # ── Send ──────────────────────────────────────────────────────────────────

    async def send(self, data: bytes, dst_id: Optional[bytes] = None) -> bool:
        """Transmit *data* bytes over available BLE channels.

        Channel strategy
        ----------------
        * **Unicast** (dst_id is a specific node): write via the central channel
          if a direct connection exists.  Only fall through to peripheral
          notification when the direct write fails or no connection exists.
          This avoids the previous behaviour of always double-sending to peers
          reachable via both channels.
        * **Broadcast** (dst_id is None): write to all central connections AND
          peripheral-notify.  These two channels cover disjoint peer sets
          (peers we connected *to* vs. peers that connected *to us*), so both
          are needed.  The dedup cache prevents double-processing on nodes that
          receive via both channels.

        Returns True if at least one send succeeded.
        """
        success = False

        # ── Central channel ───────────────────────────────────────────────────
        central_hit = False
        if dst_id is not None:
            neighbor = self._neighbors.get(dst_id)
            if neighbor and neighbor.is_connected and neighbor.address:
                ok = await self.central.send_to(neighbor.address, data)
                success = success or ok
                central_hit = ok   # reached peer directly — skip peripheral notify
        else:
            # Broadcast: write to every peer we are connected to as central
            n = await self.central.send_to_all(data)
            success = success or (n > 0)

        # ── Peripheral channel ────────────────────────────────────────────────
        # For unicast: only notify if the central write didn't reach the peer —
        #   this avoids double-delivery and wasted airtime.
        # For broadcast: always notify, covering peers connected to us as peripheral.
        if self.peripheral and not central_hit:
            ok = await self.peripheral.notify_all(data)
            success = success or ok

        return success

    async def send_to_address(self, address: str, data: bytes) -> bool:
        """Send directly to a specific BLE address (bypasses routing)."""
        return await self.central.send_to(address, data)

    @property
    def connection_count(self) -> int:
        return self.central.connection_count
