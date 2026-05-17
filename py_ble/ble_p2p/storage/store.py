"""
storage/store.py — SQLite-backed persistence layer.

Responsibilities
----------------
1. Peer registry (long-term): stores peer info across restarts.
2. Outbound message queue: persists undelivered messages so they survive
   process restarts.  The Node drains the queue when a peer comes online.

Design notes
------------
- Uses WAL mode for better concurrent read performance.
- All operations are synchronous (run in executor by the Node if needed).
- Each outbound packet is stored as a raw BLE frame blob so the Node can
  write it directly to the characteristic without re-serialising.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..constants import CONFIG_DIR

log = logging.getLogger(__name__)

_DB_PATH = CONFIG_DIR / "messages.db"


# ─────────────────────────────────────────────────────────────
# DTOs
# ─────────────────────────────────────────────────────────────
@dataclass
class QueuedPacket:
    """One serialised BLE frame waiting to be delivered to dst_id."""
    row_id      : int
    dst_id_hex  : str
    frame       : bytes      # raw wire bytes, ready to write to GATT
    msg_id      : str        # logical message identifier (for ACK correlation)
    created_at  : float
    retry_count : int


@dataclass
class StoredPeer:
    device_id_hex: str
    name         : str
    capabilities : int
    ble_address  : str
    last_seen    : float


# ─────────────────────────────────────────────────────────────
# MessageStore
# ─────────────────────────────────────────────────────────────
class MessageStore:
    """
    Thread-safe SQLite store.

    The connection is opened once and kept alive for the process lifetime.
    SQLite's `check_same_thread=False` is used because writes are serialised
    by the Node's single asyncio event loop (via run_in_executor), not by
    multiple OS threads.
    """

    def __init__(self, db_path: Path = _DB_PATH):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()
        log.info("MessageStore opened: %s", db_path)

    # ── Schema ────────────────────────────────────────────────

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS outbound_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                dst_id_hex  TEXT    NOT NULL,
                frame       BLOB    NOT NULL,
                msg_id      TEXT    NOT NULL DEFAULT '',
                created_at  REAL    NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                delivered   INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_oq_dst
                ON outbound_queue(dst_id_hex, delivered);

            CREATE TABLE IF NOT EXISTS peers (
                device_id_hex TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                capabilities  INTEGER NOT NULL DEFAULT 0,
                ble_address   TEXT NOT NULL DEFAULT '',
                last_seen     REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS received_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                src_id_hex  TEXT NOT NULL,
                msg_type    INTEGER NOT NULL,
                feature_id  INTEGER,
                payload_hex TEXT,
                timestamp   REAL NOT NULL
            );
        """)
        self._conn.commit()

    # ── Outbound Queue ────────────────────────────────────────

    def enqueue_frame(self, dst_id_hex: str, frame: bytes, msg_id: str = "") -> int:
        """
        Persist one raw BLE frame for later delivery.  Returns the row id.
        """
        cur = self._conn.execute(
            "INSERT INTO outbound_queue (dst_id_hex, frame, msg_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (dst_id_hex, frame, msg_id, time.time()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_pending_frames(self, dst_id_hex: str, limit: int = 50) -> List[QueuedPacket]:
        """Retrieve undelivered frames for a peer, ordered oldest-first."""
        rows = self._conn.execute(
            "SELECT id, dst_id_hex, frame, msg_id, created_at, retry_count "
            "FROM outbound_queue "
            "WHERE dst_id_hex=? AND delivered=0 "
            "ORDER BY created_at ASC LIMIT ?",
            (dst_id_hex, limit),
        ).fetchall()
        return [QueuedPacket(*r) for r in rows]

    def get_pending_for_broadcast(self, limit: int = 50) -> List[QueuedPacket]:
        """Retrieve broadcast-addressed frames (dst_id_hex = '0000000000000000')."""
        return self.get_pending_frames("0000000000000000", limit)

    def mark_delivered(self, row_ids: List[int]):
        """Mark a batch of frame rows as delivered."""
        if not row_ids:
            return
        placeholders = ",".join("?" * len(row_ids))
        self._conn.execute(
            f"UPDATE outbound_queue SET delivered=1 WHERE id IN ({placeholders})",
            row_ids,
        )
        self._conn.commit()

    def increment_retries(self, row_id: int):
        self._conn.execute(
            "UPDATE outbound_queue SET retry_count=retry_count+1 WHERE id=?",
            (row_id,),
        )
        self._conn.commit()

    def purge_old_delivered(self, older_than_s: float = 3600.0):
        """Housekeeping: delete delivered rows older than *older_than_s* seconds."""
        cutoff = time.time() - older_than_s
        self._conn.execute(
            "DELETE FROM outbound_queue WHERE delivered=1 AND created_at<?",
            (cutoff,),
        )
        self._conn.commit()

    def purge_expired_undelivered(self, older_than_s: float = 3600.0):
        """Discard messages that were never delivered and are now too old."""
        cutoff = time.time() - older_than_s
        self._conn.execute(
            "DELETE FROM outbound_queue WHERE delivered=0 AND created_at<?",
            (cutoff,),
        )
        self._conn.commit()

    # ── Peer Registry ─────────────────────────────────────────

    def upsert_peer(
        self,
        device_id_hex: str,
        name         : str,
        capabilities : int,
        ble_address  : str,
    ):
        self._conn.execute(
            """
            INSERT INTO peers (device_id_hex, name, capabilities, ble_address, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(device_id_hex) DO UPDATE SET
                name=excluded.name,
                capabilities=excluded.capabilities,
                ble_address=excluded.ble_address,
                last_seen=excluded.last_seen
            """,
            (device_id_hex, name, capabilities, ble_address, time.time()),
        )
        self._conn.commit()

    def get_all_peers(self) -> List[StoredPeer]:
        rows = self._conn.execute(
            "SELECT device_id_hex, name, capabilities, ble_address, last_seen "
            "FROM peers ORDER BY last_seen DESC"
        ).fetchall()
        return [StoredPeer(*r) for r in rows]

    # ── Received log ──────────────────────────────────────────

    def log_received(
        self,
        src_id_hex  : str,
        msg_type    : int,
        feature_id  : Optional[int] = None,
        payload_hex : Optional[str] = None,
    ):
        self._conn.execute(
            "INSERT INTO received_log (src_id_hex, msg_type, feature_id, payload_hex, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (src_id_hex, msg_type, feature_id, payload_hex, time.time()),
        )
        self._conn.commit()

    # ── Lifecycle ─────────────────────────────────────────────

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass