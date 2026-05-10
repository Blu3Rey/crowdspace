from .constants import MAX_MSG_ID, CHAT_SERV
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from rich.console import Console
from rich.text import Text
from rich.rule import Rule

@dataclass
class ChatRecord:
    """One entry in the in-memory message history."""
    ts:     datetime
    sender: str
    text:   str
    msg_id: int
    is_mine:int
    acked:  bool = False

class ChatUI:
    """
    Rich-formatted terminal chat display.

    Colour palette:
        Green   - your own messages
        Cyan    - peer messages
        Yellow  - system/status lines
        Red     - errors and disconnections
        Dim     - timestamps, metadata, prompts
    """

    def __init__(self, my_name: str):
        self.my_name    = my_name
        self.console    = Console(highlight=False, markup=True)
        self._history: list[ChatRecord] = []
        self._peer_name = "Peer"
        self._role      = "?"
        self._stats     = {"tx": 0, "rx": 0, "ping": 0, "pong": 0}
        self._session_start = time.monotonic()
        self._msg_counter   = 0
        self._ping_sent_at: Optional[float] = None
    
    # ID counter
    def next_msg_id(self) -> int:
        self._msg_counter = (self._msg_counter + 1) % MAX_MSG_ID
        return self._msg_counter
    
    # Formatted output
    def print_banner(self):
        self.console.print()
        self.console.rule("[bold blue]◆  BLE Messenger[/bold blue]")
        self.console.print(
            f"  Name    : [bold]{self.my_name}[/bold]\n"
            f"  Service : [dim]{CHAT_SERV}[/dim]"
        )
        self.console.print()
    
    def system(self, text: str, color: str = "yellow"):
        self.console.print(f"   [italic {color}]{text}[/italic {color}]")
    
    def error(self, text: str):
        self.console.print(f"  [bold red]✗  {text}[/bold red]")
    
    def print_connected(self, peer: str, role: str, addr: str):
        self._peer_name = peer
        self._role = role
        self.console.print()
        self.console.rule("[green]● Connected[/green]")
        self.console.print(
            f"  Role : [bold cyan]{role}[/bold cyan]\n"
            f"  Peer : [bold]{peer}[/bold]  [dim]({addr})[/dim]\n"
            f"  Type a message and press [bold]Enter[/bold]. "
            f"[dim]/help[/dim] for commands.\n"
        )
        self.console.rule()
        self.console.print()
    
    def print_disconnected(self, reason: str = "Connection lost"):
        self.console.print()
        self.console.rule(f"[red]✗  {reason}[/red]")
        self.console.print()
    
    def add_sent(self, text: str, msg_id: int) -> ChatRecord:
        rec = ChatRecord(datetime.now(), self.my_name, text, msg_id, is_mine=True)
        self._history.append(rec)
        self._stats["tx"] += 1

        ts   = rec.ts.strftime("%H:%M:%S")
        line = Text()
        line.append(f"[{ts}] ", style="dim")
        line.append("You", style="bold green")
        line.append(f"  {text}")
        line.append("  ✓", style="dim")
        self.console.print(line)
        return rec
    
    def add_received(self, sender: str, text: str, msg_id: int):
        rec = ChatRecord(datetime.now(), sender, text, msg_id, is_mine=False, acked=True)
        self._history.append(rec)
        self._stats["rx"] += 1

        ts   = rec.ts.strftime("%H:%M:%S")
        line = Text()
        line.append(f"[{ts}] ", style="dim")
        line.append(sender, style="bold cyan")
        line.append(f"  {text}")
        self.console.print(line)
    
    def mark_acked(self, msg_id: int):
        """Upgrade a sent message to double-tick (✓✓) status."""
        for rec in reversed(self._history):
            if rec.msg_id == msg_id and rec.is_mine and not rec.acked:
                rec.acked = True
                # In a linear terminal we can't re-render the past line, so we
                # print a subtle delivery receipt on a new line.
                self.console.print(f"  [dim]✓✓ delivered (msg #{msg_id})[/dim]")
                break
 
    def show_typing(self, on: bool):
        if on:
            self.console.print(f"  [dim italic]{self._peer_name} is typing…[/dim italic]")
 
    def record_ping_sent(self):
        self._ping_sent_at = time.monotonic()
        self._stats["ping"] += 1
 
    def record_pong_received(self):
        self._stats["pong"] += 1
        if self._ping_sent_at is not None:
            rtt = (time.monotonic() - self._ping_sent_at) * 1000
            self.system(f"Pong  {rtt:.1f} ms RTT", "dim")
            self._ping_sent_at = None
 
    def print_stats(self):
        elapsed = int(time.monotonic() - self._session_start)
        m, s    = divmod(elapsed, 60)
        h, m    = divmod(m, 60)
        st      = self._stats
        self.console.print(
            f"  [dim]TX [bold]{st['tx']}[/bold]  RX [bold]{st['rx']}[/bold]  "
            f"Ping/Pong [bold]{st['ping']}/{st['pong']}[/bold]  "
            f"Uptime [bold]{h:02d}:{m:02d}:{s:02d}[/bold][/dim]"
        )
 
    def print_summary(self):
        self.console.print()
        self.console.rule("[dim]Session ended[/dim]")
        self.print_stats()
        self.console.print()