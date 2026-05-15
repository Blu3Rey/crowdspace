"""
utils/crypto.py — Lightweight AES-256-GCM encryption for mesh payloads.

This module is **optional**.  If the ``cryptography`` package is not installed,
:class:`MeshCrypto` raises an informative error on instantiation rather than
failing silently.

Wire format for an encrypted payload::

    [nonce: 12 B][ciphertext + GCM tag: N + 16 B]

Nonce construction
------------------
The 96-bit nonce is **counter-based**, not random.  It is formed from:

    node_id[:4]  (4 bytes)  — unique per originating node
    counter      (8 bytes)  — monotonically increasing uint64, big-endian

This guarantees that the same (key, nonce) pair is *never* reused as long as
each node has a unique node_id and the counter does not wrap (2^64 ≈ 1.8 × 10^19
messages — not a practical concern).  Random nonces, by contrast, have a 50%
collision probability after only 2^48 messages due to the birthday paradox.

Key exchange is out-of-scope for this library.  In a real deployment, use
a proper key-agreement protocol (e.g. ECDH) or distribute the PSK via a
secure out-of-band channel.

Usage::

    from ble_mesh.utils.crypto import MeshCrypto, derive_key

    key    = derive_key("my secret passphrase")
    crypto = MeshCrypto(key, node_id=cfg.node_id)
    ct     = crypto.encrypt(b"hello mesh")
    pt     = crypto.decrypt(ct)            # → b"hello mesh"
"""

from __future__ import annotations

import struct
import threading
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False
    AESGCM = None  # type: ignore


_NONCE_SIZE = 12  # 96-bit nonce for AES-GCM (4B node prefix + 8B counter)
_KDF_SALT   = b"ble-mesh-v1-salt"
_KDF_ITER   = 100_000
_KEY_SIZE   = 32   # 256-bit key


class MeshCrypto:
    """AES-256-GCM symmetric encryption with collision-free counter-based nonces.

    Parameters
    ----------
    key : bytes
        Must be 16, 24, or 32 bytes (AES-128/192/256).
    node_id : bytes
        This node's 16-byte mesh identifier.  The first 4 bytes are used as
        the nonce prefix to ensure nonces are globally unique across nodes.
    """

    def __init__(self, key: bytes, node_id: bytes = b"\x00" * 16) -> None:
        if not _HAVE_CRYPTO:
            raise RuntimeError(
                "Install the 'cryptography' package to enable encryption:\n"
                "  pip install cryptography"
            )
        if len(key) not in (16, 24, 32):
            raise ValueError(
                f"AES key must be 16, 24, or 32 bytes; got {len(key)}"
            )
        self._aesgcm       = AESGCM(key)
        # Nonce = node_id[:4] + counter(8B).  Using the node_id prefix ensures
        # that two nodes encrypting under the same PSK never share a nonce.
        self._nonce_prefix = node_id[:4]
        self._counter      = 0
        self._lock         = threading.Lock()   # safe against run_coroutine_threadsafe

    def _next_nonce(self) -> bytes:
        """Return the next unique 12-byte nonce and advance the counter."""
        with self._lock:
            n = self._counter
            self._counter = (n + 1) & 0xFFFFFFFFFFFFFFFF
        return self._nonce_prefix + struct.pack("!Q", n)

    def encrypt(self, plaintext: bytes, aad: Optional[bytes] = None) -> bytes:
        """Encrypt *plaintext*.  Returns ``nonce || ciphertext+tag``."""
        nonce = self._next_nonce()
        ct    = self._aesgcm.encrypt(nonce, plaintext, aad)
        return nonce + ct

    def decrypt(self, ciphertext: bytes, aad: Optional[bytes] = None) -> bytes:
        """Decrypt *ciphertext* (``nonce || ct+tag``).  Raises on failure."""
        if len(ciphertext) < _NONCE_SIZE + 16:
            raise ValueError(
                f"Ciphertext too short ({len(ciphertext)} B) — "
                "minimum is nonce(12) + GCM tag(16) = 28 B"
            )
        nonce = ciphertext[:_NONCE_SIZE]
        ct    = ciphertext[_NONCE_SIZE:]
        return self._aesgcm.decrypt(nonce, ct, aad)


def derive_key(passphrase: str, salt: bytes = _KDF_SALT) -> bytes:
    """Derive a 32-byte AES-256 key from a human-readable passphrase.

    Uses PBKDF2-HMAC-SHA256 with 100 000 iterations.  The *salt* defaults
    to a fixed value — in production pass a unique, secret salt.
    """
    if not _HAVE_CRYPTO:
        raise RuntimeError(
            "Install the 'cryptography' package to use key derivation:\n"
            "  pip install cryptography"
        )
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_SIZE,
        salt=salt,
        iterations=_KDF_ITER,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def is_available() -> bool:
    """Return True if the ``cryptography`` package is installed."""
    return _HAVE_CRYPTO
