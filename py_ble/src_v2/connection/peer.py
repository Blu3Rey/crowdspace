
"""
connection/peer.py — Peer represents one connected remote device.
 
Holds connection state, RSSI history for ranging, and per-feature
msg_id counters.  The ConnectionManager owns all Peer instances.
"""

import time

from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from ..protocol import Reassembler, Message
from ..addressing import short_addr_from_mac


class PeerRole(Enum):
    CENTRAL     = auto()    # we are the central, they are the peripheral
    PERIPHERAL  = auto()    # we are the peripheral, they are the central

class PeerState(Enum):
    CONNECTING      = auto()
    HANDSHAKING     = auto()
    CONNECTED       = auto()
    RECONNECTING    = auto()


@dataclass
class RSSISample:
    value:  int     # dBm
    ts:     float   # monotonic timestamp


class Peer:
    """
    Represents one connected remote BLE device.

    Owns:
        - connection metadata (address, name, role, state)
        - per-peer Reassembler (independent reassembly stream per peer)
        - RSSI sample ring buffer (for ranging)
        - per-feature outbound msg_id counters
        - connection statistics (bytes_tx, bytes_rx, msg counts)
    """

    RSSI_WINDOW = 20    # samples retained for smoothing

    def __init__(
        self,
        mac:    str,
        role:   PeerRole,
        on_msg: callable,   # Callable[[Peer, Message], None]
    ):
        self.mac        = mac.upper()
        self.role       = role
        self.state      = PeerState.CONNECTING
        self.name: Optional[str] = None
        self.connected_at = time.monotonic()

        # Short address derived from MAC - used in mesh routing headers
        self.short_addr = short_addr_from_mac(mac)

        # Packet reassembly - one per peer so concurrent msg_ids don't collide
        self._reassembler = Reassembler(lambda msg: on_msg(self, msg))

        # RSSI tracking
        self._rssi_samples: deque[RSSISample] = deque(maxlen=self.RSSI_WINDOW)
        self._last_rssi:    Optional[int] = None

        # Per-feature outbound msg_id counters (wraps at 256)
        self._msg_counters: dict[int, int] = {}

        # Stats
        self.bytes_tx = 0
        self.bytes_rx = 0
        self.msgs_tx = 0
        self.msgs_rx = 0
        self.ping_rtt_ms: Optional[float] = None
    
    # --- Reassembly -------------------------------------------------------

    def feed(self, raw: bytes) -> None:
        """Feed a raw BLE packet into this peer's reassembler."""
        self.bytes_rx += len(raw)
        self._reassembler.feed(raw)
    
    # --- RSSI -------------------------------------------------------------

    def record_rssi(self, rssi: int) -> None:
        sample = RSSISample(rssi, time.monotonic())
        self._rssi_samples.append(sample)
        self._last_rssi = rssi
    
    @property
    def rssi(self) -> Optional[int]:
        return self._last_rssi
    
    @property
    def rssi_mean(self) -> Optional[float]:
        if not self._rssi_samples:
            return None
        return sum(s.value for s in self._rssi_samples) / len(self._rssi_samples)
    
    def rssi_samples(self) -> list[int]:
        return [s.value for s in self._rssi_samples]
    
    # --- Msg-id counter -----------------------------------------------------

    def next_msg_id(self, feature_id: int) -> int:
        """Returns the next outbound msg_id for a given feature, then increment."""
        current = self._msg_counters.get(feature_id, 0)
        self._msg_counters[feature_id] = (current + 1) % 256
        return current
    
    # --- Stats --------------------------------------------------------------

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.connected_at
    
    def record_tx(self, n_bytes: int) -> None:
        self.bytes_tx += n_bytes
        self.msgs_tx += 1
    
    def record_rx(self) -> None:
        self.msgs_rx += 1
    
    # --- Repr ----------------------------------------------------------------

    def __repr__(self) -> str:
        name = f"'{self.name}'" if self.name else "unnamed"
        return (
            f"Peer({name} {self.mac} "
            f"0x{self.short_addr:04X} "
            f"{self.role.name} {self.state.name})"
        )