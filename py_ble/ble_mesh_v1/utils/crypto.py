"""
utils/crypto.py — Lightweight AES-256-GCM encryption for mesh payloads.

This module is **optional**.  If the ``cryptography`` package is not installed,
:class:`MeshCrypto` raises an informative error on instantiation rather than
failing silently.

Wire format for an encrypted payload::

    [nonce: 12 B][ciphertext + GCM tag: N + 16 B]

The 96-bit nonce is freshly generated for every encryption call, which
guarantees semantic security as long as the same key is not used for more
than 2^32 messages (≈ 4 billion — not a practical concern here).

Key exchange is out-of-scope for this library.  In a real deployment, use
a proper key-agreement protocol (e.g. ECDH) or distribute the PSK via a
secure out-of-band channel.

Usage::

    from ble_mesh.utils.crypto import MeshCrypto, derive_key

    key    = derive_key("my secret passphrase")
    crypto = MeshCrypto(key)
    ct     = crypto.encrypt(b"hello mesh")
    pt     = crypto.decrypt(ct)            # → b"hello mesh"
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    _HAVE_CRYPTO = True
except ImportError:
    _HAVE_CRYPTO = False
    AESGCM = None  # type: ignore


_NONCE_SIZE = 12  # 96-bit nonce for AES-GCM
_KDF_SALT   = b"ble-mesh-v1-salt"
_KDF_ITER   = 100_000
_KEY_SIZE   = 32   # 256-bit key


class MeshCrypto:
    """AES-256-GCM symmetric encryption.

    Parameters
    ----------
    key : bytes
        Must be 16, 24, or 32 bytes (AES-128/192/256).
    """

    def __init__(self, key: bytes) -> None:
        if not _HAVE_CRYPTO:
            raise RuntimeError(
                "Install the 'cryptography' package to enable encryption:\n"
                "  pip install cryptography"
            )
        if len(key) not in (16, 24, 32):
            raise ValueError(
                f"AES key must be 16, 24, or 32 bytes; got {len(key)}"
            )
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: bytes, aad: Optional[bytes] = None) -> bytes:
        """Encrypt *plaintext*.  Returns ``nonce || ciphertext+tag``."""
        nonce = os.urandom(_NONCE_SIZE)
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