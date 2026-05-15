"""
features/locating.py — RSSI-based device proximity and multi-hop locating.

How it works
------------
1. Any node can call :meth:`ping` / :meth:`locate` to send a LOC_REQ.
2. Every node that hears the LOC_REQ replies with a LOC_RESP containing:
   - The RSSI at which *they* received the request.
   - The number of hops from the requesting node.
3. The requesting node aggregates responses into a :class:`LocationReport`
   sorted by descending RSSI (stronger signal = closer device).

Wire formats
------------
LOC_REQ payload::

    [target_id: 16B]   ← BROADCAST_ADDR to ping all, or a specific node_id

LOC_RESP payload::

    [requester_id: 16B][rssi: 1B signed int8][hops: 1B uint8]

LOC_REPORT (aggregated, emitted as a local event — not sent on the wire)::

    Synthesised by :class:`DeviceLocator` from collected LOC_RESPs.

RSSI-to-distance mapping
-------------------------
Use the log-distance path-loss model::

    d = 10 ^ ((TxPower - RSSI) / (10 × n))

where *n* (path-loss exponent) is typically 2.0 in open space and 3.0–4.0
indoors.  The method :meth:`rssi_to_distance_m` exposes this conversion.

Usage::

    locator = DeviceLocator()
    node.register_feature(locator)

    report = await locator.locate(target_id, timeout=5.0)
    for entry in report:
        print(f"  via {entry['responder_name']:15s}  RSSI={entry['rssi']} dBm  hops={entry['hops']}")
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from ..core.packet import Packet
from ..core.protocol import BROADCAST_ADDR, MsgType
from ..utils.logger import log
from .base import Feature

_REQ_FMT   = "!16s"
_RESP_FMT  = "!16sbB"   # requester_id(16) + rssi(int8) + hops(uint8)
_RESP_SIZE = struct.calcsize(_RESP_FMT)

# Callback: (report: list[LocationEntry]) → None/Awaitable
LocCallback = Callable[[List[dict]], Awaitable[None]]

# RSSI path-loss model defaults
_TX_POWER_DEFAULT = -59   # dBm at 1 m (typical BLE)
_PATH_LOSS_N      = 2.0   # open-space exponent


@dataclass
class LocationEntry:
    responder_id:   bytes
    responder_name: str
    rssi:           int
    hops:           int
    timestamp:      float = field(default_factory=time.monotonic)

    @property
    def estimated_distance_m(self) -> float:
        """Rough distance estimate using log-distance path-loss model."""
        return rssi_to_distance_m(self.rssi)

    def to_dict(self) -> dict:
        return {
            "responder_id":   self.responder_id.hex(),
            "responder_name": self.responder_name,
            "rssi":           self.rssi,
            "hops":           self.hops,
            "distance_m":     round(self.estimated_distance_m, 2),
            "age_s":          round(time.monotonic() - self.timestamp, 1),
        }


def rssi_to_distance_m(
    rssi: int,
    tx_power: int = _TX_POWER_DEFAULT,
    n: float = _PATH_LOSS_N,
) -> float:
    """Convert RSSI (dBm) to an estimated distance in metres."""
    if rssi >= tx_power:
        return 0.1
    return 10 ** ((tx_power - rssi) / (10 * n))


class DeviceLocator(Feature):
    """RSSI-based device proximity estimation.

    Parameters
    ----------
    auto_respond : bool
        If True (default) this node automatically replies to all LOC_REQ
        packets with its measured RSSI.
    tx_power : int
        Assumed BLE transmit power at 1 m (dBm).  Used for distance estimates.
    """

    handled_types = frozenset({MsgType.LOC_REQ, MsgType.LOC_RESP})
    name = "device-locator"

    def __init__(self, auto_respond: bool = True, tx_power: int = _TX_POWER_DEFAULT) -> None:
        super().__init__()
        self._auto_respond = auto_respond
        self._tx_power     = tx_power

        # requester_id → asyncio.Event  (set when ≥1 response arrives)
        self._pending:   Dict[bytes, asyncio.Event]          = {}
        # requester_id → list[LocationEntry]
        self._responses: Dict[bytes, List[LocationEntry]]    = defaultdict(list)
        # Persistent last-known location table: node_id → LocationEntry
        self._known:     Dict[bytes, LocationEntry]          = {}

        self._callbacks: List[LocCallback] = []

    # ── Public API ────────────────────────────────────────────────────────────

    async def locate(
        self,
        target_id: Optional[bytes] = None,
        timeout:   float = 5.0,
    ) -> List[dict]:
        """Send a LOC_REQ and collect responses for *timeout* seconds.

        Parameters
        ----------
        target_id : bytes, optional
            16-byte node ID to locate.  Pass ``None`` (or omit) to ping *all*
            nodes (BROADCAST_ADDR).
        timeout : float
            How long to collect LOC_RESP replies.

        Returns
        -------
        list[dict]
            Sorted by RSSI descending (strongest = closest).
        """
        req_id = target_id or BROADCAST_ADDR
        event  = asyncio.Event()
        self._pending[req_id]   = event
        self._responses[req_id] = []

        payload = struct.pack(_REQ_FMT, req_id)
        await self.node.send(MsgType.LOC_REQ, payload, dst_id=BROADCAST_ADDR)
        log.debug("[Locator] LOC_REQ sent for %s.", req_id.hex()[:8])

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._pending.pop(req_id, None)

        entries  = self._responses.pop(req_id, [])
        sorted_e = sorted(entries, key=lambda e: e.rssi, reverse=True)

        # Persist into known table
        for e in sorted_e:
            self._known[e.responder_id] = e

        report = [e.to_dict() for e in sorted_e]
        for cb in self._callbacks:
            try:
                result = cb(report)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                log.warning("[Locator] Callback error: %s", exc)

        return report

    def last_known(self, node_id: bytes) -> Optional[dict]:
        """Return the most recent location data for *node_id*, or None."""
        entry = self._known.get(node_id)
        return entry.to_dict() if entry else None

    def all_known(self) -> List[dict]:
        """Return all cached location entries sorted by RSSI descending."""
        return sorted(
            [e.to_dict() for e in self._known.values()],
            key=lambda d: d["rssi"], reverse=True,
        )

    def on_report(self, fn: LocCallback) -> LocCallback:
        """Decorator that fires *fn* whenever a locate() response arrives."""
        self._callbacks.append(fn)
        return fn

    def estimate_distance(self, node_id: bytes) -> Optional[float]:
        """Rough distance in metres to *node_id* from last cached RSSI."""
        entry = self._known.get(node_id)
        return rssi_to_distance_m(entry.rssi, self._tx_power) if entry else None

    # ── Feature interface ─────────────────────────────────────────────────────

    async def handle(self, packet: Packet) -> None:
        if packet.msg_type == MsgType.LOC_REQ:
            await self._handle_req(packet)
        elif packet.msg_type == MsgType.LOC_RESP:
            await self._handle_resp(packet)

    async def _handle_req(self, packet: Packet) -> None:
        if not self._auto_respond:
            return
        if len(packet.payload) < 16:
            return
        target_id = packet.payload[:16]

        # Only respond if the request targets us or is broadcast
        if target_id not in (self.node.node_id, BROADCAST_ADDR):
            return

        rssi = packet.rssi if packet.rssi is not None else -100
        resp_payload = struct.pack(_RESP_FMT, packet.src_id, rssi, 1) # Removed '& 0xFF' appended to the 3rd argument
        await self.node.send(
            MsgType.LOC_RESP, resp_payload, dst_id=packet.src_id
        )
        log.debug("[Locator] Responded to LOC_REQ from %s (RSSI=%d).",
                  packet.src_id.hex()[:8], rssi)

    async def _handle_resp(self, packet: Packet) -> None:
        if len(packet.payload) < _RESP_SIZE:
            return
        requester_id, rssi, hops = struct.unpack(_RESP_FMT, packet.payload[:_RESP_SIZE])
        # rssi = rssi_byte if rssi_byte < 128 else rssi_byte - 256  # uint8 → int8

        # Find responder name from neighbour table
        nb = self.node.neighbors.get(packet.src_id)
        responder_name = nb.name if nb else packet.src_id.hex()[:8]

        entry = LocationEntry(
            responder_id=packet.src_id,
            responder_name=responder_name,
            rssi=rssi,
            hops=hops,
        )
        self._known[packet.src_id] = entry
        print("KEYS: ", (requester_id, packet.src_id, BROADCAST_ADDR))
        print("PENDING: ", self._pending)
        # Deliver to any pending locate() call
        for key in (requester_id, packet.src_id, BROADCAST_ADDR):
            if key in self._pending:
                self._responses[key].append(entry)
                self._pending[key].set()

        log.debug("[Locator] LOC_RESP from %s RSSI=%d hops=%d.",
                  packet.src_id.hex()[:8], rssi, hops)