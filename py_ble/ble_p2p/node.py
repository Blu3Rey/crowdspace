"""
node.py — BLEMeshNode: the top-level orchestrator.

The Node owns every other component and runs three concurrent asyncio tasks:

  1. _peripheral_task  — keeps the GATT server running indefinitely.
  2. _scan_loop_task   — periodic: scan → connect → session → repeat.
  3. _housekeeping_task— periodic: expire fragments, purge DB, log stats.

Public API (minimal surface area by design)
--------------------------------------------
node = BLEMeshNode(name="Alice")
dm   = DirectMessageFeature(node)
node.register_feature(dm)
dm.on_message(my_handler)
await node.start()
await dm.send(peer_id, "Hello!")
await node.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional

from .constants import (
    MsgType, MsgFlags, BROADCAST_ID,
    SCAN_DURATION_S, SCAN_INTERVAL_S, ACK_TIMEOUT_S, MAX_RETRIES,
    RECONNECT_BACKOFF_S,
)
from .device    import LocalDevice
from .message   import OutboundMessage
from .protocol  import Protocol
from .network.peer   import PeerRegistry
from .network.router import Router
from .storage.store  import MessageStore
from .transport.peripheral import BLEPeripheral
from .transport.central    import BLECentral
from .features.base        import Feature

log = logging.getLogger(__name__)


class BLEMeshNode:
    """
    A single BLE-P2P node.

    Responsibilities
    ----------------
    • Maintain a GATT server (peripheral role) so peers can send us messages.
    • Periodically scan and open ephemeral sessions to deliver queued messages.
    • Route received messages to registered Feature handlers.
    • Manage per-peer outbound queues with retry and backoff.
    """

    def __init__(
        self,
        name         : Optional[str] = None,
        capabilities : int            = 0,
        scan_interval: float          = SCAN_INTERVAL_S,
        scan_duration: float          = SCAN_DURATION_S,
    ):
        self.device        = LocalDevice(name=name, capabilities=capabilities)
        self.protocol      = Protocol(device_id=self.device.device_id)
        self.peers         = PeerRegistry()
        self.store         = MessageStore()
        self.router        = Router(
            local_device_id=self.device.device_id,
            local_name=self.device.name,
        )
        self.peripheral    = BLEPeripheral(
            name=self.device.name,
            info_payload=self.device.info_payload(),
        )
        self.central       = BLECentral(local_device_id=self.device.device_id)

        self._scan_interval = scan_interval
        self._scan_duration = scan_duration

        # Per-peer outbound queue: device_id_hex → list of raw frames
        self._outbound     : Dict[str, List[bytes]] = defaultdict(list)
        # Track last connection attempt time per peer for backoff
        self._last_attempt : Dict[str, float]       = {}

        # Asyncio task handles
        self._peripheral_task   : Optional[asyncio.Task] = None
        self._scan_task         : Optional[asyncio.Task] = None
        self._housekeeping_task : Optional[asyncio.Task] = None
        self._running = False

        # Wire up callbacks
        self.peripheral.set_data_callback(self._on_incoming_frame)
        self.central.set_data_callback(self._on_incoming_frame)
        self.central.set_discovery_callback(self._on_peer_discovered)

        # Wire up router hooks
        self.router.on_handshake  = self._handle_handshake_data
        self.router.on_ack_needed = self._send_ack
        self.router.on_send_pong  = self._send_pong

        # Restore peers from DB on startup
        self._restore_peers()

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self):
        """Start all background tasks.  Returns immediately."""
        if self._running:
            return
        self._running = True
        log.info("Starting BLEMeshNode %s (%s)", self.device.name, self.device.id_hex)

        # Notify all registered features
        for feat in self.router.get_all_features().values():
            await feat.on_start()

        self._peripheral_task   = asyncio.create_task(self._peripheral_loop(),    name="peripheral")
        self._scan_task         = asyncio.create_task(self._scan_loop(),           name="scan_loop")
        self._housekeeping_task = asyncio.create_task(self._housekeeping_loop(),   name="housekeeping")

    async def stop(self):
        """Gracefully stop the node and flush SQLite."""
        if not self._running:
            return
        self._running = False
        log.info("Stopping BLEMeshNode %s", self.device.name)

        for t in (self._peripheral_task, self._scan_task, self._housekeeping_task):
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

        await self.peripheral.stop()

        for feat in self.router.get_all_features().values():
            await feat.on_stop()

        self.store.close()
        log.info("BLEMeshNode stopped")

    # ── Feature registration ──────────────────────────────────

    def register_feature(self, feature: Feature):
        """Register a Feature module.  Must be called before start()."""
        self.router.register_feature(feature)

    # ── Message sending (public) ──────────────────────────────

    async def send_message(
        self,
        msg_type : int,
        payload  : bytes,
        dst_id   : bytes,
        flags    : int = 0,
    ) -> bool:
        """
        Queue a logical message for delivery.

        Fragments the payload and places raw BLE frames into the per-peer
        outbound queue.  Frames will be written during the next session with
        the peer (or immediately if the peer is currently in a session).

        Returns True if successfully enqueued.
        """
        try:
            is_broadcast = (dst_id == BROADCAST_ID or bool(flags & MsgFlags.BROADCAST))
            target_hex   = BROADCAST_ID.hex() if is_broadcast else dst_id.hex()

            frames = self.protocol.fragment(
                msg_type = msg_type,
                payload  = payload,
                dst_id   = dst_id,
                flags    = flags,
            )

            msg = OutboundMessage(
                msg_type=msg_type, payload=payload,
                dst_id=dst_id, flags=flags,
            )

            # In-memory queue
            if is_broadcast:
                # Broadcast: enqueue for all known live peers
                for peer in self.peers.fresh_peers():
                    if peer.device_id != self.device.device_id:
                        self._outbound[peer.id_hex].extend(frames)
            else:
                self._outbound[target_hex].extend(frames)

            # Persistent queue for reliability (frames, not the full message)
            for frame in frames:
                self.store.enqueue_frame(target_hex, frame, msg.message_id)

            log.debug(
                "Enqueued %d frame(s) for %s (type=%#x, %d B payload)",
                len(frames), target_hex[:8], msg_type, len(payload),
            )
            return True

        except Exception as exc:
            log.error("send_message failed: %s", exc)
            return False

    # ── Background tasks ──────────────────────────────────────

    async def _peripheral_loop(self):
        """Keep the GATT server running, restarting on unexpected errors."""
        while self._running:
            try:
                await self.peripheral.start()
                # Run until cancelled
                while self._running:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Peripheral crashed: %s — restarting in 5 s", exc)
                await asyncio.sleep(5.0)
        await self.peripheral.stop()

    async def _scan_loop(self):
        """
        Periodic scan → discover peers → open sessions → deliver queued messages.
        """
        # Short initial delay so the peripheral has time to start
        await asyncio.sleep(3.0)

        while self._running:
            try:
                await self._do_scan_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Scan cycle error: %s", exc)

            try:
                await asyncio.sleep(self._scan_interval)
            except asyncio.CancelledError:
                break

    async def _do_scan_cycle(self):
        log.debug("Scan cycle starting (duration=%.1f s)", self._scan_duration)
        found = await self.central.scan(duration=self._scan_duration)

        for device, adv_data in found:
            addr    = device.address
            rssi    = adv_data.rssi if hasattr(adv_data, "rssi") else -127
            peer    = self.peers.by_address(addr)

            if peer is None:
                # First encounter — read device info
                info_raw = await self.central.read_peer_info(device)
                if info_raw:
                    try:
                        info = json.loads(info_raw)
                        dev_id_hex   = info.get("id", "")
                        name         = info.get("name", addr)
                        caps         = info.get("caps", 0)
                        if dev_id_hex:
                            peer = self.peers.upsert(
                                device_id   = bytes.fromhex(dev_id_hex),
                                name        = name,
                                capabilities= caps,
                                ble_address = addr,
                                rssi        = rssi,
                            )
                            self.store.upsert_peer(dev_id_hex, name, caps, addr)
                    except Exception as exc:
                        log.warning("Failed to parse peer info from %s: %s", addr, exc)
                        continue
                else:
                    continue   # Could not identify — skip
            else:
                self.peers.update_rssi(addr, rssi)

                # Update locator RSSI if registered
                locator = self.router.get_feature(0x03)   # FeatureID.DEVICE_LOCATOR
                if locator:
                    locator.record_advertisement_rssi(peer.id_hex, peer.name, rssi)

            if peer is None:
                continue

            # Back-off check
            last = self._last_attempt.get(peer.id_hex, 0.0)
            if peer.connect_failures > 0:
                backoff = min(RECONNECT_BACKOFF_S * peer.connect_failures, 300.0)
                if time.monotonic() - last < backoff:
                    log.debug("Skipping %s (backoff %.0f s)", peer.name, backoff)
                    continue

            # Check if we have anything to send
            pending_frames = self._collect_pending(peer.id_hex)

            # Always do a session even without outbound data — lets the peer
            # send us anything it has queued.  Send HANDSHAKE if no real data.
            if not pending_frames:
                handshake_frames = self.protocol.fragment(
                    msg_type = MsgType.HANDSHAKE,
                    payload  = self.device.info_payload(),
                    dst_id   = peer.device_id,
                    flags    = int(MsgFlags.PRIORITY),
                )
                pending_frames = handshake_frames
            else:
                # Prepend a handshake so the peer knows who we are
                hs = self.protocol.fragment(
                    msg_type = MsgType.HANDSHAKE,
                    payload  = self.device.info_payload(),
                    dst_id   = peer.device_id,
                    flags    = int(MsgFlags.PRIORITY),
                )
                pending_frames = hs + pending_frames

            self._last_attempt[peer.id_hex] = time.monotonic()
            peer.pending_connect = True

            result = await self.central.open_session(device, pending_frames)

            if result.success:
                peer.mark_connected()
                # Notify features
                for feat in self.router.get_all_features().values():
                    await feat.on_peer_connected(peer.device_id, peer.name)
                # Clear in-memory queue and mark DB rows delivered
                self._outbound.pop(peer.id_hex, None)
                rows = self.store.get_pending_frames(peer.id_hex)
                if rows:
                    self.store.mark_delivered([r.row_id for r in rows])
                log.info(
                    "Session with %s: sent=%d notif=%d",
                    peer.name, result.frames_sent, result.notifications_rcvd,
                )
            else:
                peer.mark_failed()
                log.warning(
                    "Session with %s failed: %s (failures=%d)",
                    peer.name, result.error, peer.connect_failures,
                )

    async def _housekeeping_loop(self):
        """Periodic maintenance every 60 seconds."""
        while self._running:
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                break
            self.protocol.expire_fragments()
            self.store.purge_old_delivered(older_than_s=3600.0)
            self.store.purge_expired_undelivered(older_than_s=3600.0)
            log.debug(
                "Housekeeping: %d known peers, %d in-memory queues",
                len(self.peers), len(self._outbound),
            )

    # ── Incoming frame processing ─────────────────────────────

    async def _on_incoming_frame(self, raw: bytes, source_addr: str):
        """Called by both the peripheral and central callbacks."""
        result = self.protocol.process_incoming(raw)
        if result is None:
            return   # malformed, duplicate, or incomplete fragment

        first_frag, complete_payload = result
        h = first_frag.header

        # Give the incoming RSSI to the locator (source_addr may be "peripheral")
        if source_addr != "peripheral":
            peer = self.peers.by_address(source_addr)
            if peer:
                locator = self.router.get_feature(0x03)
                if locator:
                    locator.record_advertisement_rssi(peer.id_hex, peer.name, -70)

        await self.router.route(
            msg              = first_frag,
            complete_payload = complete_payload,
            source_address   = source_addr,
            peer_name_lookup = self._peer_name,
        )

    # ── Router hooks ──────────────────────────────────────────

    async def _handle_handshake_data(
        self, data: dict, src_id: bytes, address: str
    ):
        """Update peer registry when a HANDSHAKE arrives."""
        dev_id_hex = data.get("id", "")
        name       = data.get("name", src_id.hex()[:8])
        caps       = data.get("caps", 0)
        if dev_id_hex:
            self.peers.upsert(
                device_id   = bytes.fromhex(dev_id_hex),
                name        = name,
                capabilities= caps,
                ble_address = address,
            )
            self.store.upsert_peer(dev_id_hex, name, caps, address)
            log.info("Handshake registered: %s (%s)", name, dev_id_hex[:8])

    async def _send_ack(self, dst_id: bytes, seq: int, frag_id: int):
        """Send an ACK back to the source device."""
        body = json.dumps(
            {"seq": seq, "frag_id": frag_id},
            separators=(",", ":"),
        ).encode()
        await self.send_message(
            msg_type = MsgType.ACK,
            payload  = body,
            dst_id   = dst_id,
            flags    = int(MsgFlags.PRIORITY),
        )

    async def _send_pong(self, dst_id: bytes):
        await self.send_message(
            msg_type = MsgType.PONG,
            payload  = b"{}",
            dst_id   = dst_id,
            flags    = int(MsgFlags.PRIORITY),
        )

    # ── Discovery callback ────────────────────────────────────

    async def _on_peer_discovered(self, device, adv_data):
        """Lightweight hook — heavy work happens in _do_scan_cycle."""
        rssi = getattr(adv_data, "rssi", -127) or -127
        self.peers.update_rssi(device.address, rssi)

    # ── Helpers ───────────────────────────────────────────────

    def _collect_pending(self, peer_id_hex: str) -> List[bytes]:
        """
        Gather in-memory frames + DB-persisted frames for a peer,
        deduplicating any overlap.
        """
        mem = list(self._outbound.get(peer_id_hex, []))
        db  = [r.frame for r in self.store.get_pending_frames(peer_id_hex)]

        # Deduplicate (frames from DB may already be in mem after an enqueue)
        seen  = set(mem)
        extra = [f for f in db if f not in seen]
        return mem + extra

    def _peer_name(self, device_id: bytes) -> str:
        peer = self.peers.by_id(device_id)
        return peer.name if peer else device_id.hex()[:8]

    def _restore_peers(self):
        """Load previously known peers from SQLite into the in-memory registry."""
        for sp in self.store.get_all_peers():
            try:
                self.peers.upsert(
                    device_id   = bytes.fromhex(sp.device_id_hex),
                    name        = sp.name,
                    capabilities= sp.capabilities,
                    ble_address = sp.ble_address,
                    rssi        = -127,
                )
            except Exception:
                pass
        log.info("Restored %d peer(s) from store", len(self.peers))

    # ── Status / introspection ────────────────────────────────

    def status(self) -> dict:
        """Return a snapshot of current node state (for CLI / monitoring)."""
        return {
            "device"     : {"name": self.device.name, "id": self.device.id_hex},
            "peers"      : [
                {
                    "name"   : p.name,
                    "id"     : p.id_hex,
                    "rssi"   : p.rssi,
                    "fresh"  : p.is_fresh,
                    "address": p.ble_address,
                }
                for p in self.peers.all_peers()
            ],
            "queued"     : {k: len(v) for k, v in self._outbound.items() if v},
            "peripheral" : self.peripheral.is_running,
            "features"   : [
                type(f).__name__
                for f in self.router.get_all_features().values()
            ],
        }