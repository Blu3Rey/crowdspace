"""
config.py — Mesh-wide configuration dataclass.

Power profiles adjust BLE scan duty-cycle and heartbeat cadence so the same
codebase can run on a Raspberry Pi 4 and a coin-cell ESP32 alike.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MeshConfig:
    """Centralised configuration for a single mesh node.

    Attributes
    ----------
    node_name : str
        Human-readable name advertised over BLE.
    node_id : bytes
        Unique 16-byte node identifier (defaults to a fresh UUID4).
    scan_interval : float
        Seconds between BLE scan bursts (overridden by *power_profile*).
    scan_duration : float
        Duration of each scan burst in seconds.
    max_connections : int
        Maximum simultaneous BLE central connections (stack limit ≈ 7).
    connection_timeout : float
        Seconds before a connection attempt is abandoned.
    heartbeat_interval : float
        Seconds between HEARTBEAT/DISCOVERY broadcasts.
    route_timeout : float
        Seconds until a routing-table entry expires.
    max_ttl : int
        Maximum hop count for forwarded packets.
    dedup_window : int
        Number of (src_id, seq_num) pairs cached for duplicate detection.
    ack_timeout : float
        Seconds to wait for an ACK before retransmission.
    max_retries : int
        Retransmission attempts for reliable (ACK_REQ) packets.
    mtu : int
        Negotiated BLE ATT MTU.  244 bytes is the BLE 4.2 DLE maximum.
    enable_encryption : bool
        Wrap every packet payload in AES-256-GCM.  Requires *psk*.
    psk : Optional[bytes]
        32-byte pre-shared key.  Auto-generated when *enable_encryption*
        is True and *psk* is None (you must distribute the generated key).
    power_profile : str
        ``"low_power"`` | ``"balanced"`` | ``"high_performance"``
    """

    node_name: str = "MeshNode"
    node_id: bytes = field(default_factory=lambda: uuid.uuid4().bytes)

    # ── BLE scanning ──────────────────────────────────────────────────────────
    scan_interval: float = 8.0
    scan_duration: float = 4.0

    # ── Connections ───────────────────────────────────────────────────────────
    max_connections: int = 7
    connection_timeout: float = 10.0

    # ── Protocol ─────────────────────────────────────────────────────────────
    heartbeat_interval: float = 30.0
    route_timeout: float = 120.0
    max_ttl: int = 7
    dedup_window: int = 512
    ack_timeout: float = 5.0
    max_retries: int = 3

    # ── Fragmentation ─────────────────────────────────────────────────────────
    mtu: int = 244

    # ── Security ──────────────────────────────────────────────────────────────
    enable_encryption: bool = False
    psk: Optional[bytes] = None

    # ── Power ─────────────────────────────────────────────────────────────────
    power_profile: str = "balanced"
    # Use passive BLE scanning (no scan-request packets).  Cuts radio-on power
    # draw by ~60–70 % at the cost of slightly reduced advertisement detection
    # on some peripherals.  Recommended for battery-constrained nodes.
    passive_scan: bool = False

    # ── Internal ─────────────────────────────────────────────────────────────
    _PROFILES: dict = field(default_factory=lambda: {
        "low_power":        {"scan_interval": 20.0, "scan_duration": 2.0, "heartbeat_interval": 60.0},
        "balanced":         {"scan_interval": 8.0,  "scan_duration": 4.0, "heartbeat_interval": 30.0},
        "high_performance": {"scan_interval": 3.0,  "scan_duration": 8.0, "heartbeat_interval": 10.0},
    }, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.node_id) != 16:
            raise ValueError("node_id must be exactly 16 bytes")
        if self.enable_encryption and self.psk is None:
            self.psk = os.urandom(32)
        if self.psk is not None and len(self.psk) not in (16, 24, 32):
            raise ValueError("psk must be 16, 24, or 32 bytes for AES-128/192/256-GCM")
        # Apply power-profile overrides only if the caller left defaults
        profile = self._PROFILES.get(self.power_profile, {})
        for attr, val in profile.items():
            # Only override when the user hasn't customised the field
            if getattr(self, attr) == MeshConfig.__dataclass_fields__[attr].default:
                object.__setattr__(self, attr, val)

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def max_payload_size(self) -> int:
        """Max plain-text bytes per fragment (MTU minus headers)."""
        from .core.protocol import HEADER_SIZE, FRAG_HEADER_SIZE
        return self.mtu - HEADER_SIZE - FRAG_HEADER_SIZE
