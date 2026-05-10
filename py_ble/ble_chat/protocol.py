from ble_chat.constants import CHUNK_SIZE
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable

class MsgType(IntEnum):
    HANDSHAKE   = 0x00
    CHAT        = 0x01
    ACK         = 0x02
    PING        = 0x03
    PONG        = 0x04
    TYPING_ON   = 0x05
    TYPING_OFF  = 0x06
    GOODBYE     = 0x07


HEADER_SIZE = 4 # bytes consumed by every packet's header


@dataclass
class Packet:
    """
    Single BLE-layer transmission unit.

    Wire layout:
        [0] msg_type    (1 byte)
        [1] msg_id      (1 byte)
        [2] chunk_idx   (1 byte)
        [3] n_chunks    (1 byte)
        [4:] payload    (0 - CHUNK_SIZE bytes)
    """
    msg_type: MsgType
    msg_id: int
    chunk_idx: int
    n_chunks: int
    payload: bytes

    def encode(self) -> bytes:
        return bytes([self.msg_type, self.msg_id, self.chunk_idx, self.n_chunks]) + self.payload
    
    @staticmethod
    def decode(raw: bytes | bytearray) -> "Packet":
        raw = bytes(raw)
        if len(raw) < HEADER_SIZE:
            raise ValueError(f"Packet too short ({len(raw)} bytes)")
        return Packet(
            msg_type    = MsgType(raw[0]),
            msg_id      = raw[1],
            chunk_idx   = raw[2],
            n_chunks    = raw[3],
            payload     = raw[HEADER_SIZE:],
        )

@dataclass
class Message:
    """A fully reassembled application-layer message."""
    msg_type:   MsgType
    msg_id:     int
    payload:    bytes
    received_at: float = field(default_factory=time.monotonic)

def build_packets(msg_type: MsgType, msg_id: int, payload: bytes | str) -> list[bytes]:
    """
    Encode an application message into one or more BLE-sized packets.

    If the payload fits ina single CHUNK_SIZE window, one packet is returned.
    Otherwise the payload is split into sequential chunks that the remote Reassembler will stitch back together in order.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    
    # Always produce at least one packet, even for empty payloads
    windows = [payload[i : i + CHUNK_SIZE] for i in range(0, max(1, len(payload)), CHUNK_SIZE)]
    n = len(windows)
    return [
        Packet(msg_type, msg_id, idx, n, chunk).encode()
        for idx, chunk in enumerate(windows)
    ]

class Reassembler:
    """
    Buffers incoming packets by (msg_id, chunk_idx) and fires `on_complete` when every chunk of a message has been received.

    Handles:
        * Out-of-order chunk arrival (BLE does not guarantee order across writes)
        * Independent reassembly streams for concurrent msg_ids
        * Silently drops malformed packets
    
    NOTE: Must be called exclusively from the asyncio event loop. The BLE node classes ensure this via call_soon_threadsafe.
    """

    def __init__(self, on_complete: Callable[[Message], None]):
        self._on_complete = on_complete
        self._chunks: dict[int, dict[int, bytes]] = defaultdict(dict)
        self._n_chunks: dict[int, int] = {}
    
    def feed(self, raw: bytes):
        try:
            pkt = Packet.decode(raw)
        except Exception:
            return
        
        self._chunks[pkt.msg_id][pkt.chunk_idx] = pkt.payload
        self._n_chunks[pkt.msg_id] = pkt.n_chunks

        if len(self._chunks[pkt.msg_id]) == pkt.n_chunks:
            # All chunks present - reassemble in chunk_idx order
            payload = b"".join(
                self._chunks[pkt.msg_id][i] for i in range(pkt.n_chunks)
            )
            msg_type = pkt.msg_type # same for all chunks of a message
            msg_id = pkt.msg_id
            del self._chunks[pkt.msg_id]
            del self._n_chunks[pkt.msg_id]
            self._on_complete(Message(msg_type, msg_id, payload))