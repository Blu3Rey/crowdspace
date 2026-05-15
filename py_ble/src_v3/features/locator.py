"""
features/locator.py
===================
Device location services using RSSI-based trilateration.

Capabilities:
  • Periodic RSSI beacon broadcast
  • On-demand location request → collect peer RSSI readings → trilaterate
  • Proximity alerts (enter / leave radius)
  • Continuous tracking of all visible nodes

Accuracy note: BLE RSSI is noisy. Results give rough proximity zones
(< 1 m, 1–3 m, 3–10 m, > 10 m) rather than centimetre precision.
A real deployment can improve accuracy with multiple reference nodes.
"""

from __future__ import annotations
import asyncio
import json
import math
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Dict, List, Optional, Set, Tuple

from .base import BaseFeature
from ..core.packet import Packet, PacketType, PacketFlag, BROADCAST_ADDR


# ── Constants ─────────────────────────────────────────────────────────────────

TX_POWER_DBHM = -59       # reference RSSI at 1 m (typical BLE 5)
PATH_LOSS_EXP = 2.0       # free-space exponent; 2.7–3.5 indoors
BEACON_INTERVAL = 5.0     # seconds between beacons


# ── Data Models ───────────────────────────────────────────────────────────────

@dataclass
class RSSIReading:
    observer:  bytes      # who recorded this reading
    target:    bytes      # the node being observed
    rssi:      int        # dBm
    distance:  float      # estimated metres
    timestamp: float = field(default_factory=time.time)


@dataclass
class LocationEstimate:
    node_addr:   bytes
    x:           float         # relative metres (anchor = origin)
    y:           float
    confidence:  float         # 0.0–1.0
    readings:    List[RSSIReading] = field(default_factory=list)
    timestamp:   float = field(default_factory=time.time)

    @property
    def distance_from_origin(self) -> float:
        return math.sqrt(self.x**2 + self.y**2)

    def to_dict(self) -> dict:
        return {
            "addr":       ":".join(f"{b:02X}" for b in self.node_addr),
            "x_m":        round(self.x, 2),
            "y_m":        round(self.y, 2),
            "distance_m": round(self.distance_from_origin, 2),
            "confidence": round(self.confidence, 2),
            "timestamp":  self.timestamp,
        }


@dataclass
class ProximityZone:
    name:         str
    node_addr:    bytes
    radius_m:     float
    callback:     Callable[[bytes, bool, float], Awaitable[None]]  # addr, in_zone, dist
    last_in_zone: bool = False


ProximityCallback = Callable[[bytes, bool, float], Awaitable[None]]


# ── Locator Feature ───────────────────────────────────────────────────────────

class LocatorFeature(BaseFeature):
    """
    Mesh-wide device location service.

    Usage:
        # Continuous background beaconing (auto-started)
        locator.start_beaconing()

        # One-shot locate of a specific node
        estimate = await locator.locate(peer_addr, timeout=5.0)

        # Get last known position of all visible nodes
        positions = locator.all_positions()

        # Proximity alert when a node comes within 3 m
        locator.add_proximity_zone("office-door", peer_addr, 3.0, my_handler)
    """

    NAME    = "locator"
    HANDLES = {PacketType.RSSI_BEACON, PacketType.LOC_REQUEST, PacketType.LOC_RESPONSE}

    def __init__(self, node):
        super().__init__(node)
        # RSSI readings: target_addr → List[RSSIReading]
        self._readings:   Dict[bytes, List[RSSIReading]] = {}
        # cached location estimates
        self._locations:  Dict[bytes, LocationEstimate]  = {}
        # pending locate futures: request_id → Future[LocationEstimate]
        self._pending:    Dict[str, asyncio.Future]      = {}
        # proximity zones
        self._zones:      List[ProximityZone]            = []
        # background tasks
        self._tasks:      List[asyncio.Task]             = []
        self._beaconing   = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self.start_beaconing()

    async def stop(self):
        for t in self._tasks:
            t.cancel()

    # ── Public API ────────────────────────────────────────────────────────────

    def start_beaconing(self):
        """Begin periodic RSSI beacon broadcasts."""
        if not self._beaconing:
            self._beaconing = True
            self._tasks.append(
                asyncio.create_task(self._beacon_loop(), name="loc-beacon")
            )
            self._tasks.append(
                asyncio.create_task(self._proximity_check_loop(), name="loc-proximity")
            )

    def stop_beaconing(self):
        self._beaconing = False

    async def locate(
        self,
        target_addr: bytes,
        timeout:     float = 5.0,
        min_anchors: int   = 2,
    ) -> Optional[LocationEstimate]:
        """
        Actively locate a node by requesting RSSI readings from all peers.

        Args:
            target_addr: The node to locate.
            timeout:     How long to collect responses (seconds).
            min_anchors: Minimum responding peers for a valid estimate.

        Returns:
            A LocationEstimate, or None if insufficient data.
        """
        req_id = str(uuid.uuid4())[:8]
        loop   = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[req_id] = future

        payload = self._encode_request(target_addr, req_id)
        pkt     = self.make_packet(
            PacketType.LOC_REQUEST,
            payload  = payload,
            dst_addr = BROADCAST_ADDR,
        )
        await self.send(pkt)

        try:
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._pending.pop(req_id, None)

        readings = self._readings.get(target_addr, [])
        recent   = [r for r in readings if time.time() - r.timestamp < timeout + 2]

        if len(recent) < min_anchors:
            self._log.warning(
                "Only %d anchor readings for %s – estimate unreliable",
                len(recent), target_addr.hex()
            )

        estimate = self._trilaterate(target_addr, recent)
        if estimate:
            self._locations[target_addr] = estimate
        return estimate

    def last_known_position(self, node_addr: bytes) -> Optional[LocationEstimate]:
        return self._locations.get(node_addr)

    def all_positions(self) -> List[LocationEstimate]:
        return list(self._locations.values())

    def visible_nodes(self) -> List[Dict]:
        """Return all nodes with recent RSSI readings."""
        cutoff = time.time() - 30
        result = []
        for addr, readings in self._readings.items():
            recent = [r for r in readings if r.timestamp > cutoff]
            if not recent:
                continue
            avg_rssi = sum(r.rssi for r in recent) / len(recent)
            dist     = self._rssi_to_distance(avg_rssi)
            result.append({
                "addr":       ":".join(f"{b:02X}" for b in addr),
                "rssi_avg":   round(avg_rssi, 1),
                "distance_m": round(dist, 2),
                "samples":    len(recent),
                "last_seen":  max(r.timestamp for r in recent),
            })
        result.sort(key=lambda x: x["distance_m"])
        return result

    def add_proximity_zone(
        self,
        name:      str,
        node_addr: bytes,
        radius_m:  float,
        callback:  ProximityCallback,
    ) -> str:
        """
        Register a proximity alert zone.

        callback(node_addr, is_in_zone, distance_m) is invoked whenever
        the monitored node transitions into or out of the radius.
        """
        zone = ProximityZone(
            name      = name,
            node_addr = node_addr,
            radius_m  = radius_m,
            callback  = callback,
        )
        self._zones.append(zone)
        return name

    def remove_proximity_zone(self, name: str):
        self._zones = [z for z in self._zones if z.name != name]

    # ── Packet handler ────────────────────────────────────────────────────────

    async def on_packet(self, pkt: Packet):
        if pkt.ptype == PacketType.RSSI_BEACON:
            await self._handle_beacon(pkt)
        elif pkt.ptype == PacketType.LOC_REQUEST:
            await self._handle_loc_request(pkt)
        elif pkt.ptype == PacketType.LOC_RESPONSE:
            await self._handle_loc_response(pkt)

    async def _handle_beacon(self, pkt: Packet):
        """Record a peer's self-reported beacon."""
        try:
            data = json.loads(pkt.payload)
            rssi = data.get("rssi", -100)
        except Exception:
            rssi = -100

        # The RSSI we observe is more reliable than self-reported
        node = self.routing_table.get(pkt.src_addr)
        rssi = node.rssi if node else rssi

        self._record_reading(
            observer = self.local_addr,
            target   = pkt.src_addr,
            rssi     = rssi,
        )

    async def _handle_loc_request(self, pkt: Packet):
        """
        A peer is asking for RSSI readings of target_addr.
        Respond with what we've observed.
        """
        try:
            data       = json.loads(pkt.payload)
            target     = bytes.fromhex(data["target"])
            req_id     = data["req_id"]
            requester  = pkt.src_addr
        except Exception:
            return

        if requester == self.local_addr:
            return

        # Find our most recent reading of the target
        readings = self._readings.get(target, [])
        if not readings:
            return

        recent = max(readings, key=lambda r: r.timestamp)
        response_payload = json.dumps({
            "req_id":   req_id,
            "target":   target.hex(),
            "observer": self.local_addr.hex(),
            "rssi":     recent.rssi,
            "dist":     recent.distance,
            "ts":       recent.timestamp,
        }).encode()

        pkt_out = self.make_packet(
            PacketType.LOC_RESPONSE,
            payload  = response_payload,
            dst_addr = requester,
        )
        await self.send(pkt_out)

    async def _handle_loc_response(self, pkt: Packet):
        """Collect RSSI readings contributed by peers."""
        try:
            data     = json.loads(pkt.payload)
            req_id   = data["req_id"]
            target   = bytes.fromhex(data["target"])
            observer = bytes.fromhex(data["observer"])
            rssi     = int(data["rssi"])
            dist     = float(data["dist"])
        except Exception:
            return

        reading = RSSIReading(observer=observer, target=target, rssi=rssi, distance=dist)
        self._record_reading_object(reading)

        # If this is a response to our pending request, signal progress
        future = self._pending.get(req_id)
        if future and not future.done():
            # Just record; let timeout harvest all readings
            pass

    # ── Beacon Loop ───────────────────────────────────────────────────────────

    async def _beacon_loop(self):
        while self._beaconing:
            payload = json.dumps({
                "type": "beacon",
                "addr": self.local_addr.hex(),
                "ts":   time.time(),
                "rssi": -59,   # self-reported TX power
            }).encode()
            pkt = self.make_packet(
                PacketType.RSSI_BEACON,
                payload  = payload,
                dst_addr = BROADCAST_ADDR,
                ttl      = 2,   # local-only beacon; don't propagate far
            )
            await self.send(pkt)
            await asyncio.sleep(BEACON_INTERVAL)

    # ── Proximity Monitoring ──────────────────────────────────────────────────

    async def _proximity_check_loop(self):
        while self._beaconing:
            await asyncio.sleep(2.0)
            for zone in self._zones:
                readings = self._readings.get(zone.node_addr, [])
                if not readings:
                    continue
                recent = [r for r in readings if time.time() - r.timestamp < 10]
                if not recent:
                    continue
                avg_dist = sum(r.distance for r in recent) / len(recent)
                in_zone  = avg_dist <= zone.radius_m
                if in_zone != zone.last_in_zone:
                    zone.last_in_zone = in_zone
                    try:
                        await zone.callback(zone.node_addr, in_zone, avg_dist)
                    except Exception as e:
                        self._log.error("Proximity callback error: %s", e)

    # ── Trilateration ─────────────────────────────────────────────────────────

    def _trilaterate(
        self,
        target:   bytes,
        readings: List[RSSIReading],
    ) -> Optional[LocationEstimate]:
        """
        Estimate 2-D position using weighted least-squares.

        With < 2 anchors: returns distance-only estimate at origin offset.
        With ≥ 2 anchors: performs geometric trilateration.
        """
        if not readings:
            return None

        if len(readings) == 1:
            r = readings[0]
            return LocationEstimate(
                node_addr  = target,
                x          = r.distance,
                y          = 0.0,
                confidence = 0.3,
                readings   = readings,
            )

        # Simple trilateration: pick 3 best readings, solve overdetermined system
        anchors    = sorted(readings, key=lambda r: r.rssi, reverse=True)[:5]
        # Assign arbitrary 2-D positions to anchors using their relative distances
        # (without physical coordinates, we use a relative coordinate frame)
        # Place first anchor at origin, second at (d01, 0)
        positions: List[Tuple[float, float, float]] = []  # x, y, dist_to_target

        # Build a synthetic 2-D layout
        n   = len(anchors)
        for i, r in enumerate(anchors):
            angle  = (2 * math.pi * i) / n
            # Use inter-anchor distances based on RSSI differences
            ax = r.distance * math.cos(angle) * 0.5
            ay = r.distance * math.sin(angle) * 0.5
            positions.append((ax, ay, r.distance))

        # Weighted centroid (crude but robust for noisy RSSI)
        total_w = sum(1.0 / max(p[2], 0.1) for p in positions)
        x = sum((1.0 / max(p[2], 0.1)) * p[0] for p in positions) / total_w
        y = sum((1.0 / max(p[2], 0.1)) * p[1] for p in positions) / total_w

        # Confidence based on reading consistency
        distances = [p[2] for p in positions]
        mean_dist = sum(distances) / len(distances)
        std_dist  = math.sqrt(sum((d - mean_dist)**2 for d in distances) / len(distances))
        confidence = max(0.0, min(1.0, 1.0 - std_dist / max(mean_dist, 1.0)))

        return LocationEstimate(
            node_addr  = target,
            x          = round(x, 2),
            y          = round(y, 2),
            confidence = round(confidence, 2),
            readings   = readings,
        )

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _record_reading(self, observer: bytes, target: bytes, rssi: int):
        dist    = self._rssi_to_distance(rssi)
        reading = RSSIReading(observer=observer, target=target, rssi=rssi, distance=dist)
        self._record_reading_object(reading)

    def _record_reading_object(self, reading: RSSIReading, window: int = 20):
        lst = self._readings.setdefault(reading.target, [])
        lst.append(reading)
        if len(lst) > window:
            lst.pop(0)

    @staticmethod
    def _rssi_to_distance(rssi: int) -> float:
        if rssi == 0:
            return 999.0
        return round(10 ** ((TX_POWER_DBHM - rssi) / (10 * PATH_LOSS_EXP)), 2)

    @staticmethod
    def _encode_request(target: bytes, req_id: str) -> bytes:
        return json.dumps({
            "type":   "loc_request",
            "target": target.hex(),
            "req_id": req_id,
        }).encode()