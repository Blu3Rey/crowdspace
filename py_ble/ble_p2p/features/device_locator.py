"""
features/device_locator.py — RSSI-based device locating and presence beacons.

Two complementary mechanisms
-----------------------------
1. Passive RSSI tracking: the Node feeds advertisement RSSI into the locator
   on every scan via record_advertisement_rssi().  No messages needed.

2. Ping/Pong RTT (active): call ping() to send a LOCATOR PING message and
   measure round-trip time.  Combine with RSSI for better estimates.

3. Presence beacons: call beacon() to broadcast this node's current location
   descriptor (text label, e.g. "Kitchen") to all reachable peers.

Wire payload (after feature_id byte) — JSON:
{
  "op"     : "ping" | "pong" | "beacon",
  "mid"    : "<request id>",          # ping/pong correlation
  "label"  : "<location text>",       # beacon
  "ts"     : <unix_ms>
}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

from ..constants import FeatureID, MsgType, MsgFlags, BROADCAST_ID
from .base import Feature

log = logging.getLogger(__name__)

PresenceHandler = Callable[[str, str, str, float], Awaitable[None]]
# (from_id_hex, from_name, label, ts)


@dataclass
class DeviceLocation:
    """Rolling proximity / location state for one peer."""
    device_id_hex : str
    name          : str
    rssi_samples  : List[int] = field(default_factory=list)
    label         : str       = ""          # last reported location label
    label_ts      : float     = 0.0
    last_ping_rtt : Optional[float] = None  # seconds

    @property
    def avg_rssi(self) -> Optional[float]:
        if not self.rssi_samples:
            return None
        return sum(self.rssi_samples) / len(self.rssi_samples)

    @property
    def distance_m(self) -> Optional[float]:
        """Log-distance path-loss estimate (TX power ≈ −59 dBm at 1 m)."""
        r = self.avg_rssi
        if r is None:
            return None
        tx = -59
        ratio = r / tx
        if ratio < 1.0:
            return ratio ** 10
        return 0.89976 * (ratio ** 7.7095) + 0.111

    @property
    def proximity(self) -> str:
        """Human-readable proximity bucket."""
        d = self.distance_m
        if d is None:
            return "unknown"
        if d < 0.5:
            return "immediate"
        if d < 3.0:
            return "near"
        if d < 10.0:
            return "medium"
        return "far"

    def add_rssi(self, rssi: int, window: int = 10):
        self.rssi_samples.append(rssi)
        if len(self.rssi_samples) > window:
            self.rssi_samples.pop(0)

    def summary(self) -> dict:
        return {
            "id"        : self.device_id_hex,
            "name"      : self.name,
            "avg_rssi"  : round(self.avg_rssi, 1) if self.avg_rssi is not None else None,
            "distance_m": round(self.distance_m, 2) if self.distance_m is not None else None,
            "proximity" : self.proximity,
            "label"     : self.label or None,
            "rtt_ms"    : round(self.last_ping_rtt * 1000, 1)
                          if self.last_ping_rtt is not None else None,
        }


class DeviceLocatorFeature(Feature):
    """
    Tracks peer proximity using BLE RSSI and optional active pinging.

    The Node should call feature.record_advertisement_rssi() inside its
    scan discovery callback to keep the passive tracking up to date.
    """

    feature_id = FeatureID.DEVICE_LOCATOR

    def __init__(self, node):
        super().__init__(node)
        self._locations   : Dict[str, DeviceLocation] = {}
        self._ping_waiters: Dict[str, asyncio.Future] = {}
        self._pres_handlers: List[PresenceHandler]    = []

    # ── Public API ────────────────────────────────────────────

    def on_presence(self, callback: PresenceHandler) -> None:
        """Register callback for BEACON messages from peers."""
        self._pres_handlers.append(callback)

    def record_advertisement_rssi(
        self, device_id_hex: str, name: str, rssi: int
    ):
        """
        Feed one RSSI observation into the rolling window for a peer.
        Call this from the node's BLE scan callback for passive tracking.
        """
        loc = self._locations.get(device_id_hex)
        if loc is None:
            loc = DeviceLocation(device_id_hex=device_id_hex, name=name)
            self._locations[device_id_hex] = loc
        loc.name = name
        loc.add_rssi(rssi)

    def get_all(self) -> List[dict]:
        """Return proximity summary for every tracked peer."""
        return [loc.summary() for loc in self._locations.values()]

    def get_peer(self, device_id_hex: str) -> Optional[dict]:
        loc = self._locations.get(device_id_hex)
        return loc.summary() if loc else None

    async def ping(self, dst_id: bytes, timeout: float = 5.0) -> Optional[float]:
        """
        Send an active PING to *dst_id* and return RTT in seconds.
        Returns None on timeout or failure.
        """
        mid  = uuid.uuid4().hex[:8]
        body = json.dumps(
            {"op": "ping", "mid": mid, "ts": int(time.time() * 1000)},
            separators=(",", ":"),
        ).encode()
        payload = self.encode_payload(body)

        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        self._ping_waiters[mid] = fut
        sent_at = time.monotonic()

        await self.node.send_message(
            msg_type = MsgType.FEATURE,
            payload  = payload,
            dst_id   = dst_id,
            flags    = int(MsgFlags.PRIORITY),
        )

        try:
            await asyncio.wait_for(fut, timeout=timeout)
            rtt = time.monotonic() - sent_at
            hex_id = dst_id.hex()
            if hex_id in self._locations:
                self._locations[hex_id].last_ping_rtt = rtt
            return rtt
        except asyncio.TimeoutError:
            self._ping_waiters.pop(mid, None)
            return None

    async def beacon(self, label: str) -> bool:
        """
        Broadcast this node's current location *label* to all peers.
        e.g. label = "Office / 3rd Floor"
        """
        body = json.dumps(
            {"op": "beacon", "label": label, "ts": int(time.time() * 1000)},
            separators=(",", ":"),
        ).encode()
        payload = self.encode_payload(body)
        return await self.node.send_message(
            msg_type = MsgType.FEATURE,
            payload  = payload,
            dst_id   = BROADCAST_ID,
            flags    = int(MsgFlags.BROADCAST),
        )

    # ── Feature interface ─────────────────────────────────────

    async def handle_message(self, body: bytes, src_id: bytes, src_name: str) -> None:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return

        op = data.get("op", "")

        if op == "ping":
            # Send PONG back
            mid   = data.get("mid", "")
            pong  = json.dumps(
                {"op": "pong", "mid": mid, "ts": int(time.time() * 1000)},
                separators=(",", ":"),
            ).encode()
            await self.node.send_message(
                msg_type = MsgType.FEATURE,
                payload  = self.encode_payload(pong),
                dst_id   = src_id,
                flags    = int(MsgFlags.PRIORITY),
            )

        elif op == "pong":
            mid = data.get("mid", "")
            fut = self._ping_waiters.pop(mid, None)
            if fut and not fut.done():
                fut.set_result(True)

        elif op == "beacon":
            label = data.get("label", "")
            ts    = data.get("ts", int(time.time() * 1000))
            hex_id = src_id.hex()

            loc = self._locations.get(hex_id)
            if loc is None:
                loc = DeviceLocation(device_id_hex=hex_id, name=src_name)
                self._locations[hex_id] = loc
            loc.label    = label
            loc.label_ts = ts / 1000.0

            log.info("Beacon from %s: %r", src_name, label)
            for cb in self._pres_handlers:
                try:
                    await cb(hex_id, src_name, label, ts / 1000.0)
                except Exception as exc:
                    log.exception("Presence handler raised: %s", exc)