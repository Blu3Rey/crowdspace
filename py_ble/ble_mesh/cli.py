"""
cli.py — Interactive command-line interface for the BLE mesh node.

Run with::

    python -m ble_mesh.cli --name MyNode

Commands
--------
  msg  <node_id_prefix> <text>   — Send a direct message
  grp  join  <group>             — Join a group channel
  grp  leave <group>             — Leave a group channel
  grp  send  <group> <text>      — Send a group message
  loc  [node_id_prefix]          — Locate devices (RSSI)
  nb                             — List neighbours
  rt                             — Print routing table
  st                             — Node status
  help                           — Show this help
  quit                           — Stop and exit
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import MeshConfig
from .core.node import MeshNode
from .core.protocol import BROADCAST_ADDR
from .features.messaging import DirectMessaging
from .features.group_chat import GroupChat
from .features.locating import DeviceLocator
from .utils.logger import log, set_level


# ── Callbacks ─────────────────────────────────────────────────────────────────

def make_msg_handler(node_name: str):
    async def handler(src_id: bytes, text: str, msg_id: int) -> None:
        print(f"\n  📨 [{node_name}] DM from {src_id.hex()[:8]}… (id={msg_id}): {text}\n> ", end="", flush=True)
    return handler


def make_group_handler(node_name: str):
    async def handler(group_id: str, src_id: bytes, text: str, msg_id: int) -> None:
        print(f"\n  💬 [{node_name}] #{group_id} | {src_id.hex()[:8]}…: {text}\n> ", end="", flush=True)
    return handler


# ── CLI ───────────────────────────────────────────────────────────────────────

async def repl(node: MeshNode, messaging: DirectMessaging, chat: GroupChat, locator: DeviceLocator) -> None:
    """Read-Eval-Print Loop for the mesh CLI."""
    loop = asyncio.get_running_loop()
    print("\n  BLE Mesh CLI  —  type 'help' for commands\n")

    def _read_line() -> str:
        return sys.stdin.readline()

    while True:
        print("> ", end="", flush=True)
        try:
            line = await loop.run_in_executor(None, _read_line)
        except (EOFError, KeyboardInterrupt):
            break

        parts = line.strip().split()
        if not parts:
            continue
        cmd, *args = parts

        try:
            # ── Direct message ────────────────────────────────────────────────
            if cmd == "msg":
                if len(args) < 2:
                    print("  Usage: msg <node_id_hex_prefix> <text>")
                    continue
                prefix, text = args[0].lower(), " ".join(args[1:])
                # Resolve node_id from prefix
                dst = None
                for nb in node.neighbors.all():
                    if nb.node_id.hex().startswith(prefix):
                        dst = nb.node_id
                        break
                if dst is None:
                    print(f"  ✗ No neighbour matching prefix '{prefix}'")
                    continue
                ok = await messaging.send(text, dst_id=dst, reliable=True)
                print(f"  {'✓' if ok else '✗'} Message {'delivered' if ok else 'failed'}.")

            # ── Group commands ─────────────────────────────────────────────────
            elif cmd == "grp":
                if not args:
                    print("  Usage: grp join|leave|send <group> [text]")
                    continue
                sub = args[0]
                if sub == "join" and len(args) >= 2:
                    await chat.join(args[1])
                    print(f"  ✓ Joined #{args[1]}")
                elif sub == "leave" and len(args) >= 2:
                    await chat.leave(args[1])
                    print(f"  ✓ Left #{args[1]}")
                elif sub == "send" and len(args) >= 3:
                    text = " ".join(args[2:])
                    ok   = await chat.send(args[1], text)
                    print(f"  {'✓' if ok else '✗'} Group message {'sent' if ok else 'failed'}.")
                else:
                    print("  Usage: grp join|leave|send <group> [text]")

            # ── Locating ──────────────────────────────────────────────────────
            elif cmd == "loc":
                print("  🔍 Sending location request… (5s)")
                target = None
                if args:
                    prefix = args[0].lower()
                    for nb in node.neighbors.all():
                        if nb.node_id.hex().startswith(prefix):
                            target = nb.node_id
                            break
                report = await locator.locate(target, timeout=5.0)
                if not report:
                    print("  No responses received.")
                else:
                    print(f"  {'NAME':<20}  {'NODE ID':>10}  {'RSSI':>6}  {'HOPS':>5}  {'DIST(m)':>8}")
                    print("  " + "-" * 58)
                    for e in report:
                        print(f"  {e['responder_name']:<20}  {e['responder_id'][:10]}  "
                              f"{e['rssi']:>6} dBm  {e['hops']:>5}  ~{e['distance_m']:>6.1f} m")

            # ── Neighbour table ───────────────────────────────────────────────
            elif cmd in ("nb", "neighbors"):
                nbs = node.neighbors.all()
                if not nbs:
                    print("  No neighbours discovered yet.")
                else:
                    print(f"  {'NAME':<20}  {'NODE ID':>10}  {'RSSI':>6}  {'CONN':>6}  {'AGE(s)':>7}")
                    print("  " + "-" * 57)
                    for nb in nbs:
                        print(f"  {nb.name:<20}  {nb.node_id.hex()[:10]}  "
                              f"{nb.rssi:>6} dBm  {'yes' if nb.is_connected else 'no':>6}  "
                              f"{nb.age:>6.0f}s")

            # ── Routing table ─────────────────────────────────────────────────
            elif cmd in ("rt", "routes"):
                routes = node.router.all_routes()
                if not routes:
                    print("  Routing table empty.")
                else:
                    print(f"  {'DESTINATION':>12}  {'NEXT HOP':>12}  {'HOPS':>5}  {'RSSI':>6}  {'METRIC':>7}")
                    print("  " + "-" * 50)
                    for r in routes:
                        print(f"  {r.dst_id.hex()[:10]}  {r.next_hop_id.hex()[:10]}  "
                              f"{r.hop_count:>5}  {r.rssi:>6} dBm  {r.metric:>7.2f}")

            # ── Status ────────────────────────────────────────────────────────
            elif cmd in ("st", "status"):
                s = node.status()
                print(f"  Node:        {s['node_name']} ({s['node_id'][:16]}…)")
                print(f"  Running:     {s['running']}")
                print(f"  Connections: {s['connections']}")
                print(f"  Neighbours:  {len(s['neighbors'])}")
                print(f"  Routes:      {s['routes']}")
                print(f"  Features:    {', '.join(s['features'])}")
                print(f"  Groups:      {', '.join(chat.joined_groups) or '(none)'}")

            elif cmd == "help":
                print(__doc__)

            elif cmd in ("quit", "exit", "q"):
                break

            else:
                print(f"  Unknown command: '{cmd}'.  Type 'help' for usage.")

        except Exception as exc:
            print(f"  ✗ Error: {exc}")

    await node.stop()


async def main(args: argparse.Namespace) -> None:
    set_level(args.log_level)

    cfg = MeshConfig(
        node_name=args.name,
        power_profile=args.profile,
        enable_encryption=args.encrypt,
        psk=(args.psk.encode() * 4)[:32] if args.psk else None,
    )

    print(f"  Node ID : {cfg.node_id.hex()}")
    print(f"  Profile : {cfg.power_profile}")
    print(f"  Encrypt : {cfg.enable_encryption}")
    if cfg.enable_encryption:
        print(f"  PSK     : {cfg.psk.hex() if cfg.psk else 'auto-generated'}")

    node      = MeshNode(cfg)
    messaging = DirectMessaging(ack_timeout=5.0, max_retries=3)
    chat      = GroupChat()
    locator   = DeviceLocator()

    # Register callbacks
    messaging.on_message(make_msg_handler(args.name))
    chat.on_any_message(make_group_handler(args.name))

    # Attach features to the node
    node.register_feature(messaging)
    node.register_feature(chat)
    node.register_feature(locator)

    await node.start()

    # Auto-join groups from CLI args
    for g in args.join:
        await chat.join(g)

    await repl(node, messaging, chat, locator)


def cli_entry() -> None:
    parser = argparse.ArgumentParser(
        description="BLE Mesh Network — interactive node",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--name",      default="MeshNode",  help="Node display name")
    parser.add_argument("--profile",   default="balanced",
                        choices=["low_power", "balanced", "high_performance"],
                        help="Power / scan profile")
    parser.add_argument("--encrypt",   action="store_true", help="Enable AES-256-GCM encryption")
    parser.add_argument("--psk",       default=None,        help="Pre-shared key (ASCII, padded to 32B)")
    parser.add_argument("--join",      nargs="*", default=[], metavar="GROUP",
                        help="Auto-join these groups on startup")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log verbosity")
    args = parser.parse_args()
    asyncio.run(main(args))


if __name__ == "__main__":
    cli_entry()
