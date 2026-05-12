"""
core/mesh_router.py
===================
Multi-hop mesh routing engine.

Strategy:
  1. Flooding (default) – re-broadcast with TTL decrement; deduplication via
     seen-packet cache prevents loops.
  2. Directed routing – when a route is known, send only toward the
     best next-hop to conserve airtime.
  3. Reliability layer – optional ACK + retransmit for RELIABLE-flagged packets.
  4. Route discovery – ROUTE_REQUEST / ROUTE_REPLY protocol to populate the
     routing table on demand.
"""

from __future__ import annotations
import asyncio
import logging
import time
from collections import OrderedDict
from typing import Callable, Dict, Optional, Set, Awaitable, List, Tuple

from .packet import (
    Packet, PacketType, PacketFlag, PacketFactory,
    BROADCAST_ADDR, FragmentBuffer
)
from .node import RoutingTable, PeerNode
from .crypto import KeyManager

log = logging.getLogger(__name__)


# ── Types ─────────────────────────────────────────────────────────────────────

SendCallback    = Callable[[bytes, bytes], Awaitable[bool]]   # peer_addr, raw_bytes → success
DeliverCallback = Callable[[Packet], Awaitable[None]]          # fully-routed packet


# ── Seen-Packet Cache ─────────────────────────────────────────────────────────

class SeenCache:
    """
    Fixed-size LRU cache for (src_addr, seq_num) pairs.
    Used to drop duplicate / already-forwarded packets.
    """

    def __init__(self, capacity: int = 1024, ttl: float = 30.0):
        self._cache: OrderedDict[tuple, float] = OrderedDict()
        self._cap   = capacity
        self._ttl   = ttl

    def seen(self, pkt: Packet) -> bool:
        key = pkt.cache_key
        now = time.monotonic()
        self._evict(now)
        if key in self._cache:
            return True
        self._cache[key] = now
        self._cache.move_to_end(key)
        if len(self._cache) > self._cap:
            self._cache.popitem(last=False)
        return False

    def _evict(self, now: float):
        cutoff = now - self._ttl
        stale  = [k for k, t in self._cache.items() if t < cutoff]
        for k in stale:
            del self._cache[k]


# ── Pending ACK ───────────────────────────────────────────────────────────────

class PendingAck:
    def __init__(self, pkt: Packet, retries: int, interval: float):
        self.pkt      = pkt
        self.retries  = retries
        self.interval = interval
        self.attempts = 0
        self.next_try = time.monotonic() + interval


# ── Mesh Router ───────────────────────────────────────────────────────────────

class MeshRouter:
    """
    Core packet-forwarding engine.

    Responsibilities:
      • receive(pkt, rssi) – process an incoming packet
      • send(pkt)          – originate and route a packet
      • forward(pkt)       – re-broadcast a packet one hop
      • ACK / retransmit   – reliability for RELIABLE-flagged packets
      • Route discovery    – RREQ / RREP protocol
    """

    FLOOD_NEIGHBORS_LIMIT = 8    # max peers to flood to simultaneously
    MAX_RETRIES           = 3
    RETRY_INTERVAL        = 0.5  # seconds
    ROUTE_DISC_TIMEOUT    = 3.0  # wait for RREP

    def __init__(
        self,
        local_addr:    bytes,
        factory:       PacketFactory,
        routing_table: RoutingTable,
        crypto:        KeyManager,
        on_send:       SendCallback,
        on_deliver:    DeliverCallback,
    ):
        self._addr   = local_addr
        self._factory = factory
        self._rt      = routing_table
        self._crypto  = crypto
        self._on_send = on_send
        self._deliver = on_deliver

        self._seen       = SeenCache()
        self._frags      = FragmentBuffer()
        self._pending_acks: Dict[Tuple[bytes, int], PendingAck] = {}

        # Route-discovery: futures waiting for a RREP
        self._route_waiters: Dict[bytes, asyncio.Future] = {}

        self._tasks: List[asyncio.Task] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        self._tasks.append(
            asyncio.create_task(self._ack_retry_loop(), name="router-ack-retry")
        )
        self._tasks.append(
            asyncio.create_task(self._table_maintenance(), name="router-maintenance")
        )

    async def stop(self):
        for t in self._tasks:
            t.cancel()

    # ── Receive Path ──────────────────────────────────────────────────────────

    async def receive(self, pkt: Packet, rssi: int = -100):
        """
        Entry point for every packet arriving from the BLE layer.
        """
        # 1. Update routing table from observation
        self._rt.upsert(pkt.src_addr, rssi=rssi, hop_distance=pkt.hop_count + 1,
                        next_hop=pkt.src_addr)

        # 2. Decrypt if encrypted
        pkt = self._crypto.decrypt_packet(pkt, peer_addr=pkt.src_addr)
        if pkt is None:
            log.debug("[Router] Decrypt failed – drop")
            return

        # 3. Fragment reassembly
        if pkt.is_fragmented:
            pkt = self._frags.add(pkt)
            if pkt is None:
                return   # waiting for remaining fragments

        # 4. Deduplication
        if self._seen.seen(pkt):
            log.debug("[Router] Dup dropped: %s", pkt)
            return

        # 5. Dispatch control packets
        await self._handle_control(pkt, rssi)

        # 6. Route or deliver
        is_for_me = (pkt.dst_addr == self._addr or pkt.is_broadcast or
                     pkt.dst_addr == BROADCAST_ADDR)
        if is_for_me:
            await self._deliver(pkt)
            if pkt.dst_addr == self._addr and PacketFlag.RELIABLE in pkt.flags:
                await self._send_ack(pkt)

        # 7. Forward if not expired and not destined only for us
        if pkt.ttl > 1 and not (is_for_me and not pkt.is_broadcast):
            await self._forward(pkt)

    # ── Send Path ─────────────────────────────────────────────────────────────

    async def send(self, pkt: Packet):
        """
        Originate a packet from this node.
        Handles fragmentation, encryption, and routing.
        """
        # Fragment if necessary
        frags = self._factory.fragment(pkt)

        for frag in frags:
            # Encrypt
            encrypted = self._crypto.encrypt_packet(frag)

            # Track for ACK
            if PacketFlag.RELIABLE in pkt.flags:
                key = (pkt.dst_addr, pkt.seq_num)
                self._pending_acks[key] = PendingAck(
                    encrypted, self.MAX_RETRIES, self.RETRY_INTERVAL
                )

            await self._route_and_send(encrypted)

    # ── Internal Routing ──────────────────────────────────────────────────────

    async def _route_and_send(self, pkt: Packet):
        """Choose routing strategy and dispatch."""
        if pkt.is_broadcast or pkt.dst_addr == BROADCAST_ADDR:
            await self._flood(pkt)
        else:
            next_hop, cost = self._rt.best_route_to(pkt.dst_addr)
            if next_hop is not None:
                raw = pkt.to_bytes()
                ok  = await self._on_send(next_hop, raw)
                if not ok:
                    log.debug("[Router] Directed send failed; falling back to flood")
                    await self._flood(pkt)
            else:
                # Trigger route discovery then retry
                found = await self._discover_route(pkt.dst_addr)
                if found:
                    await self._route_and_send(pkt)
                else:
                    log.warning("[Router] No route to %s – flooding as last resort",
                                ":".join(f"{b:02X}" for b in pkt.dst_addr))
                    await self._flood(pkt)

    async def _flood(self, pkt: Packet):
        """Re-broadcast to all direct neighbours (up to FLOOD_NEIGHBORS_LIMIT)."""
        neighbors = self._rt.neighbors()[:self.FLOOD_NEIGHBORS_LIMIT]
        raw       = pkt.to_bytes()
        results   = await asyncio.gather(
            *[self._on_send(n.addr, raw) for n in neighbors],
            return_exceptions=True,
        )
        sent = sum(1 for r in results if r is True)
        log.debug("[Router] Flooded to %d/%d neighbours", sent, len(neighbors))

    async def _forward(self, pkt: Packet):
        """Decrement TTL and forward."""
        fwd           = Packet(
            ptype      = pkt.ptype,
            src_addr   = pkt.src_addr,
            dst_addr   = pkt.dst_addr,
            group_id   = pkt.group_id,
            seq_num    = pkt.seq_num,
            ttl        = pkt.ttl - 1,
            hop_count  = pkt.hop_count + 1,
            flags      = pkt.flags,
            frag_idx   = pkt.frag_idx,
            frag_total = pkt.frag_total,
            payload    = pkt.payload,
            tag        = pkt.tag,
        )
        await self._route_and_send(fwd)

    # ── Control Plane ─────────────────────────────────────────────────────────

    async def _handle_control(self, pkt: Packet, rssi: int):
        if pkt.ptype == PacketType.HEARTBEAT:
            await self._handle_heartbeat(pkt, rssi)
        elif pkt.ptype == PacketType.ACK:
            self._handle_ack(pkt)
        elif pkt.ptype == PacketType.ROUTE_REQUEST:
            await self._handle_rreq(pkt)
        elif pkt.ptype == PacketType.ROUTE_REPLY:
            self._handle_rrep(pkt)
        elif pkt.ptype == PacketType.PING:
            await self._handle_ping(pkt)
        elif pkt.ptype == PacketType.PONG:
            self._handle_pong(pkt)

    async def _handle_heartbeat(self, pkt: Packet, rssi: int):
        """Parse heartbeat payload and update routing table."""
        import json
        try:
            meta = json.loads(pkt.payload.decode("utf-8", errors="replace"))
            self._rt.upsert(
                addr        = pkt.src_addr,
                rssi        = rssi,
                name        = meta.get("name", ""),
                hop_distance= 1,
                next_hop    = pkt.src_addr,
                public_key  = bytes.fromhex(meta["pk"]) if "pk" in meta else None,
                groups      = set(meta.get("groups", [])),
                features    = set(meta.get("features", [])),
            )
        except Exception:
            pass

    def _handle_ack(self, pkt: Packet):
        """Remove from pending retransmit queue."""
        import struct
        try:
            orig_seq = struct.unpack("<I", pkt.payload[:4])[0]
            key      = (pkt.src_addr, orig_seq)
            self._pending_acks.pop(key, None)
            log.debug("[Router] ACK received for seq=%d", orig_seq)
        except Exception:
            pass

    async def _send_ack(self, pkt: Packet):
        import struct
        ack = self._factory.build(
            PacketType.ACK,
            payload  = struct.pack("<I", pkt.seq_num),
            dst_addr = pkt.src_addr,
            ttl      = 3,
        )
        await self._route_and_send(ack)

    async def _handle_rreq(self, pkt: Packet):
        """Route Request: if we know the dest, send a RREP."""
        if len(pkt.payload) < 6:
            return
        target = pkt.payload[:6]
        if target == self._addr or self._rt.get(target) is not None:
            await self._send_rrep(pkt.src_addr, target, pkt.seq_num)

    async def _send_rrep(self, requester: bytes, target: bytes, rreq_seq: int):
        import struct
        MAX_TTL = 7
        payload = target + struct.pack("<I", rreq_seq)
        rrep    = self._factory.build(
            PacketType.ROUTE_REPLY,
            payload  = payload,
            dst_addr = requester,
            ttl      = MAX_TTL,
        )
        await self._route_and_send(rrep)

    def _handle_rrep(self, pkt: Packet):
        if len(pkt.payload) < 6:
            return
        target = pkt.payload[:6]
        waiter = self._route_waiters.pop(target, None)
        if waiter and not waiter.done():
            waiter.set_result(True)

    async def _discover_route(self, target: bytes) -> bool:
        """Flood a ROUTE_REQUEST and wait up to ROUTE_DISC_TIMEOUT for a reply."""
        if target in self._route_waiters:
            return await self._route_waiters[target]

        loop   = asyncio.get_event_loop()
        future = loop.create_future()
        self._route_waiters[target] = future

        rreq = self._factory.build(
            PacketType.ROUTE_REQUEST,
            payload  = target,
            dst_addr = BROADCAST_ADDR,
            ttl      = 7,
        )
        await self._flood(rreq)

        try:
            return await asyncio.wait_for(future, timeout=self.ROUTE_DISC_TIMEOUT)
        except asyncio.TimeoutError:
            self._route_waiters.pop(target, None)
            return False

    async def _handle_ping(self, pkt: Packet):
        pong = self._factory.build(
            PacketType.PONG,
            payload  = pkt.payload,
            dst_addr = pkt.src_addr,
            ttl      = 3,
        )
        await self._route_and_send(pong)

    def _handle_pong(self, pkt: Packet):
        """Compute RTT and update routing table."""
        import struct
        try:
            sent_ts = struct.unpack("<d", pkt.payload[:8])[0]
            rtt_ms  = (time.monotonic() - sent_ts) * 1000
            node    = self._rt.get(pkt.src_addr)
            if node:
                node.rtt_ms = rtt_ms
            log.debug("[Router] RTT to %s: %.1f ms", pkt.src_addr.hex(), rtt_ms)
        except Exception:
            pass

    # ── Background Tasks ──────────────────────────────────────────────────────

    async def _ack_retry_loop(self):
        while True:
            await asyncio.sleep(0.1)
            now    = time.monotonic()
            failed = []
            for key, pending in list(self._pending_acks.items()):
                if now < pending.next_try:
                    continue
                if pending.attempts >= pending.retries:
                    log.warning("[Router] Packet unacknowledged after %d retries", pending.retries)
                    failed.append(key)
                    continue
                pending.attempts += 1
                pending.next_try  = now + pending.interval * (2 ** pending.attempts)
                await self._route_and_send(pending.pkt)
            for k in failed:
                del self._pending_acks[k]

    async def _table_maintenance(self):
        while True:
            await asyncio.sleep(30)
            stale = self._rt.evict_stale()
            if stale:
                log.debug("[Router] Evicted %d stale routes", len(stale))