#!/usr/bin/env python3
"""
ble_chat.py — Symmetric BLE peer-to-peer messenger
═══════════════════════════════════════════════════

Run this SAME script on two machines. They automatically negotiate
peripheral / central roles by comparing a random priority number
embedded in their advertising name, then establish a full-duplex
GATT messaging channel.

Features
────────
  · Zero configuration — just run the script on both devices
  · Deterministic role negotiation (no race conditions)
  · 5-byte frame protocol with sequence numbers
  · Automatic message fragmentation / reassembly (MTU-aware)
  · Delivery ACK with ✓ confirmation display
  · Ping / pong with round-trip latency measurement
  · Typing indicator
  · Auto-reconnect with exponential backoff (central side)
  · ANSI terminal UI — messages scroll, input stays at bottom

Requirements
────────────
  pip install bleak bless

Usage
─────
  python ble_chat.py
  python ble_chat.py --name Alice
  python ble_chat.py --debug

Commands (during chat)
──────────────────────
  /ping          measure round-trip latency
  /name <name>   change your display name
  /quit          exit

Platform notes
──────────────
  Linux   : requires BlueZ 5.50+. Run bluetoothd with --experimental
            flag if GATT server registration fails.
  macOS   : grants Bluetooth permission on first run (OS dialog).
  Windows : requires Windows 10 1709+ (WinRT Bluetooth LE APIs).

  bless and bleak can coexist on the same asyncio loop. During role
  negotiation both a BlessServer (peripheral/advertising) and a
  BleakScanner (central/scanning) run simultaneously. After role
  assignment, the device that becomes central tears down its
  BlessServer and connects as a BleakClient.
"""

import argparse
import asyncio
import logging
import random
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bless import (
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

VERSION = "1.0"

# ── GATT UUIDs ─────────────────────────────────────────────────────────────────
CHAT_SVC = "c0debabe-cafe-dead-beef-000000000001"
TX_CHAR  = "c0debabe-cafe-dead-beef-000000000002"  # peripheral→central  (NOTIFY)
RX_CHAR  = "c0debabe-cafe-dead-beef-000000000003"  # central→peripheral  (WRITE)

# ── Advertising ────────────────────────────────────────────────────────────────
NAME_PFX   = "BLEChat-"   # advertising name prefix
SCAN_LIMIT = 45.0         # max seconds to wait for a peer before giving up

# ── Connection ─────────────────────────────────────────────────────────────────
RECONNECT_BASE    = 1.0   # initial reconnect delay (seconds)
RECONNECT_MAX     = 60.0  # maximum reconnect delay (seconds)
INTER_FRAME_DELAY = 0.012 # seconds between consecutive frame writes
ACK_TIMEOUT       = 4.0   # seconds to wait for a delivery ACK

# ═══════════════════════════════════════════════════════════════════════════════
# FRAME PROTOCOL
# ═══════════════════════════════════════════════════════════════════════════════
#
#  Header: 5 bytes
#  ┌────────┬─────┬──────────┬──────────────┬─────────┐
#  │ type   │ seq │ frag_idx │ frag_total   │ msg_id  │
#  │ u8     │ u8  │ u8       │ u8           │ u8      │
#  └────────┴─────┴──────────┴──────────────┴─────────┘
#  Followed by payload bytes (0 … ATT_MTU−3−5).
#
#  seq:       monotonically increasing TX counter, wraps 0→255
#  frag_idx:  0-based index of this fragment within the message
#  frag_total: total fragments for this message (1 = not fragmented)
#  msg_id:    groups fragments belonging to the same message (wraps)

HDR = 5  # header size in bytes


class MsgType(IntEnum):
    HANDSHAKE = 0x01   # exchange display names on connect
    DATA      = 0x02   # chat message (possibly fragmented)
    ACK       = 0x03   # delivery acknowledgement
    PING      = 0x04   # latency probe (payload: 4-byte random ID)
    PONG      = 0x05   # latency probe reply (echo payload)
    TYPING    = 0x06   # typing indicator (payload: 0x00=stop, 0x01=start)


@dataclass
class Frame:
    """A single BLE packet. Encodes to / decodes from raw bytes."""

    type:  MsgType
    seq:   int
    fi:    int    # frag_idx
    fn:    int    # frag_total
    mid:   int    # msg_id
    data:  bytes = b""

    def encode(self) -> bytes:
        return struct.pack(
            "BBBBB",
            int(self.type),
            self.seq  & 0xFF,
            self.fi   & 0xFF,
            self.fn   & 0xFF,
            self.mid  & 0xFF,
        ) + self.data

    @classmethod
    def decode(cls, raw: bytes | bytearray) -> "Frame":
        if len(raw) < HDR:
            raise ValueError(f"Frame too short ({len(raw)} bytes)")
        t, seq, fi, fn, mid = struct.unpack_from("BBBBB", raw)
        return cls(MsgType(t), seq, fi, fn, mid, bytes(raw[HDR:]))


class Coder:
    """
    Handles fragmentation and reassembly of messages.

    chunk_size is the maximum payload bytes per frame. It is set
    conservatively to 16 bytes for the default ATT MTU (23) and
    updated to (mtu - 3 - HDR) after MTU negotiation succeeds.
    """

    def __init__(self, chunk_size: int = 16):
        self.chunk_size = max(1, chunk_size)
        self._seq = 0
        self._mid = 0
        # reassembly buffer: msg_id → {frag_idx: data}
        self._buf:  dict[int, dict[int, bytes]] = {}
        self._tots: dict[int, int] = {}

    def _next_seq(self) -> int:
        v = self._seq; self._seq = (v + 1) & 0xFF; return v

    def _next_mid(self) -> int:
        v = self._mid; self._mid = (v + 1) & 0xFF; return v

    def fragment(self, typ: MsgType, payload: bytes) -> list[Frame]:
        """Split payload into MTU-sized frames."""
        chunks = [
            payload[i : i + self.chunk_size]
            for i in range(0, max(1, len(payload)), self.chunk_size)
        ]
        mid = self._next_mid()
        return [
            Frame(typ, self._next_seq(), idx, len(chunks), mid, chunk)
            for idx, chunk in enumerate(chunks)
        ]

    def reassemble(self, frame: Frame) -> bytes | None:
        """
        Feed an incoming frame. Returns the complete message payload
        once all fragments have arrived, otherwise returns None.
        """
        if frame.fn == 1:           # not fragmented
            return frame.data

        mid = frame.mid
        if mid not in self._buf:
            self._buf[mid]  = {}
            self._tots[mid] = frame.fn

        self._buf[mid][frame.fi] = frame.data

        if len(self._buf[mid]) == self._tots[mid]:
            payload = b"".join(
                self._buf[mid][i] for i in range(self._tots[mid])
            )
            del self._buf[mid], self._tots[mid]
            return payload

        return None


# ═══════════════════════════════════════════════════════════════════════════════
# TERMINAL UI
# ═══════════════════════════════════════════════════════════════════════════════

RST = "\033[0m"
BLD = "\033[1m"
DIM = "\033[2m"
GRN = "\033[32m"
CYN = "\033[36m"
YLW = "\033[33m"
RED = "\033[31m"
MGN = "\033[35m"
WHT = "\033[97m"
BLU = "\033[34m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class UI:
    """
    Thread-safe terminal UI. Maintains a persistent "You ▸" input
    prompt at the bottom of the screen. Any output line erases the
    prompt, prints, then re-draws the prompt — so user input is
    never clobbered by incoming messages.
    """

    def __init__(self, name: str):
        self.name = name
        self.peer = "Peer"
        self._lock   = threading.Lock()
        self._typing = False    # is peer currently typing?

    # ── internal ───────────────────────────────────────────────────────────────

    def _write(self, line: str) -> None:
        with self._lock:
            sys.stdout.write(f"\r\033[K{line}\n")
            self._draw_prompt()

    def _draw_prompt(self) -> None:
        sys.stdout.write(f"{CYN}{BLD}You{RST} ▸ ")
        sys.stdout.flush()

    # ── public ─────────────────────────────────────────────────────────────────

    def banner(self) -> None:
        print(
            f"\n{CYN}{BLD}"
            f"  ╔══════════════════════════════════════╗\n"
            f"  ║      BLE P2P Chat  ·  v{VERSION}          ║\n"
            f"  ╚══════════════════════════════════════╝{RST}\n"
            f"\n  Running as {BLD}{self.name}{RST}"
            f"\n  {DIM}Waiting to discover a peer…{RST}"
            f"\n  {DIM}Commands:  /ping   /name <n>   /quit{RST}\n"
        )

    def status(self, msg: str, role: str = "") -> None:
        tag = f" {DIM}[{role}]{RST}" if role else ""
        self._write(f"{DIM}── {msg}{tag} ──{RST}")

    def incoming(self, text: str) -> None:
        self._write(
            f"{DIM}[{_ts()}]{RST} "
            f"{CYN}{BLD}{self.peer}{RST}: {WHT}{text}{RST}"
        )

    def outgoing(self, text: str, delivered: bool = False) -> None:
        tick = f"  {DIM}✓{RST}" if delivered else ""
        self._write(
            f"{DIM}[{_ts()}]{RST} "
            f"{GRN}{BLD}{self.name}{RST}: {WHT}{text}{RST}{tick}"
        )

    def event(self, msg: str) -> None:
        self._write(f"{DIM}[{_ts()}] ── {msg} ──{RST}")

    def ping_result(self, ms: float) -> None:
        self._write(f"{DIM}  ◎  {ms:.1f} ms round-trip{RST}")

    def error(self, msg: str) -> None:
        self._write(f"{RED}  ✗  {msg}{RST}")

    def typing_start(self) -> None:
        if not self._typing:
            self._typing = True
            self._write(f"{DIM}  {self.peer} is typing…{RST}")

    def typing_stop(self) -> None:
        self._typing = False

    def prompt(self) -> None:
        with self._lock:
            self._draw_prompt()


# ═══════════════════════════════════════════════════════════════════════════════
# BLE CHAT APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

class BLEChat:
    """
    Symmetric BLE peer-to-peer chat.

    Both instances run the same code. Role assignment (peripheral vs
    central) is resolved automatically by comparing uint32 priority
    numbers embedded in advertising names.
    """

    def __init__(self, display_name: str) -> None:
        self._name     = display_name
        self._priority = random.randint(0, 0xFFFF_FFFF)
        self._adv_name = f"{NAME_PFX}{self._priority:08X}"
        self._ui       = UI(display_name)
        self._loop:    asyncio.AbstractEventLoop | None = None

        # Shared state
        self._role:      str = ""          # "peripheral" | "central"
        self._connected: bool = False
        self._peer_name: str = "Peer"

        # Coders — one per direction to keep sequence numbers independent
        self._tx_coder = Coder(chunk_size=16)  # updated after MTU negotiation
        self._rx_coder = Coder()

        # Inter-coroutine communication
        self._tx_queue: asyncio.Queue[Frame] = asyncio.Queue()

        # ACK tracking: seq → (Event, message_text)
        self._pending_acks: dict[int, tuple[asyncio.Event, str]] = {}

        # Ping tracking: ping_id → monotonic start time
        self._ping_times: dict[int, float] = {}

        # BLE handles (set when roles are determined)
        self._server: BlessServer  | None = None
        self._client: BleakClient  | None = None

        # Handshake state: avoid echoing indefinitely
        self._handshake_sent = False

    # ═══ INTERNAL HELPERS ═════════════════════════════════════════════════════

    def _enqueue(self, typ: MsgType, payload: bytes) -> list[Frame]:
        """Fragment payload and put all frames on the TX queue. Returns frames."""
        frames = self._tx_coder.fragment(typ, payload)
        for f in frames:
            self._tx_queue.put_nowait(f)
        return frames

    async def _send_handshake(self) -> None:
        if self._handshake_sent:
            return
        self._handshake_sent = True
        self._enqueue(MsgType.HANDSHAKE, self._name.encode())

    async def _send_ack(self, seq: int) -> None:
        self._enqueue(MsgType.ACK, bytes([seq & 0xFF]))

    async def _send_pong(self, ping_payload: bytes) -> None:
        self._enqueue(MsgType.PONG, ping_payload)

    async def _send_typing(self, active: bool) -> None:
        self._enqueue(MsgType.TYPING, bytes([0x01 if active else 0x00]))

    # ═══ FRAME DISPATCH ═══════════════════════════════════════════════════════

    def _on_frame_from_thread(self, frame: Frame) -> None:
        """
        Entry point from BLE stack threads (bleak callbacks run in the
        asyncio loop thread; bless callbacks may run elsewhere).
        Always safe to call from any thread.
        """
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._dispatch, frame)

    def _dispatch(self, frame: Frame) -> None:
        """Route a frame to the appropriate handler (runs in event loop)."""
        if   frame.type == MsgType.HANDSHAKE: self._on_handshake(frame)
        elif frame.type == MsgType.DATA:      self._on_data(frame)
        elif frame.type == MsgType.ACK:       self._on_ack(frame)
        elif frame.type == MsgType.PING:      asyncio.ensure_future(self._on_ping(frame))
        elif frame.type == MsgType.PONG:      self._on_pong(frame)
        elif frame.type == MsgType.TYPING:    self._on_typing(frame)

    def _on_handshake(self, frame: Frame) -> None:
        payload = self._rx_coder.reassemble(frame)
        if payload is None:
            return
        peer_name = payload.decode(errors="replace").strip("\x00")
        if not peer_name:
            return
        self._peer_name  = peer_name
        self._ui.peer    = peer_name
        self._connected  = True
        self._ui.status(f"Connected to {peer_name}", role=self._role.capitalize())
        self._ui.event(f"{peer_name} joined")
        # Echo our own handshake so peer knows our name
        asyncio.ensure_future(self._send_handshake())
        self._ui.prompt()

    def _on_data(self, frame: Frame) -> None:
        payload = self._rx_coder.reassemble(frame)
        if payload is None:
            return
        self._ui.incoming(payload.decode("utf-8", errors="replace"))
        asyncio.ensure_future(self._send_ack(frame.seq))
        self._ui.typing_stop()

    def _on_ack(self, frame: Frame) -> None:
        payload = self._rx_coder.reassemble(frame)
        if payload is None or not payload:
            return
        acked_seq = payload[0]
        if acked_seq in self._pending_acks:
            evt, text = self._pending_acks.pop(acked_seq)
            evt.set()
            self._ui.outgoing(text, delivered=True)

    async def _on_ping(self, frame: Frame) -> None:
        payload = self._rx_coder.reassemble(frame)
        if payload:
            await self._send_pong(payload)

    def _on_pong(self, frame: Frame) -> None:
        payload = self._rx_coder.reassemble(frame)
        if payload and len(payload) >= 4:
            pid = struct.unpack_from("<I", payload)[0]
            if pid in self._ping_times:
                ms = (time.monotonic() - self._ping_times.pop(pid)) * 1000
                self._ui.ping_result(ms)

    def _on_typing(self, frame: Frame) -> None:
        if frame.data:
            if frame.data[0]:
                self._ui.typing_start()
            else:
                self._ui.typing_stop()

    # ═══ PUBLIC API (called from input loop) ══════════════════════════════════

    async def send_message(self, text: str) -> None:
        frames = self._tx_coder.fragment(MsgType.DATA, text.encode("utf-8"))
        first_seq = frames[0].seq

        evt = asyncio.Event()
        self._pending_acks[first_seq] = (evt, text)
        self._ui.outgoing(text)  # show immediately; ✓ added on ACK

        for f in frames:
            await self._tx_queue.put(f)

        # If no ACK arrives within timeout, still show the message cleanly
        asyncio.ensure_future(self._expire_ack(first_seq, evt))

    async def _expire_ack(self, seq: int, evt: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(evt.wait(), ACK_TIMEOUT)
        except asyncio.TimeoutError:
            # Remove stale entry quietly — message was likely delivered
            self._pending_acks.pop(seq, None)

    async def send_ping(self) -> None:
        pid = random.randint(0, 0xFFFF_FFFF)
        self._ping_times[pid] = time.monotonic()
        self._enqueue(MsgType.PING, struct.pack("<I", pid))

    # ═══ ROLE NEGOTIATION ═════════════════════════════════════════════════════

    async def _negotiate_role(self) -> tuple[str, BLEDevice | None]:
        """
        Both devices advertise and scan simultaneously.

        The advertising name encodes an 8-hex-digit random priority:
          "BLEChat-A3F2C109"

        When each device's scanner finds a peer:
          · own priority > peer priority  →  stay as peripheral (GATT server)
          · own priority < peer priority  →  become central   (GATT client)
          · equal (1 in 2^32 chance)      →  re-roll and retry

        Returns ("peripheral", None) or ("central", <BLEDevice>).
        """
        self._ui.status("Advertising & scanning for peer…")
        found_q: asyncio.Queue[tuple[BLEDevice, int]] = asyncio.Queue()
        seen: set[str] = set()

        def on_advertisement(device: BLEDevice, adv) -> None:
            name = adv.local_name or device.name or ""
            if not name.startswith(NAME_PFX) or device.address in seen:
                return
            seen.add(device.address)
            try:
                peer_prio = int(name[len(NAME_PFX) : len(NAME_PFX) + 8], 16)
            except ValueError:
                return
            if peer_prio == self._priority:
                return          # reflected our own advertisement (rare)
            self._loop.call_soon_threadsafe(
                found_q.put_nowait, (device, peer_prio)
            )

        async with BleakScanner(detection_callback=on_advertisement):
            try:
                device, peer_prio = await asyncio.wait_for(
                    found_q.get(), SCAN_LIMIT
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "No peer discovered. Is the other device running "
                    f"the same script? (waited {SCAN_LIMIT:.0f}s)"
                )

        if self._priority == peer_prio:
            self._ui.event("Priority collision — re-rolling…")
            self._priority = random.randint(0, 0xFFFF_FFFF)
            self._adv_name = f"{NAME_PFX}{self._priority:08X}"
            return await self._negotiate_role()

        if self._priority > peer_prio:
            return "peripheral", None
        else:
            return "central", device

    # ═══ PERIPHERAL SIDE ══════════════════════════════════════════════════════

    async def _run_as_peripheral(self) -> None:
        """
        Build the GATT server, advertise, and serve connections forever.
        TX frames (outgoing to central) are sent via update_value/notify.
        RX frames (incoming from central) arrive via the write callback.
        """
        self._ui.status("Waiting for peer to connect…", role="Peripheral")

        server = BlessServer(name=self._adv_name)
        server.read_request_func  = self._gatt_read
        server.write_request_func = self._gatt_write
        self._server = server

        await server.add_new_service(CHAT_SVC)

        # TX: peripheral → central  (notify only — central subscribes via CCCD)
        await server.add_new_characteristic(
            CHAT_SVC, TX_CHAR,
            GATTCharacteristicProperties.notify,
            None,
            GATTAttributePermissions.readable,
        )

        # RX: central → peripheral  (write only)
        await server.add_new_characteristic(
            CHAT_SVC, RX_CHAR,
            GATTCharacteristicProperties.write,
            None,
            GATTAttributePermissions.writeable,
        )

        await server.start()

        # TX loop: drain queue and notify connected central(s)
        while True:
            frame = await self._tx_queue.get()
            try:
                char = server.get_characteristic(TX_CHAR)
                char.value = bytearray(frame.encode())
                server.update_value(CHAT_SVC, TX_CHAR)
            except Exception as exc:
                logging.debug(f"[Peripheral TX] {exc}")
            await asyncio.sleep(INTER_FRAME_DELAY)

    def _gatt_read(
        self, char: BlessGATTCharacteristic, **_
    ) -> bytearray:
        return bytearray()

    def _gatt_write(
        self, char: BlessGATTCharacteristic, value: Any, **_
    ) -> None:
        if char.uuid.lower() != RX_CHAR:
            return
        try:
            self._on_frame_from_thread(Frame.decode(bytes(value)))
        except Exception as exc:
            logging.debug(f"[Peripheral RX] {exc}")

    # ═══ CENTRAL SIDE ═════════════════════════════════════════════════════════

    async def _run_as_central(self, peer: BLEDevice) -> None:
        """
        Connect to the peripheral, subscribe to TX notifications,
        and write RX frames. Auto-reconnects with exponential backoff.
        """
        backoff = RECONNECT_BASE

        while True:
            self._ui.status(f"Connecting to {peer.name or peer.address}…", role="Central")
            self._handshake_sent = False

            try:
                async with BleakClient(
                    peer,
                    timeout=15.0,
                    disconnected_callback=self._on_ble_disconnect,
                ) as client:
                    self._client = client
                    backoff = RECONNECT_BASE  # reset on success

                    # Update TX chunk size to match negotiated MTU
                    eff_payload = max(1, client.mtu_size - 3 - HDR)
                    self._tx_coder = Coder(chunk_size=eff_payload)
                    logging.debug(f"MTU={client.mtu_size}, chunk={eff_payload}")

                    # Subscribe to peripheral's notifications
                    await client.start_notify(TX_CHAR, self._on_notify)

                    # Kick off the session with our handshake
                    await self._send_handshake()

                    # TX loop: drain queue and write to peripheral
                    while self._connected or not self._tx_queue.empty():
                        frame = await self._tx_queue.get()
                        try:
                            await client.write_gatt_char(
                                RX_CHAR,
                                bytearray(frame.encode()),
                                response=False,  # Write Without Response (faster)
                            )
                        except Exception as exc:
                            logging.debug(f"[Central TX] {exc}")
                            break
                        await asyncio.sleep(INTER_FRAME_DELAY)

            except Exception as exc:
                logging.debug(f"[Central] connection error: {exc}")

            self._connected = False
            self._handshake_sent = False
            self._ui.status(f"Reconnecting in {backoff:.0f}s…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    def _on_notify(self, _characteristic: Any, data: bytearray) -> None:
        """Called by bleak in the event-loop thread for each incoming notification."""
        try:
            self._on_frame_from_thread(Frame.decode(data))
        except Exception as exc:
            logging.debug(f"[Central notify] {exc}")

    def _on_ble_disconnect(self, _client: Any) -> None:
        self._connected = False
        self._ui.event("Link dropped — reconnecting…")

    # ═══ INPUT LOOP ═══════════════════════════════════════════════════════════

    async def _input_loop(self) -> None:
        """
        Reads stdin in a background daemon thread, dispatching each line
        to the event loop via a thread-safe queue. Using a thread avoids
        blocking the asyncio loop on stdin.readline().
        """
        line_q: asyncio.Queue[str] = asyncio.Queue()

        def _reader() -> None:
            while True:
                try:
                    raw = sys.stdin.readline()
                    if not raw:   # EOF
                        break
                    line_q.put_nowait(raw.rstrip("\n"))
                except Exception:
                    break

        threading.Thread(target=_reader, daemon=True).start()
        self._ui.prompt()

        typing_active = False

        while True:
            line = await line_q.get()
            line = line.strip()

            if not line:
                if typing_active and self._connected:
                    await self._send_typing(False)
                    typing_active = False
                self._ui.prompt()
                continue

            # ── commands ────────────────────────────────────────────────────
            if line == "/quit":
                self._ui.event("Bye!")
                sys.exit(0)

            elif line == "/ping":
                if self._connected:
                    await self.send_ping()
                else:
                    self._ui.error("Not connected yet")

            elif line.startswith("/name "):
                new_name = line[6:].strip()
                if new_name:
                    self._name    = new_name
                    self._ui.name = new_name
                    self._ui.event(f"You are now {BLD}{new_name}{RST}")
                    self._handshake_sent = False   # re-send with new name
                    if self._connected:
                        await self._send_handshake()

            elif line.startswith("/"):
                self._ui.error(f"Unknown command: {line}")

            # ── chat message ─────────────────────────────────────────────────
            else:
                if not self._connected:
                    self._ui.error("Not connected — waiting for peer")
                else:
                    if typing_active:
                        await self._send_typing(False)
                        typing_active = False
                    await self.send_message(line)

            self._ui.prompt()

    # ═══ MAIN ENTRY ═══════════════════════════════════════════════════════════

    async def run(self) -> None:
        """
        Full startup sequence:

        1. Print banner and start advertising (BlessServer).
        2. Simultaneously scan for a peer (BleakScanner).
        3. Negotiate role via priority comparison.
        4. Run as peripheral (keep server, start TX loop) or
           central (tear down server, connect as BleakClient).
        5. Concurrently run the user input loop.
        """
        self._loop = asyncio.get_running_loop()
        self._ui.banner()

        # Phase 1 — start advertising immediately so the peer can find us
        adv_server = BlessServer(name=self._adv_name)
        adv_server.read_request_func  = self._gatt_read
        adv_server.write_request_func = self._gatt_write

        await adv_server.add_new_service(CHAT_SVC)
        await adv_server.add_new_characteristic(
            CHAT_SVC, TX_CHAR,
            GATTCharacteristicProperties.notify,
            None, GATTAttributePermissions.readable,
        )
        await adv_server.add_new_characteristic(
            CHAT_SVC, RX_CHAR,
            GATTCharacteristicProperties.write,
            None, GATTAttributePermissions.writeable,
        )
        await adv_server.start()
        self._server = adv_server

        # Phase 2 — negotiate role while advertising
        role, peer_device = await self._negotiate_role()
        self._role = role

        if role == "peripheral":
            # Keep the existing BlessServer; add TX loop
            self._ui.status("Waiting for peer to connect…", role="Peripheral")
            await asyncio.gather(
                self._run_as_peripheral(),
                self._input_loop(),
            )
        else:
            # Tear down the advertising server; become a GATT client
            await adv_server.stop()
            self._server = None
            await asyncio.gather(
                self._run_as_central(peer_device),
                self._input_loop(),
            )


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Symmetric BLE peer-to-peer messenger.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--name", "-n",
        default="User",
        metavar="NAME",
        help="your display name (default: User)",
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="enable verbose BLE debug logging",
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # Silence noisy bleak/bless internals unless --debug
    if not args.debug:
        for logger_name in ("bleak", "bless", "dbus_fast"):
            logging.getLogger(logger_name).setLevel(logging.ERROR)

    chat = BLEChat(display_name=args.name)
    try:
        asyncio.run(chat.run())
    except KeyboardInterrupt:
        print(f"\n{DIM}Interrupted.{RST}")
    except RuntimeError as exc:
        print(f"\n{RED}Error: {exc}{RST}")
        sys.exit(1)


if __name__ == "__main__":
    main()