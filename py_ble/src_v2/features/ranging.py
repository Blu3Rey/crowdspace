"""
features/ranging.py — RSSI-based distance estimation.

Protocol:
    RANGE_PING  → peer replies RANGE_PONG with its last measured RSSI
    RANGE_REPORT → unsolicited distance report from peer

    Packet payload for RANGE_PONG:
        Byte 0:    rssi (signed int8, the sender's measured RSSI of the ping)
        Bytes 1-4: tx_power (signed int8 padded to 4 bytes)

Distance formula (log-distance path loss model):
    d = 10 ^ ((tx_power - rssi) / (10 * n))
    where n is RSSI_N_FACTOR (path-loss exponent).

Kalman filter:
    Applied per peer to reduce RSSI noise.  Process noise Q and measurement
    noise R are tunable in constants.py.

BLE 5.1 Direction Finding note:
    Hardware that supports AoA/AoD (angle-of-arrival / angle-of-departure)
    exposes CTE (Constant Tone Extension) via the HCI LE_Set_Connectionless_IQ_Sampling
    command.  bleak does not yet expose this directly; you would need to use
    a raw HCI socket (Python `hci` or `aioblescan`).  The ranging feature is
    designed so that a direction_angle field can be added to RANGE_PONG without
    changing any other layer.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from typing import TYPE_CHECKING

from ..constants import (
    KALMAN_Q, KALMAN_R, RANGING_INTERVAL, RSSI_N_FACTOR, TX_POWER_DEFAULT
)
from ..events import RANGING_UPDATE, EventBus
from ..protocol import FeatureID, Message
from . import FeatureBase

if TYPE_CHECKING:
    from ..connection.manager import ConnectionManager
    from ..connection.peer import Peer

log = logging.getLogger(__name__)


class RangingMsg:
    PING   = 0x01
    PONG   = 0x02
    REPORT = 0x03


class KalmanRSSI:
    """
    Scalar Kalman filter for RSSI smoothing.

    State: estimated RSSI (dBm)
    Transitions: stationary (no control input)
    Measurement: raw RSSI from BLE stack
    """

    def __init__(self, q: float = KALMAN_Q, r: float = KALMAN_R):
        self._q  = q    # process noise covariance
        self._r  = r    # measurement noise covariance
        self._x  = None # estimated state
        self._p  = 1.0  # estimated error covariance

    def update(self, measurement: float) -> float:
        if self._x is None:
            self._x = measurement
            return self._x

        # Predict
        p_pred = self._p + self._q

        # Update (Kalman gain)
        k       = p_pred / (p_pred + self._r)
        self._x = self._x + k * (measurement - self._x)
        self._p = (1 - k) * p_pred

        return self._x


def rssi_to_distance(rssi: float, tx_power: int = TX_POWER_DEFAULT, n: float = RSSI_N_FACTOR) -> float:
    """
    Estimate distance in metres from RSSI using the log-distance path loss model.

        d = 10 ^ ((tx_power - rssi) / (10 * n))

    tx_power: RSSI measured at exactly 1 m from the antenna (calibrate per device).
    n:        path-loss exponent (2.0 = free space, 2.5-4.0 = indoor).
    """
    if rssi == 0:
        return -1.0
    return 10.0 ** ((tx_power - rssi) / (10.0 * n))


class RangingFeature(FeatureBase):
    """
    Continuously estimates distance to each connected peer.

    For each peer:
        1.  Every RANGING_INTERVAL seconds, send RANGE_PING.
        2.  Peer replies RANGE_PONG with its measured RSSI of our ping.
        3.  Apply Kalman filter to smooth the measurement.
        4.  Compute distance and emit ranging.update event.

    The ranging.update event (peer, rssi, distance_m) can be consumed by:
        • The UI (display live distance)
        • A MeshFeature (prefer closer peers as next hops)
        • A ProximityFeature (trigger events at distance thresholds)
    """

    FEATURE_ID = FeatureID.RANGING

    def __init__(self, bus: EventBus, conn: "ConnectionManager",
                 tx_power: int = TX_POWER_DEFAULT):
        super().__init__(bus, conn)
        self._tx_power  = tx_power
        self._filters:  dict[str, KalmanRSSI] = {}   # mac → filter
        self._distances: dict[str, float]     = {}   # mac → metres

    async def start(self) -> None:
        await super().start()
        asyncio.create_task(self._ranging_loop(), name="ranging_loop")

    async def _ranging_loop(self) -> None:
        """Periodically ping every connected peer for RSSI."""
        while self._running:
            await asyncio.sleep(RANGING_INTERVAL)
            for peer in self.conn.get_peers():
                try:
                    await self._send(peer, RangingMsg.PING,
                                     struct.pack("b", self._tx_power))
                except Exception as exc:
                    log.debug(f"ranging ping error: {exc}")

    async def on_message(self, peer: "Peer", msg: Message) -> None:
        if msg.msg_type == RangingMsg.PING:
            # Reply with the RSSI we measured for the incoming ping packet.
            # peer.rssi is the last RSSI recorded by the connection layer.
            measured = peer.rssi or -70
            payload  = struct.pack("bb", measured, self._tx_power)
            await self._send(peer, RangingMsg.PONG, payload)

        elif msg.msg_type == RangingMsg.PONG:
            if len(msg.payload) >= 1:
                raw_rssi = struct.unpack_from("b", msg.payload, 0)[0]
                self._process_rssi(peer, raw_rssi)

        elif msg.msg_type == RangingMsg.REPORT:
            if len(msg.payload) >= 5:
                raw_rssi, dist_cm = struct.unpack_from(">bI", msg.payload)
                self._process_rssi(peer, raw_rssi, dist_cm / 100.0)

    def _process_rssi(self, peer: "Peer", rssi: int,
                      reported_dist: float | None = None) -> None:
        filt = self._filters.setdefault(peer.mac, KalmanRSSI())
        smoothed = filt.update(float(rssi))
        distance = reported_dist if reported_dist is not None else rssi_to_distance(smoothed)
        self._distances[peer.mac] = distance
        peer.record_rssi(rssi)
        self.bus.emit_nowait(
            RANGING_UPDATE,
            peer       = peer,
            rssi       = rssi,
            rssi_smooth = smoothed,
            distance_m = distance,
        )
        log.debug(f"Ranging {peer.name or peer.mac}: RSSI={rssi} dBm  d={distance:.2f} m")

    def distance_to(self, mac: str) -> float | None:
        """Return last estimated distance in metres, or None if unknown."""
        return self._distances.get(mac.upper())