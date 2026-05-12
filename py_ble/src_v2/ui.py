"""
ui.py — Terminal chat UI.

Design contract:
    ChatUI knows NOTHING about BLE, features, or connection management.
    It subscribes to EventBus events and calls back into app.py via
    the callbacks passed to input_loop().  This keeps the UI swappable:
    replace ChatUI with a WebUI or GUIApp by wiring different subscribers
    to the same EventBus — zero changes to any other layer.

EventBus events consumed:
    peer.named          peer was named via handshake (peer: Peer)
    peer.disconnected   link lost (peer: Peer, reason: str)
    chat.received       text message arrived (peer, sender, text, msg_id)
    chat.acked          delivery ACK received (peer, msg_id)
    chat.typing         peer typing state changed (peer, typing: bool)
    ranging.update      RSSI/distance update (peer, rssi, distance_m)
    group.message       group chat message (group_id, sender, text)
    ping.pong           RTT measured (peer, rtt_ms)
    mesh.data           mesh payload received (src_addr, payload, hops)
    app.shutdown        emitted by app.py just before stopping

Slash commands handled by app.py (passed as on_command callback):
    /dm <name> <text>   DM a specific peer by display name
    /peers              list connected peers and stats
    /groups             list joined groups
    /join <id>          join a group (hex or decimal group_id)
    /leave <id>         leave a group
    /group <id> <text>  send to group
    /ping               ping all peers
    /ranging            show distance estimates
    /routing            show mesh routing table (if mesh enabled)
    /mtu                show negotiated MTU per peer
    /stats              show session statistics
    /help               list commands
    /quit               graceful shutdown
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from datetime import datetime
from typing import Callable, Coroutine, Optional, TYPE_CHECKING

from rich.console import Console
from rich.text import Text

from .events import (
    CHAT_ACKED, CHAT_RECEIVED, EventBus,
    GROUP_MESSAGE, PEER_DISCONNECTED, RANGING_UPDATE,
)

if TYPE_CHECKING:
    from .connection.peer import Peer

# Type aliases for the callbacks passed to input_loop()
SendCallback    = Callable[[str, Optional[str]], Coroutine]  # (text, peer_name_or_None)
CommandCallback = Callable[[str], Coroutine]                 # ("/command …")


class ChatUI:
    """
    Rich-formatted terminal chat display.

    Instantiate once; call subscribe(bus) to wire event handlers.
    Call input_loop(on_send, on_command) to start reading stdin.
    """

    def __init__(self, my_name: str):
        self.my_name   = my_name
        self.console   = Console(highlight=False, markup=True)
        self._peers:   dict[str, str]  = {}    # mac → display name
        self._acked:   set[int]        = set() # msg_ids confirmed delivered
        self._start    = time.monotonic()
        self._stats    = defaultdict(int)      # "tx", "rx", "pong" …

    # ── Wiring ────────────────────────────────────────────────────────────────

    def subscribe(self, bus: EventBus) -> None:
        """Wire all event handlers to the bus.  Call once after construction."""
        bus.on("peer.named",       self._on_peer_named)
        bus.on(PEER_DISCONNECTED,  self._on_peer_disconnected)
        bus.on(CHAT_RECEIVED,      self._on_chat_received)
        bus.on(CHAT_ACKED,         self._on_chat_acked)
        bus.on("chat.typing",      self._on_typing)
        bus.on(RANGING_UPDATE,     self._on_ranging)
        bus.on(GROUP_MESSAGE,      self._on_group_message)
        bus.on("ping.pong",        self._on_pong)
        bus.on("mesh.data",        self._on_mesh_data)
        bus.on("app.shutdown",     self._on_shutdown)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_peer_named(self, peer: "Peer", **_) -> None:
        self._peers[peer.mac] = peer.name or peer.mac
        self.system(
            f"Connected to [bold]{peer.name}[/bold]  "
            f"[dim]{peer.mac}  0x{peer.short_addr:04X}[/dim]",
            "green",
        )

    def _on_peer_disconnected(self, peer: "Peer", reason: str = "", **_) -> None:
        name = self._peers.pop(peer.mac, peer.mac)
        self.system(f"{name} disconnected  [dim]{reason}[/dim]", "red")

    def _on_chat_received(self, peer: "Peer", sender: str, text: str, msg_id: int, **_) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = Text()
        line.append(f"[{ts}] ", style="dim")
        line.append(sender, style="bold cyan")
        line.append(f"  {text}")
        self.console.print(line)
        self._stats["rx"] += 1

    def _on_chat_acked(self, peer: "Peer", msg_id: int, **_) -> None:
        self._acked.add(msg_id)
        # Subtle inline delivery receipt — doesn't reprint the original line
        self.console.print(f"  [dim]✓✓ msg #{msg_id} delivered[/dim]")

    def _on_typing(self, peer: "Peer", typing: bool, **_) -> None:
        if typing:
            name = self._peers.get(peer.mac, peer.mac)
            self.console.print(f"  [dim italic]{name} is typing…[/dim italic]")

    def _on_ranging(self, peer: "Peer", rssi: int, distance_m: float, **kw) -> None:
        name = self._peers.get(peer.mac, peer.mac)
        smooth = kw.get("rssi_smooth", rssi)
        self.console.print(
            f"  [dim]📡 {name}  RSSI {rssi} dBm  "
            f"(smooth {smooth:.1f})  ≈ {distance_m:.2f} m[/dim]"
        )

    def _on_group_message(self, group_id: int, sender: str, text: str, **_) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        gid = f"0x{group_id:04X}"
        line = Text()
        line.append(f"[{ts}] ", style="dim")
        line.append(f"[{gid}] ", style="bold yellow")
        line.append(sender, style="bold cyan")
        line.append(f"  {text}")
        self.console.print(line)
        self._stats["rx_group"] += 1

    def _on_pong(self, peer: "Peer", rtt_ms: float, **_) -> None:
        name = self._peers.get(peer.mac, peer.mac)
        self.system(f"Pong from {name}  {rtt_ms:.1f} ms RTT", "dim")
        self._stats["pong"] += 1

    def _on_mesh_data(self, src_addr: int, payload: bytes, hops: int, **kw) -> None:
        sender = kw.get("sender", f"0x{src_addr:04X}")
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            text = payload.decode("utf-8")
        except Exception:
            text = payload.hex()
        self.console.print(
            f"  [dim][{ts}] 🕸  mesh/{sender} ({hops} hop{'s' if hops!=1 else ''})  {text}[/dim]"
        )

    def _on_shutdown(self, **_) -> None:
        self.print_summary()

    # ── Formatted output helpers ───────────────────────────────────────────────

    def print_banner(self) -> None:
        self.console.print()
        self.console.rule("[bold blue]◆  BLE Stack[/bold blue]")
        self.console.print(f"  Name : [bold]{self.my_name}[/bold]")
        self.console.print()

    def system(self, msg: str, style: str = "yellow") -> None:
        self.console.print(f"  [italic {style}]{msg}[/italic {style}]")

    def error(self, msg: str) -> None:
        self.console.print(f"  [bold red]✗  {msg}[/bold red]")

    def print_connected_header(self, role: str) -> None:
        self.console.print()
        self.console.rule(f"[green]● {role}[/green]")
        self.console.print(
            f"  Type a message and [bold]Enter[/bold] to send to all peers.  "
            f"[dim]/help[/dim] for commands.\n"
        )
        self.console.rule()
        self.console.print()

    def print_sent(self, text: str, msg_id: int, target: Optional[str] = None) -> None:
        ts  = datetime.now().strftime("%H:%M:%S")
        to  = f" → {target}" if target else ""
        line = Text()
        line.append(f"[{ts}] ", style="dim")
        line.append(f"You{to}", style="bold green")
        line.append(f"  {text}")
        line.append("  ✓", style="dim")
        self.console.print(line)
        self._stats["tx"] += 1

    def print_summary(self) -> None:
        elapsed = int(time.monotonic() - self._start)
        m, s    = divmod(elapsed, 60)
        h, m    = divmod(m, 60)
        self.console.print()
        self.console.rule("[dim]Session ended[/dim]")
        self.console.print(
            f"  TX [bold]{self._stats['tx']}[/bold]  "
            f"RX [bold]{self._stats['rx']}[/bold]  "
            f"Groups [bold]{self._stats['rx_group']}[/bold]  "
            f"Pongs [bold]{self._stats['pong']}[/bold]  "
            f"Uptime [bold]{h:02d}:{m:02d}:{s:02d}[/bold]"
        )
        self.console.print()

    # ── Input loop ────────────────────────────────────────────────────────────

    async def input_loop(
        self,
        on_send:    SendCallback,
        on_command: CommandCallback,
    ) -> None:
        """
        Read stdin line by line in an executor thread (non-blocking).
        Plain text → on_send(text, None).
        /commands  → on_command("/cmd …").
        Exits on /quit, EOF, or KeyboardInterrupt.
        """
        loop = asyncio.get_running_loop()

        while True:
            try:
                raw = await loop.run_in_executor(None, lambda: input("> "))
            except (EOFError, KeyboardInterrupt):
                break

            line = raw.strip()
            if not line:
                continue

            if line.lower() in {"/quit", "/exit", "/q"}:
                break

            if line.startswith("/"):
                await on_command(line)
            else:
                await on_send(line, None)