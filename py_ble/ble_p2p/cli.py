"""
cli.py — Interactive command-line interface for BLE-P2P.

Run with:
    python -m ble_p2p.cli [--name MyNode] [--debug]

All commands are non-blocking: input is read in a thread pool executor so
the asyncio event loop keeps running (peripheral + scan tasks continue
operating while you type).

Commands
--------
help              — Show this list
status            — Node / peer / queue snapshot
peers             — List known peers
dm <id> <text>    — Send a direct message  (id = first 4+ hex chars)
broadcast <text>  — Broadcast to all reachable peers
mkgroup <name> [id1 id2 …]  — Create a group
groups            — List groups
gc <gid> <text>   — Send group chat message
locate            — Show RSSI / distance estimates for all peers
ping <id>         — Ping a peer (RTT measurement)
beacon <label>    — Broadcast your current location label
history <id>      — Show DM history with a peer
quit / exit       — Graceful shutdown
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from typing import Optional

from .node                        import BLEMeshNode
from .features.direct_message     import DirectMessageFeature
from .features.group_chat         import GroupChatFeature
from .features.device_locator     import DeviceLocatorFeature
from .constants                   import FeatureID


# ─────────────────────────────────────────────────────────────
# Colour helpers (graceful degradation on Windows / dumb terms)
# ─────────────────────────────────────────────────────────────
def _c(code: str, text: str) -> str:
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text

def _ok(t):   return _c("32", t)
def _warn(t): return _c("33", t)
def _err(t):  return _c("31", t)
def _dim(t):  return _c("2",  t)
def _bold(t): return _c("1",  t)
def _cyan(t): return _c("36", t)


def _ts(ts_ms: Optional[int]) -> str:
    if ts_ms is None:
        return "?"
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S")


# ─────────────────────────────────────────────────────────────
# CLI class
# ─────────────────────────────────────────────────────────────
class CLI:
    PROMPT = _cyan("ble-p2p> ")

    def __init__(self, node: BLEMeshNode, dm: DirectMessageFeature,
                 gc: GroupChatFeature, loc: DeviceLocatorFeature):
        self.node = node
        self.dm   = dm
        self.gc   = gc
        self.loc  = loc

    # ── Boot helpers ──────────────────────────────────────────

    def _resolve_peer(self, prefix: str):
        """Find a peer whose id_hex starts with *prefix* (case-insensitive)."""
        prefix = prefix.lower()
        for peer in self.node.peers.all_peers():
            if peer.id_hex.startswith(prefix) or peer.name.lower().startswith(prefix):
                return peer
        return None

    def _print_banner(self):
        d = self.node.device
        print()
        print(_bold("╔══════════════════════════════════════╗"))
        print(_bold("║       BLE-P2P Mesh Node  v1.0        ║"))
        print(_bold("╚══════════════════════════════════════╝"))
        print(f"  Name : {_cyan(d.name)}")
        print(f"  ID   : {_dim(d.id_hex)}")
        print(f"  UUID : {_dim(d.device_uuid)}")
        print()
        print(_dim("Type 'help' for commands."))
        print()

    # ── Command handlers ──────────────────────────────────────

    async def _cmd_help(self, _args):
        print(__doc__)

    async def _cmd_status(self, _args):
        s = self.node.status()
        print(_bold(f"\n── Node: {s['device']['name']} ({s['device']['id']}) ──"))
        print(f"  Peripheral : {'✓ running' if s['peripheral'] else '✗ stopped'}")
        print(f"  Features   : {', '.join(s['features']) or 'none'}")
        print(f"  Peers      : {len(s['peers'])}")
        print(f"  Queued     : {sum(s['queued'].values()) if s['queued'] else 0} frame(s)")
        if s['queued']:
            for dst, cnt in s['queued'].items():
                print(f"               → {dst[:8]}… : {cnt} frame(s)")
        print()

    async def _cmd_peers(self, _args):
        peers = self.node.peers.all_peers()
        if not peers:
            print(_warn("No peers discovered yet."))
            return
        print(_bold(f"\n{'Name':<20} {'ID':<18} {'RSSI':>6} {'Fresh':<7} {'Addr'}"))
        print("─" * 72)
        for p in sorted(peers, key=lambda x: x.last_seen, reverse=True):
            fresh = _ok("✓") if p.is_fresh else _warn("✗")
            rssi_s = _ok(f"{p.rssi:>4} dBm") if p.rssi > -80 else _warn(f"{p.rssi:>4} dBm")
            print(f"{p.name:<20} {p.id_hex[:16]:<18} {rssi_s}  {fresh}      {p.ble_address}")
        print()

    async def _cmd_dm(self, args):
        if len(args) < 2:
            print(_err("Usage: dm <peer_id_prefix_or_name> <message text>"))
            return
        peer = self._resolve_peer(args[0])
        if peer is None:
            print(_err(f"Unknown peer: {args[0]}"))
            return
        text = " ".join(args[1:])
        ok   = await self.dm.send(peer.device_id, text)
        if ok:
            print(_ok(f"✓ Queued DM for {peer.name}"))
        else:
            print(_err("Failed to queue message"))

    async def _cmd_broadcast(self, args):
        if not args:
            print(_err("Usage: broadcast <text>"))
            return
        text = " ".join(args)
        ok   = await self.dm.broadcast(text)
        print(_ok("✓ Broadcast queued") if ok else _err("Failed"))

    async def _cmd_mkgroup(self, args):
        if not args:
            print(_err("Usage: mkgroup <name> [peer_prefix1 peer_prefix2 …]"))
            return
        name      = args[0]
        members   = []
        for prefix in args[1:]:
            peer = self._resolve_peer(prefix)
            if peer:
                members.append(peer.device_id)
            else:
                print(_warn(f"  Peer not found: {prefix} (skipped)"))
        gid = self.gc.create_group(name, members)
        print(_ok(f"✓ Group created: {name} (id={gid}, {len(members)+1} member(s))"))

    async def _cmd_groups(self, _args):
        groups = self.gc.list_groups()
        if not groups:
            print(_warn("No groups."))
            return
        for g in groups:
            print(f"  {_cyan(g.group_id)}  {g.name}  ({len(g.members)} member(s))")
            for m in g.members:
                peer = self.node.peers.by_id_hex(m)
                label = peer.name if peer else m[:8]
                me_s  = _dim(" (you)") if m == self.node.device.id_hex else ""
                print(f"         • {label}{me_s}")
        print()

    async def _cmd_gc(self, args):
        if len(args) < 2:
            print(_err("Usage: gc <group_id_prefix> <text>"))
            return
        gid_prefix = args[0]
        # Find matching group
        group = None
        for g in self.gc.list_groups():
            if g.group_id.startswith(gid_prefix):
                group = g
                break
        if group is None:
            print(_err(f"Group not found: {gid_prefix}"))
            return
        text    = " ".join(args[1:])
        results = await self.gc.send(group.group_id, text)
        ok_cnt  = sum(1 for v in results.values() if v)
        print(_ok(f"✓ Queued for {ok_cnt}/{len(results)} member(s) of '{group.name}'"))

    async def _cmd_locate(self, _args):
        locs = self.loc.get_all()
        if not locs:
            print(_warn("No location data yet.  Wait for a scan cycle."))
            return
        print(_bold(f"\n{'Name/ID':<22} {'RSSI':>7} {'Distance':>12} {'Proximity':<12} {'Label'}"))
        print("─" * 70)
        for loc in locs:
            peer = self.node.peers.by_id_hex(loc["id"])
            name = peer.name if peer else loc["id"][:8]
            rssi_s = f"{loc['avg_rssi']:.0f} dBm" if loc["avg_rssi"] else "  n/a"
            dist_s = f"{loc['distance_m']:.1f} m" if loc["distance_m"] else "  n/a"
            rtt_s  = f"RTT {loc['rtt_ms']:.0f} ms" if loc["rtt_ms"] else ""
            print(
                f"{name:<22} {rssi_s:>7} {dist_s:>12} {loc['proximity']:<12} "
                f"{loc['label'] or ''} {_dim(rtt_s)}"
            )
        print()

    async def _cmd_ping(self, args):
        if not args:
            print(_err("Usage: ping <peer_id_prefix>"))
            return
        peer = self._resolve_peer(args[0])
        if peer is None:
            print(_err(f"Unknown peer: {args[0]}"))
            return
        print(f"Pinging {peer.name}…", end=" ", flush=True)
        rtt = await self.loc.ping(peer.device_id)
        if rtt is not None:
            print(_ok(f"RTT = {rtt*1000:.1f} ms"))
        else:
            print(_warn("timeout"))

    async def _cmd_beacon(self, args):
        if not args:
            print(_err("Usage: beacon <location label>"))
            return
        label = " ".join(args)
        ok    = await self.loc.beacon(label)
        print(_ok(f"✓ Beacon broadcasted: {label!r}") if ok else _err("Failed"))

    async def _cmd_history(self, args):
        if not args:
            print(_err("Usage: history <peer_id_prefix>"))
            return
        peer = self._resolve_peer(args[0])
        if peer is None:
            print(_err(f"Unknown peer: {args[0]}"))
            return
        history = self.dm.get_history(peer.id_hex)
        if not history:
            print(_warn(f"No DM history with {peer.name}"))
            return
        print(_bold(f"\n── DM history with {peer.name} ──"))
        for entry in history:
            arrow = _ok("→") if entry["dir"] == "out" else _cyan("←")
            ts_s  = _dim(_ts(entry["ts"]))
            print(f"  {ts_s} {arrow} {entry['text']}")
        print()

    # ── Main REPL ─────────────────────────────────────────────

    COMMANDS = {
        "help"     : _cmd_help,
        "status"   : _cmd_status,
        "peers"    : _cmd_peers,
        "dm"       : _cmd_dm,
        "broadcast": _cmd_broadcast,
        "mkgroup"  : _cmd_mkgroup,
        "groups"   : _cmd_groups,
        "gc"       : _cmd_gc,
        "locate"   : _cmd_locate,
        "ping"     : _cmd_ping,
        "beacon"   : _cmd_beacon,
        "history"  : _cmd_history,
    }

    async def run(self):
        self._print_banner()

        loop = asyncio.get_running_loop()
        while self.node._running:
            try:
                line = await loop.run_in_executor(
                    None, lambda: input(self.PROMPT)
                )
            except (EOFError, KeyboardInterrupt):
                print()
                break

            line = line.strip()
            if not line:
                continue
            if line in ("quit", "exit", "q"):
                break

            parts = line.split()
            cmd   = parts[0].lower()
            args  = parts[1:]

            handler = self.COMMANDS.get(cmd)
            if handler:
                try:
                    await handler(self, args)
                except Exception as exc:
                    print(_err(f"Error: {exc}"))
            else:
                print(_warn(f"Unknown command: {cmd!r}  (type 'help')"))

        print("\nShutting down…")
        await self.node.stop()
        print(_ok("Goodbye."))


# ─────────────────────────────────────────────────────────────
# Notification printers
# ─────────────────────────────────────────────────────────────

def _make_dm_printer(peers_ref):
    async def on_dm(from_name, from_id_hex, text, ts_ms):
        ts_s = _ts(ts_ms)
        print(f"\n{_cyan('[DM]')} {_bold(from_name)} {_dim(ts_s)}: {text}")
        print(CLI.PROMPT, end="", flush=True)
    return on_dm


def _make_gc_printer():
    async def on_gc(gid, gname, from_name, from_id_hex, text, ts_ms):
        ts_s = _ts(ts_ms)
        print(f"\n{_cyan(f'[{gname}]')} {_bold(from_name)} {_dim(ts_s)}: {text}")
        print(CLI.PROMPT, end="", flush=True)
    return on_gc


def _make_presence_printer():
    async def on_presence(from_id_hex, from_name, label, ts):
        print(f"\n{_cyan('[BEACON]')} {_bold(from_name)} is at: {label}")
        print(CLI.PROMPT, end="", flush=True)
    return on_presence


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

async def _main():
    parser = argparse.ArgumentParser(description="BLE-P2P Mesh Node")
    parser.add_argument("--name",  default=None, help="Override device display name")
    parser.add_argument("--debug", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level   = logging.DEBUG if args.debug else logging.WARNING,
        format  = "%(asctime)s %(name)-24s %(levelname)-8s %(message)s",
        datefmt = "%H:%M:%S",
    )

    # Build node and features
    node = BLEMeshNode(name=args.name)
    dm   = DirectMessageFeature(node)
    gc   = GroupChatFeature(node)
    loc  = DeviceLocatorFeature(node)

    node.register_feature(dm)
    node.register_feature(gc)
    node.register_feature(loc)

    # Register live notification printers
    dm.on_message(_make_dm_printer(node.peers))
    gc.on_message(_make_gc_printer())
    loc.on_presence(_make_presence_printer())

    await node.start()
    cli = CLI(node, dm, gc, loc)
    await cli.run()


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    main()