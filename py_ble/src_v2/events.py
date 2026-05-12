"""
events.py — Async pub/sub event bus.
 
Every layer publishes events by name; any other layer subscribes without
needing a direct reference to the publisher.  This is the primary mechanism
that keeps features, UI, and connection management loosely coupled.
 
Standard event names (by convention, use the constants below):
 
    peer.connected      (peer: Peer)
    peer.disconnected   (peer: Peer, reason: str)
    peer.rssi           (peer: Peer, rssi: int)
    message.received    (peer: Peer, msg: Message)
    chat.received       (peer: Peer, sender: str, text: str, msg_id: int)
    chat.acked          (peer: Peer, msg_id: int)
    ranging.update      (peer: Peer, rssi: int, distance_m: float)
    mesh.forwarded      (src: int, dst: int, hops: int)
    group.message       (group_id: int, sender: str, text: str)
    app.shutdown        ()
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Any, Callable, Coroutine

log = logging.getLogger(__name__)

# --- Standard event name constants -----------------------------------------
PEER_CONNECTED      = "peer.connected"
PEER_DISCONNECTED   = "peer.disconnected"
PEER_RSSI           = "peer.rssi"
MSG_RECEIVED        = "message.received"
CHAT_RECEIVED       = "chat.received"
CHAT_ACKED          = "chat.acked"
RANGING_UPDATE      = "ranging.update"
MESH_FORWARDED      = "mesh.forwarded"
GROUP_MESSAGE       = "group.message"
APP_SHUTDOWN        = "app.shutdown"

Handler = Callable[..., Any]    # sync or async, receives **kwargs

class EventBus:
    """
    Lightweight async pub/sub bus.

    Handlers may be plain functions or coroutines - the bus awaits coroutines
    automatically. Multiple handlers per event are called in subscription order.
    Exceptions in handlers are logged but never propagate to the publisher.

    Usage:
        bus = EventBus()

        # Subscribe
        bus.on("chat.received", my_handler)

        # Publish (from async context)
        await bus.emit("chat.received", peer=p, sender="Alice", text="hi", msg_id=1)
    """

    def __init__(self):
        self._handlers: dict[str, list[Handler]] = defaultdict(list)
    
    # --- Subscription ---------------------------------------------------------

    def on(self, event: str, handler: Handler) -> None:
        """Register `handler` to be called whenever `event` is emitted."""
        self._handlers[event].append(handler)
    
    def off(self, event: str, handler: Handler) -> None:
        """Remove a previously registered handler."""
        try:
            self._handlers[event].remove(handler)
        except ValueError:
            pass
    
    def once(self, event: str, handler: Handler) -> None:
        """Register a handler that fires exactly once then auto-removes itself."""
        async def _wrapper(**kwargs):
            self.off(event, _wrapper)
            if asyncio.iscoroutinefunction(handler):
                await handler(**kwargs)
            else:
                handler(**kwargs)
        self.on(event, _wrapper)

    # --- Publication -----------------------------------------------------------

    async def emit(self, event: str, **kwargs) -> None:
        """
        Fire all handlers subscribed to `event`.
        Handlers are called with `**kwargs` as keyword arguments.
        """
        for handler in list(self._handlers.get(event, [])):
            try:
                result = handler(**kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception(f"EventBus handler error for event '{event}'")
    
    def emit_nowait(self, event: str, **kwargs) -> None:
        """
        Schedule emission without awaiting. Safe to call from non-async
        contexts (e.g. BLE stack callbacks).
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.emit(event, **kwargs))
        except RuntimeError:
            pass    # no running loop - drop the event
    
    # --- Introspection ---------------------------------------------------------

    def subscribers(self, event: str) -> int:
        return len(self._handlers.get(event, []))
    
    def all_events(self) -> list[str]:
        return list(self._handlers.keys())