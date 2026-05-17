"""
features/base.py — Abstract base class for all feature modules.

To add a new feature
--------------------
1. Subclass Feature.
2. Set the class-level feature_id (pick from FeatureID or use CUSTOM_BASE+n).
3. Implement handle_message().
4. Register with node.register_feature(MyFeature(node)).

Wire format for FEATURE messages
---------------------------------
Payload byte 0 = feature_id (1 byte)
Payload bytes 1… = feature-specific body

The encode_payload() / decode_payload() helpers apply this framing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Tuple


class Feature(ABC):
    """
    Abstract base for all BLE-P2P feature modules.

    Attributes
    ----------
    feature_id : int
        Must be set as a class variable on every concrete subclass.
        Use a value from FeatureID or FeatureID.CUSTOM_BASE + n.
    """

    feature_id: int  # subclasses MUST override this

    def __init__(self, node):
        """
        Parameters
        ----------
        node : BLEMeshNode
            Back-reference to the owning node.  Features use this to call
            node.send_message() and access node.device / node.peers.
        """
        self.node = node

    # ── Must override ─────────────────────────────────────────

    @abstractmethod
    async def handle_message(
        self,
        body     : bytes,
        src_id   : bytes,
        src_name : str,
    ) -> None:
        """
        Called by the Router when a FEATURE message arrives with our feature_id.

        Parameters
        ----------
        body     : feature-specific payload (feature_id prefix already stripped)
        src_id   : 8-byte sender device ID
        src_name : display name of the sender (from PeerRegistry, may be hex fallback)
        """

    # ── Payload helpers ───────────────────────────────────────

    def encode_payload(self, body: bytes) -> bytes:
        """
        Prepend feature_id byte to *body*.  The result is the full FEATURE
        message payload handed to node.send_message().
        """
        return bytes([self.feature_id & 0xFF]) + body

    @staticmethod
    def decode_payload(payload: bytes) -> Tuple[Optional[int], bytes]:
        """
        Split a raw FEATURE payload into (feature_id, body).
        Returns (None, b'') if payload is empty.
        """
        if not payload:
            return None, b""
        return payload[0], payload[1:]

    # ── Optional lifecycle hooks ──────────────────────────────

    async def on_start(self) -> None:
        """Called once when the node starts.  Override for init work."""

    async def on_stop(self) -> None:
        """Called once when the node shuts down.  Override for cleanup."""

    async def on_peer_connected(self, peer_id: bytes, peer_name: str) -> None:
        """Called after every successful session with a peer."""

    # ── Repr ──────────────────────────────────────────────────

    def __repr__(self) -> str:
        return f"<{type(self).__name__} feature_id={self.feature_id:#x}>"