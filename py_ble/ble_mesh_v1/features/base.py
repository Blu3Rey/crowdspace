"""
features/base.py — Abstract base for pluggable mesh features.

Every feature (messaging, group chat, locating, …) extends :class:`Feature`
and registers itself with :class:`~ble_mesh.core.node.MeshNode` via
``node.register_feature(feature)``.

The node dispatches each incoming :class:`~ble_mesh.core.packet.Packet` to
every registered feature whose :attr:`handled_types` set includes the
packet's ``msg_type``.  Features that want to *handle all* types can include
a wildcard sentinel (``-1``) in the set.

Features receive a back-reference to the node, which gives them access to:

* ``node.send()`` — to originate or reply with packets.
* ``node.neighbors`` — the neighbour table.
* ``node.router`` — the routing table.
* ``node.config`` — the global configuration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, FrozenSet, Optional, Set

from ..core.packet import Packet

if TYPE_CHECKING:
    from ..core.node import MeshNode


class Feature(ABC):
    """Pluggable mesh feature.

    Subclass this, implement :meth:`handle`, and register with
    ``node.register_feature(instance)``.

    Attributes
    ----------
    handled_types : frozenset[int]
        Set of :class:`~ble_mesh.core.protocol.MsgType` constants this
        feature wants to receive.  Use ``frozenset({-1})`` for all types.
    name : str
        Human-readable name used in log messages.
    """

    handled_types: FrozenSet[int] = frozenset()
    name: str = "unnamed-feature"

    def __init__(self) -> None:
        self._node: Optional["MeshNode"] = None

    def attach(self, node: "MeshNode") -> None:
        """Called once by the node during :meth:`register_feature`."""
        self._node = node
        self.on_attach(node)

    # ── Override points ───────────────────────────────────────────────────────

    def on_attach(self, node: "MeshNode") -> None:  # noqa: B027
        """Called immediately after the feature is attached to a node.

        Override for one-time initialisation that requires node context
        (e.g. scheduling background tasks).
        """

    @abstractmethod
    async def handle(self, packet: Packet) -> None:
        """Process an inbound packet.

        This coroutine is awaited by the node's dispatch loop.  Implementations
        should return promptly; kick off long-running work with
        ``asyncio.create_task()``.
        """

    async def on_start(self) -> None:  # noqa: B027
        """Called when the node starts.  Override to launch background tasks."""

    async def on_stop(self) -> None:   # noqa: B027
        """Called when the node stops.  Override for cleanup."""

    # ── Convenience helpers ───────────────────────────────────────────────────

    @property
    def node(self) -> "MeshNode":
        if self._node is None:
            raise RuntimeError(f"Feature '{self.name}' has not been attached to a node.")
        return self._node

    async def send(self, msg_type: int, payload: bytes,
                   dst_id: bytes | None = None, flags: int = 0) -> bool:
        """Shortcut for ``self.node.send(…)``."""
        from ..core.protocol import BROADCAST_ADDR
        return await self.node.send(
            msg_type, payload,
            dst_id=dst_id or BROADCAST_ADDR,
            flags=flags,
        )

    def __repr__(self) -> str:
        return f"<Feature:{self.name}>"