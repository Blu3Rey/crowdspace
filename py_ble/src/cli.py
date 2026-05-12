"""
cli.py
======
Interactive command-line interface for the BLE Mesh Network node.

Run:
    python -m ble_mesh_network.cli --name Alice

Commands:
    peers               List all known peers
    msg <addr> <text>   Send a direct message
    bc <text>           Broadcast a message
    group create <name> Create a chat room
    group join <id>     Join a chat room
    group msg <id> <t>  Send a group message
    locate <addr>       Locate a peer by RSSI
    ping <addr>         Measure RTT to peer
    status              Show node status
    routes              Show routing table
    help                Show command list
    quit                Shutdown and exit
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import shlex
import sys
import time
from typing import Optional

from .mesh_node import MeshNode, create_node
from .features.messaging import Message
from .features.group_chat import GroupMessage
from .core.crypto import generate_network_key


# ── Terminal colours ──────────────────────────────────────────────────────────

class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[31m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    CYAN   = "\033[36m"
    BLUE   = "\033[34m"
    GREY   = "\033[90m"

def _c(color: str, text: str) -> str:
    return f"{color}{text}{C.RESET}"


# ── CLI ───────────────────────────────────────────────────────────────────────

class MeshCLI:
    PROMPT = _c(C.BOLD + C.CYAN, "mesh> ")

    def __init__(self, node: MeshNode):
        self._node = node
        self._running = True

    # ── Main Loop ─────────────────────────────────────────────────────────────

    async def run(self):
        self._register_callbacks()
        print(_c(C.GREEN + C.BOLD, f"\n✦ BLE Mesh Node '{self._node.name}' online"))
        print(_c(C.GREY,  f"  Address : {self._node.addr_str}"))
        print(_c(C.GREY,  f"  Type 'help' for commands.\n"))

        loop = asyncio.get_event_loop()
        while self._running:
            try:
                line = await loop.run_in_executor(None, self._input)
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            await self._dispatch(line)

    def _input(self) -> str:
        try:
            return input(self.PROMPT)
        except EOFError:
            return "quit"

    async def _dispatch(self, line: str):
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            print(_c(C.RED, f"Parse error: {e}"))
            return

        if not tokens:
            return

        cmd  = tokens[0].lower()
        args = tokens[1:]

        handlers = {
            "help":    self._cmd_help,
            "status":  self._cmd_status,
            "peers":   self._cmd_peers,
            "routes":  self._cmd_routes,
            "msg":     self._cmd_msg,
            "bc":      self._cmd_broadcast,
            "group":   self._cmd_group,
            "locate":  self._cmd_locate,
            "ping":    self._cmd_ping,
            "history": self._cmd_history,
            "quit":    self._cmd_quit,
            "exit":    self._cmd_quit,
        }

        handler = handlers.get(cmd)
        if handler is None:
            print(_c(C.RED, f"Unknown command: {cmd}  (type 'help')"))
            return
        try:
            await handler(args)
        except Exception as e:
            print(_c(C.RED, f"Error: {e}"))
            logging.debug("Command error", exc_info=True)

    # ── Commands ──────────────────────────────────────────────────────────────

    async def _cmd_help(self, _):
        print(f"""
{_c(C.BOLD, "BLE Mesh Network – Commands")}

  {_c(C.CYAN, "status")}                       Node info and statistics
  {_c(C.CYAN, "peers")}                        List all known peers
  {_c(C.CYAN, "routes")}                       Show routing table

  {_c(C.CYAN, "msg")} <addr> <text>            Send a direct message
  {_c(C.CYAN, "bc")} <text>                    Broadcast to entire network
  {_c(C.CYAN, "history")} [addr]               Show message history

  {_c(C.CYAN, "group create")} <name>          Create a chat room
  {_c(C.CYAN, "group join")} <id>              Join a chat room by ID
  {_c(C.CYAN, "group leave")} <id>             Leave a chat room
  {_c(C.CYAN, "group msg")} <id> <text>        Send a group message
  {_c(C.CYAN, "group list")}                   List joined rooms
  {_c(C.CYAN, "group invite")} <id> <addr>     Invite a peer to a room

  {_c(C.CYAN, "locate")} <addr>                Estimate peer's position
  {_c(C.CYAN, "visible")}                      List nearby nodes with RSSI
  {_c(C.CYAN, "ping")} <addr>                  Measure RTT to a peer

  {_c(C.CYAN, "quit")}                         Shutdown and exit
""")

    async def _cmd_status(self, _):
        s = self._node.status()
        print(f"""
  {_c(C.BOLD, "Node Status")}
  Name      : {s['name']}
  Address   : {s['addr']}
  Peers     : {s['peers']}
  Neighbors : {s['neighbors']}
  Groups    : {s['groups']}
  Features  : {', '.join(s['features'])}
""")

    async def _cmd_peers(self, _):
        peers = self._node.peers()
        if not peers:
            print(_c(C.YELLOW, "  No peers discovered yet."))
            return
        print(f"\n  {'ADDRESS':<22} {'NAME':<16} {'HOPS':<6} {'RSSI':<8} {'DIST':<10} {'RTT'}")
        print("  " + "─" * 70)
        for p in sorted(peers, key=lambda x: x["hop_distance"]):
            dist = f"{p['distance_m']}m" if p["distance_m"] else "?"
            rtt  = f"{p['rtt_ms']:.1f}ms" if p["rtt_ms"] < 1e9 else "?"
            alive = _c(C.GREEN, "●") if p["is_alive"] else _c(C.RED, "○")
            print(f"  {alive} {p['addr']:<20} {p['name']:<16} {p['hop_distance']:<6} "
                  f"{p['rssi_avg']:<8.1f} {dist:<10} {rtt}")
        print()

    async def _cmd_routes(self, _):
        print("\n" + self._node.routing_table.summary() + "\n")

    async def _cmd_msg(self, args: list):
        if len(args) < 2:
            print(_c(C.RED, "Usage: msg <addr> <text>"))
            return
        addr = self._parse_addr(args[0])
        text = " ".join(args[1:])
        msg  = await self._node.messaging.send_text(addr, text, reliable=True)
        print(_c(C.GREY, f"  ✓ Sent [{msg.id[:8]}]"))

    async def _cmd_broadcast(self, args: list):
        if not args:
            print(_c(C.RED, "Usage: bc <text>"))
            return
        text = " ".join(args)
        msg  = await self._node.messaging.send_broadcast(text)
        print(_c(C.GREY, f"  ✓ Broadcast [{msg.id[:8]}]"))

    async def _cmd_history(self, args: list):
        if not args:
            convos = self._node.messaging.all_conversations()
            if not convos:
                print(_c(C.YELLOW, "  No messages yet."))
                return
            for peer, msgs in convos.items():
                peer_str = ":".join(f"{b:02X}" for b in peer)
                print(f"\n  {_c(C.BOLD, peer_str)}  ({len(msgs)} messages)")
                for m in list(msgs)[-5:]:
                    direction = "→" if m.src_addr == self._node.local_addr else "←"
                    ts        = time.strftime("%H:%M:%S", time.localtime(m.timestamp))
                    print(f"    [{ts}] {direction} {m.body}")
        else:
            addr  = self._parse_addr(args[0])
            msgs  = self._node.messaging.conversation(addr)
            astr  = ":".join(f"{b:02X}" for b in addr)
            print(f"\n  Conversation with {_c(C.BOLD, astr)}")
            for m in msgs:
                direction = "→" if m.src_addr == self._node.local_addr else "←"
                ts        = time.strftime("%H:%M:%S", time.localtime(m.timestamp))
                print(f"  [{ts}] {direction} {m.body}")
        print()

    async def _cmd_group(self, args: list):
        if not args:
            print(_c(C.RED, "Usage: group <create|join|leave|msg|list|invite> ..."))
            return
        sub = args[0].lower()
        rest = args[1:]

        gc = self._node.group_chat

        if sub == "create":
            name = " ".join(rest) if rest else "unnamed"
            gid  = await gc.create_room(name)
            print(_c(C.GREEN, f"  ✓ Room '{name}' created with ID {gid}"))

        elif sub == "join":
            if not rest:
                print(_c(C.RED, "Usage: group join <id>"))
                return
            gid = int(rest[0])
            await gc.join(gid)
            print(_c(C.GREEN, f"  ✓ Joined group {gid}"))

        elif sub == "leave":
            if not rest:
                print(_c(C.RED, "Usage: group leave <id>"))
                return
            gid = int(rest[0])
            await gc.leave(gid)
            print(_c(C.YELLOW, f"  Left group {gid}"))

        elif sub == "msg":
            if len(rest) < 2:
                print(_c(C.RED, "Usage: group msg <id> <text>"))
                return
            gid  = int(rest[0])
            text = " ".join(rest[1:])
            gm   = await gc.send_message(gid, text)
            if gm:
                print(_c(C.GREY, f"  ✓ Group message sent [{gm.id[:8]}]"))
            else:
                print(_c(C.RED, "  Not a member of that group."))

        elif sub == "list":
            groups = gc.my_groups()
            if not groups:
                print(_c(C.YELLOW, "  Not in any groups."))
                return
            for g in groups:
                print(f"  [{g['id']}] {_c(C.BOLD, g['name'])}  members={len(g['members'])}")

        elif sub == "invite":
            if len(rest) < 2:
                print(_c(C.RED, "Usage: group invite <id> <addr>"))
                return
            gid  = int(rest[0])
            addr = self._parse_addr(rest[1])
            await gc.invite(gid, addr)
            print(_c(C.GREEN, f"  ✓ Invite sent"))

        elif sub == "history":
            if not rest:
                print(_c(C.RED, "Usage: group history <id>"))
                return
            gid  = int(rest[0])
            msgs = gc.history(gid)
            print(f"\n  Group {gid} history ({len(msgs)} messages):")
            for gm in msgs[-20:]:
                ts = time.strftime("%H:%M:%S", time.localtime(gm.timestamp))
                print(f"  [{ts}] {gm.src_str[:11]}: {gm.body}")
            print()
        else:
            print(_c(C.RED, f"Unknown group subcommand: {sub}"))

    async def _cmd_locate(self, args: list):
        if not args:
            print(_c(C.RED, "Usage: locate <addr>"))
            return
        addr = self._parse_addr(args[0])
        print(_c(C.YELLOW, "  Collecting RSSI readings…"))
        est = await self._node.locator.locate(addr, timeout=5.0)
        if est is None:
            print(_c(C.RED, "  Could not estimate position (no anchor readings)."))
            return
        d = est.to_dict()
        print(f"""
  {_c(C.BOLD, "Location Estimate")}
  Target     : {d['addr']}
  Position   : x={d['x_m']}m, y={d['y_m']}m
  Distance   : ~{d['distance_m']}m from this node
  Confidence : {d['confidence']*100:.0f}%
  Anchors    : {len(est.readings)}
""")

    async def _cmd_ping(self, args: list):
        if not args:
            print(_c(C.RED, "Usage: ping <addr>"))
            return
        addr = self._parse_addr(args[0])
        print(_c(C.YELLOW, f"  Pinging {':'.join(f'{b:02X}' for b in addr)}…"))
        rtt = await self._node.ping(addr)
        if rtt is None or rtt == float("inf"):
            print(_c(C.RED, "  No response."))
        else:
            print(_c(C.GREEN, f"  RTT: {rtt:.1f} ms"))

    async def _cmd_quit(self, _):
        print(_c(C.YELLOW, "\n  Shutting down…"))
        self._running = False
        await self._node.stop()

    # ── Incoming Message Display ───────────────────────────────────────────────

    def _register_callbacks(self):
        if self._node.messaging:
            self._node.messaging.on_receive(self._show_direct_msg)
        if self._node.group_chat:
            self._node.group_chat.on_message(self._show_group_msg)

    async def _show_direct_msg(self, msg: Message):
        ts = time.strftime("%H:%M:%S", time.localtime(msg.timestamp))
        print(f"\n  {_c(C.GREEN + C.BOLD, '◀ MSG')} [{ts}] "
              f"{_c(C.CYAN, msg.src_str[:17])} : {msg.body}")
        print(self.PROMPT, end="", flush=True)

    async def _show_group_msg(self, gm: GroupMessage):
        if gm.src_addr == self._node.local_addr:
            return
        ts   = time.strftime("%H:%M:%S", time.localtime(gm.timestamp))
        kind = "SYS" if gm.msg_type == "system" else "GRP"
        print(f"\n  {_c(C.BLUE + C.BOLD, f'◀ {kind}')} [{ts}] "
              f"group={gm.group_id} "
              f"{_c(C.CYAN, gm.src_str[:17])} : {gm.body}")
        print(self.PROMPT, end="", flush=True)

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_addr(s: str) -> bytes:
        """Accept 'AA:BB:CC:DD:EE:FF' or hex string."""
        s = s.replace(":", "").replace("-", "")
        if len(s) != 12:
            raise ValueError(f"Invalid address: {s!r}")
        return bytes(int(s[i:i+2], 16) for i in range(0, 12, 2))


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BLE Mesh Network Node")
    parser.add_argument("--name",    default="MeshNode",   help="Node name (≤28 chars)")
    parser.add_argument("--key",     default=None,         help="Network key (hex, 64 chars)")
    parser.add_argument("--log",     default="WARNING",    help="Log level")
    args = parser.parse_args()

    network_key = bytes.fromhex(args.key) if args.key else None

    async def _run():
        node = await create_node(
            name        = args.name,
            network_key = network_key,
            log_level   = args.log,
        )
        cli = MeshCLI(node)
        try:
            await cli.run()
        finally:
            await node.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()