"""
transport/central.py — BLE Central role: scanning and ephemeral GATT sessions.

This module handles:
  1. Scanning for BLE peripherals that advertise SERVICE_UUID.
  2. Reading CHAR_INFO_UUID on first encounter to learn a peer's identity.
  3. Connecting to a peer for one "session": write outbound frames → subscribe
     to notifications → receive any frames the peer has queued for us →
     disconnect.

"Ephemeral" means connections are opened, used, and torn down — not kept alive
permanently.  This conserves power and BLE radio time, fitting the duty-cycled
model expected of battery-powered IoT nodes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Dict, List, Optional, Set, Tuple

from bleak import BleakClient, BleakScanner                 # type: ignore[import]
from bleak.backends.device import BLEDevice                 # type: ignore[import]
from bleak.backends.scanner import AdvertisementData        # type: ignore[import]

from ..constants import (
    SERVICE_UUID,
    CHAR_WRITE_UUID, CHAR_NOTIFY_UUID, CHAR_INFO_UUID,
    CONNECT_TIMEOUT, SESSION_WINDOW_S,
)

log = logging.getLogger(__name__)

# Callbacks
DiscoveryCallback = Callable[[BLEDevice, AdvertisementData], Coroutine]
DataCallback      = Callable[[bytes, str], Coroutine]


@dataclass
class SessionResult:
    """Summary of one completed (or failed) ephemeral session."""
    address           : str
    success           : bool
    frames_sent       : int  = 0
    notifications_rcvd: int  = 0
    error             : Optional[str] = None


class BLECentral:
    """
    Manages outbound BLE scanning and ephemeral GATT sessions.

    Usage
    -----
    central = BLECentral(local_device_id=device.device_id)
    central.set_discovery_callback(my_async_handler)
    central.set_data_callback(my_async_data_handler)

    found = await central.scan(duration=5.0)
    info  = await central.read_peer_info(found[0][0])
    result = await central.open_session(found[0][0], outbound_frames=[...])
    """

    def __init__(self, local_device_id: bytes):
        self._local_id       : bytes                    = local_device_id
        self._discovery_cb   : Optional[DiscoveryCallback] = None
        self._data_cb        : Optional[DataCallback]   = None
        self._connecting     : Set[str]                 = set()
        self._loop           : Optional[asyncio.AbstractEventLoop] = None

    # ── Registration ─────────────────────────────────────────

    def set_discovery_callback(self, cb: DiscoveryCallback):
        """Async callback invoked for every BLE-P2P device seen during a scan."""
        self._discovery_cb = cb

    def set_data_callback(self, cb: DataCallback):
        """Async callback invoked for every notification received in a session."""
        self._data_cb = cb

    # ── Scanning ─────────────────────────────────────────────

    async def scan(self, duration: float = 5.0) -> List[Tuple[BLEDevice, AdvertisementData]]:
        """
        Run an active BLE scan for *duration* seconds.

        Only devices advertising SERVICE_UUID are returned / dispatched.
        Returns a list of (BLEDevice, AdvertisementData) tuples.
        """
        self._loop = asyncio.get_running_loop()
        discovered: List[Tuple[BLEDevice, AdvertisementData]] = []
        seen_addrs: Set[str] = set()

        def _detection(device: BLEDevice, adv: AdvertisementData):
            uuids = {str(u).lower() for u in (adv.service_uuids or [])}
            if SERVICE_UUID.lower() not in uuids:
                return
            if device.address in seen_addrs:
                return
            seen_addrs.add(device.address)
            discovered.append((device, adv))

            if self._discovery_cb and self._loop:
                asyncio.run_coroutine_threadsafe(
                    self._discovery_cb(device, adv), self._loop
                )

        try:
            async with BleakScanner(detection_callback=_detection) as _scanner:
                await asyncio.sleep(duration)
        except Exception as exc:
            log.error("Scan error: %s", exc)

        log.debug("Scan complete — found %d BLE-P2P device(s)", len(discovered))
        return discovered

    # ── Read device info ──────────────────────────────────────

    async def read_peer_info(self, device: BLEDevice) -> Optional[bytes]:
        """
        Connect briefly to read CHAR_INFO_UUID, then disconnect.
        Returns the raw bytes (JSON) or None on failure.
        """
        try:
            async with BleakClient(device, timeout=CONNECT_TIMEOUT) as client:
                raw = await client.read_gatt_char(CHAR_INFO_UUID)
                log.debug("Read info from %s: %d bytes", device.address, len(raw))
                return bytes(raw)
        except Exception as exc:
            log.warning("read_peer_info(%s) failed: %s", device.address, exc)
            return None

    # ── Ephemeral Session ─────────────────────────────────────

    async def open_session(
        self,
        device          : BLEDevice,
        outbound_frames : List[bytes],
        inter_frame_delay: float = 0.025,
    ) -> SessionResult:
        """
        Open an ephemeral GATT session with *device*.

        Steps
        -----
        1. Connect (BleakClient context manager).
        2. Subscribe to CHAR_NOTIFY_UUID.
        3. Write each frame in *outbound_frames* to CHAR_WRITE_UUID.
        4. Wait SESSION_WINDOW_S for any incoming notifications.
        5. Unsubscribe, disconnect (context manager handles teardown).

        Returns a SessionResult describing what happened.
        """
        addr = device.address

        if addr in self._connecting:
            return SessionResult(address=addr, success=False, error="already_in_progress")
        self._connecting.add(addr)
        self._loop = asyncio.get_running_loop()

        result = SessionResult(address=addr, success=False)

        try:
            async with BleakClient(device, timeout=CONNECT_TIMEOUT) as client:
                log.info("Session opened with %s", addr)

                # Subscribe to notifications from the peer
                notif_count = 0

                def _on_notification(_: int, data: bytearray):
                    nonlocal notif_count
                    notif_count += 1
                    if self._data_cb and self._loop:
                        asyncio.run_coroutine_threadsafe(
                            self._data_cb(bytes(data), addr), self._loop
                        )

                await client.start_notify(CHAR_NOTIFY_UUID, _on_notification)

                # Write outbound frames
                sent = 0
                for frame in outbound_frames:
                    try:
                        await client.write_gatt_char(
                            CHAR_WRITE_UUID, bytearray(frame), response=False
                        )
                        sent += 1
                        if inter_frame_delay > 0:
                            await asyncio.sleep(inter_frame_delay)
                    except Exception as exc:
                        log.warning("Write to %s failed at frame %d: %s", addr, sent, exc)
                        break

                # Wait for peer's notifications
                await asyncio.sleep(SESSION_WINDOW_S)

                try:
                    await client.stop_notify(CHAR_NOTIFY_UUID)
                except Exception:
                    pass

                result.frames_sent        = sent
                result.notifications_rcvd = notif_count
                result.success            = True
                log.info(
                    "Session with %s complete: sent=%d notif=%d",
                    addr, sent, notif_count,
                )

        except asyncio.TimeoutError:
            result.error = "connect_timeout"
            log.warning("Session with %s timed out", addr)
        except Exception as exc:
            result.error = str(exc)
            log.warning("Session with %s failed: %s", addr, exc)
        finally:
            self._connecting.discard(addr)

        return result

    @property
    def active_connections(self) -> Set[str]:
        """Addresses currently in the connecting / session phase."""
        return set(self._connecting)