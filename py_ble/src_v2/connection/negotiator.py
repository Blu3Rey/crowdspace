
"""
connection/negotiator.py — BLE role negotiation.
 
Determines whether this device should become PERIPHERAL (advertise and wait)
or CENTRAL (scan, find, connect) without any external coordination.
 
Algorithm:
    1. Scan for SCAN_TIMEOUT seconds.
    2. Filter devices by SERVICE_UUID (via adv.service_uuids) OR by name
       (BlueZ sometimes omits UUIDs from the advertising payload).
    3. If one or more matches found → sort by RSSI, pick the strongest
       (closest) one, return ("central", mac).
    4. If nothing found → return ("peripheral", None).
 
Role overrides:
    --role peripheral   → skip scan, go straight to advertising
    --role central      → skip scan, connect directly to --target MAC
    --role auto (default) → run the scan algorithm above
 
The negotiator emits status lines through the optional `status_cb` so the
caller can route them to any UI without the negotiator knowing about rich/
terminal/logging directly.
"""

import asyncio
import logging

from __future__ import annotations
from typing import Callable, Optional
from bleak import BleakScanner

from ..constants import DEVICE_NAME, SCAN_TIMEOUT, SERVICE_UUID

log = logging.getLogger(__name__)

StatusCallback = Callable[[str, str], None] # (message, style)

class RoleNegotiator:
    """
    Stateless: call negotiate() each time you need a role decision.
    Can be called again after a disconnect to re-negotiate (e.g. if both
    sides restart and scan simultaneously).
    """

    def __init__(self, status_cb: Optional[StatusCallback] = None):
        self._status = status_cb or (lambda msg, style="dim": log.info(msg))
    
    async def negotiate(
        self,
        force_role: Optional[str] = None,   # "peripheral" | "central" | None
        target_mac: Optional[str] = None,   # required when force_role="central"
    ) -> tuple[str, Optional[str]]:
        """
        Returns:
            ("peripheral", None)    -> caller should advertise and wait
            ("central", "<mac>")    -> caller should connect to <mac>
        """

        if force_role == "peripheral":
            self._status("Role forced -> PERIPHERAL", "cyan")
            return "peripheral", None
        
        if force_role == "central":
            if not target_mac:
                raise ValueError("__role central requires --target <MAC>")
            self._status(f"Role forced -> CENTRAL, target {target_mac}", "cyan")
            return "central", target_mac
        
        # Auto-negotiate via scan
        return await self._scan_and_decide()
    
    async def _scan_and_decide(self) -> tuple[str, Optional[str]]:
        self._status(f"Scanning for peers ({SCAN_TIMEOUT:.0f}s)...", "yellow")
        
        # mac -> (BLEDevice, rssi)
        found: dict[str, tuple] = {}

        def on_device(device, adv):
            uuids = [u.lower() for u in (adv.service_uuids or [])]
            by_uuid = SERVICE_UUID.lower() in uuids
            by_name = (device.name or "").strip() == DEVICE_NAME
            if by_uuid or by_name:
                rssi = adv.rssi if adv.rssi is not None else -127
                found[device.address.upper()] = (device, rssi)
        
        async with BleakScanner(detection_callback=on_device):
            await asyncio.sleep(SCAN_TIMEOUT)
        
        if not found:
            self._status("No peer found -> PERIPHERAL", "cyan")
            return "peripheral", None
        
        # Prefer the peer with the strongest signal (i.e. physically closest)
        best_device, best_rssi = max(found.values(), key=lambda x: x[1])
        self._status(
            f"Found {len(found)} peer(s), strongest: "
            f"{best_device.name or best_device.address} "
            f"({best_rssi} dBm) -> CENTRAL",
            "green"
        )
        return "central", best_device.address