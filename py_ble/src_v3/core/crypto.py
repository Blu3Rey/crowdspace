"""
core/crypto.py
==============
Cryptography layer for BLE Mesh Network.

Provides:
  • AES-256-GCM authenticated encryption / decryption
  • ECDH (X25519) key exchange for session keys
  • Network-wide shared key (PSK) for broadcast encryption
  • Per-peer session keys derived via ECDH
  • HKDF key derivation
"""

from __future__ import annotations
import os
import hashlib
import hmac
import struct
import time
from typing import Optional, Dict, Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.exceptions import InvalidTag

from .packet import Packet, PacketFlag, TAG_SIZE


# ── Key Manager ───────────────────────────────────────────────────────────────

class KeyManager:
    """
    Manages all cryptographic material for a mesh node.

    Key hierarchy:
      network_key  →  shared PSK for broadcast / group traffic
      session_key  →  per-peer key derived via ECDH + HKDF
    """

    NONCE_SIZE   = 12   # 96-bit nonce for AES-GCM
    KEY_SIZE     = 32   # 256-bit keys

    def __init__(self, network_key: Optional[bytes] = None):
        # Generate or use provided network-wide PSK
        self._network_key: bytes = network_key or os.urandom(self.KEY_SIZE)

        # ECDH identity key pair (X25519)
        self._private_key = X25519PrivateKey.generate()
        self._public_key  = self._private_key.public_key()

        # Peer session keys: addr → bytes
        self._session_keys: Dict[bytes, bytes] = {}

        # Replay-protection: (src, seq) → timestamp
        self._seen_nonces: Dict[Tuple[bytes, int], float] = {}
        self._nonce_ttl   = 60.0   # seconds before evicting old nonces

    # ── Identity / Key Exchange ───────────────────────────────────────────────

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte X25519 public key for advertising / exchange."""
        return self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def derive_session_key(self, peer_addr: bytes, peer_pubkey_bytes: bytes) -> bytes:
        """
        Perform X25519 ECDH and derive a 256-bit session key via HKDF.
        Stores the session key and returns it.
        """
        peer_pubkey = X25519PublicKey.from_public_bytes(peer_pubkey_bytes)
        shared      = self._private_key.exchange(peer_pubkey)

        # HKDF with both addrs as info for domain separation
        info = b"ble-mesh-session-v1:" + self.public_key_bytes + peer_pubkey_bytes
        hkdf = HKDF(
            algorithm = hashes.SHA256(),
            length    = self.KEY_SIZE,
            salt      = None,
            info      = info,
        )
        session_key = hkdf.derive(shared)
        self._session_keys[peer_addr] = session_key
        return session_key

    def set_session_key(self, peer_addr: bytes, key: bytes):
        self._session_keys[peer_addr] = key

    def get_session_key(self, peer_addr: bytes) -> Optional[bytes]:
        return self._session_keys.get(peer_addr)

    @property
    def network_key(self) -> bytes:
        return self._network_key

    # ── Encryption ────────────────────────────────────────────────────────────

    def encrypt_packet(self, pkt: Packet, use_session: bool = False) -> Packet:
        """
        Encrypt packet payload in-place using AES-256-GCM.

        For unicast packets, prefers the session key for the destination.
        For broadcast/group, uses the network key.
        Falls back to network key when no session key exists.
        """
        if PacketFlag.ENCRYPTED in pkt.flags:
            return pkt   # already encrypted

        key = self._resolve_key(pkt, use_session)
        if key is None:
            return pkt   # no key available; send plaintext

        nonce = self._make_nonce(pkt)
        aad   = self._make_aad(pkt)   # authenticated additional data

        aesgcm          = AESGCM(key)
        ciphertext_tag  = aesgcm.encrypt(nonce, pkt.payload, aad)
        ciphertext      = ciphertext_tag[:-16]
        tag             = ciphertext_tag[-16:]

        pkt.payload = ciphertext
        pkt.tag     = tag
        pkt.flags  |= PacketFlag.ENCRYPTED
        return pkt

    def decrypt_packet(self, pkt: Packet, peer_addr: Optional[bytes] = None) -> Optional[Packet]:
        """
        Decrypt packet payload.  Returns None on authentication failure.
        Automatically performs replay-attack protection.
        """
        if PacketFlag.ENCRYPTED not in pkt.flags:
            return pkt   # plaintext

        if self._is_replay(pkt):
            return None

        key = self._resolve_key(pkt, peer_addr=peer_addr or pkt.src_addr)
        if key is None:
            return None

        nonce  = self._make_nonce(pkt)
        aad    = self._make_aad(pkt)
        aesgcm = AESGCM(key)

        try:
            plaintext    = aesgcm.decrypt(nonce, pkt.payload + pkt.tag, aad)
            pkt.payload  = plaintext
            pkt.flags   &= ~PacketFlag.ENCRYPTED
            self._record_nonce(pkt)
            return pkt
        except InvalidTag:
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_key(
        self,
        pkt:         Packet,
        use_session: bool          = False,
        peer_addr:   Optional[bytes] = None,
    ) -> Optional[bytes]:
        addr = peer_addr or pkt.src_addr
        if use_session or (not pkt.is_broadcast and addr in self._session_keys):
            return self._session_keys.get(addr) or self._network_key
        return self._network_key

    def _make_nonce(self, pkt: Packet) -> bytes:
        """
        96-bit deterministic nonce = src_addr(6) + seq_num(4) + frag_idx(1) + 0-pad(1).
        Uniqueness is guaranteed by the seq_num counter.
        """
        return pkt.src_addr + struct.pack("<IBB", pkt.seq_num, pkt.frag_idx, 0)

    # def _make_aad(self, pkt: Packet) -> bytes:
    #     """Additional authenticated data covers routing fields."""
    #     return (
    #         bytes([int(pkt.ptype)])
    #         + pkt.src_addr
    #         + pkt.dst_addr
    #         + struct.pack("<IIBB", pkt.group_id, pkt.seq_num, pkt.ttl, int(pkt.flags))
    #     )
    def _make_aad(self, pkt: Packet) -> bytes:
        return (
            bytes([int(pkt.ptype)])
            + pkt.src_addr
            + pkt.dst_addr
            + struct.pack("<II", pkt.group_id, pkt.seq_num)
        )

    def _is_replay(self, pkt: Packet) -> bool:
        self._evict_old_nonces()
        return pkt.cache_key in self._seen_nonces

    def _record_nonce(self, pkt: Packet):
        self._seen_nonces[pkt.cache_key] = time.monotonic()

    def _evict_old_nonces(self):
        cutoff = time.monotonic() - self._nonce_ttl
        stale  = [k for k, t in self._seen_nonces.items() if t < cutoff]
        for k in stale:
            del self._seen_nonces[k]


# ── Utility functions ─────────────────────────────────────────────────────────

def generate_network_key() -> bytes:
    """Generate a new 256-bit network pre-shared key."""
    return os.urandom(32)

def derive_group_key(network_key: bytes, group_id: int) -> bytes:
    """Derive a deterministic group key from the network key and group ID."""
    info = b"ble-mesh-group-v1:" + struct.pack("<I", group_id)
    hkdf = HKDF(
        algorithm = hashes.SHA256(),
        length    = 32,
        salt      = None,
        info      = info,
    )
    return hkdf.derive(network_key)