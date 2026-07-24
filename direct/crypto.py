"""MTProto 2.0 crypto primitives for the direct client.

Built on the single-block AES in aes.py. Provides AES-IGE (the mode MTProto
uses for the encrypted message body), the SHA-256 key-derivation function that
turns (auth_key, msg_key) into (aes_key, aes_iv), plus auth_key_id and msg_key
helpers. Pure/deterministic and unit-testable offline.
"""

from __future__ import annotations

import hashlib

from . import aes


def sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ---- AES-IGE ------------------------------------------------------------
def ige_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-256-IGE encrypt. `data` must be a multiple of 16 bytes; iv is 32 bytes."""
    if len(data) % 16:
        raise ValueError("IGE data must be a multiple of 16 bytes")
    if len(iv) != 32:
        raise ValueError("IGE iv must be 32 bytes")
    iv1, iv2 = iv[:16], iv[16:]
    out = bytearray()
    prev_c, prev_p = iv1, iv2
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        x = bytes(a ^ b for a, b in zip(block, prev_c))
        enc = aes.ecb_encrypt(key, x)
        c = bytes(a ^ b for a, b in zip(enc, prev_p))
        out += c
        prev_c, prev_p = c, block
    return bytes(out)


def ige_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-256-IGE decrypt. `data` must be a multiple of 16 bytes; iv is 32 bytes."""
    if len(data) % 16:
        raise ValueError("IGE data must be a multiple of 16 bytes")
    if len(iv) != 32:
        raise ValueError("IGE iv must be 32 bytes")
    iv1, iv2 = iv[:16], iv[16:]
    out = bytearray()
    prev_c, prev_p = iv1, iv2
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        x = bytes(a ^ b for a, b in zip(block, prev_p))
        dec = aes.ecb_decrypt(key, x)
        p = bytes(a ^ b for a, b in zip(dec, prev_c))
        out += p
        prev_c, prev_p = block, p
    return bytes(out)


# ---- auth key helpers ---------------------------------------------------
def auth_key_id(auth_key: bytes) -> bytes:
    """Lower 64 bits of SHA1(auth_key) — the 8-byte key identifier on the wire."""
    if len(auth_key) != 256:
        raise ValueError("auth_key must be 256 bytes")
    return sha1(auth_key)[-8:]


# ---- MTProto 2.0 message-key + KDF -------------------------------------
def msg_key(auth_key: bytes, plaintext: bytes, from_client: bool) -> bytes:
    """msg_key = middle 128 bits of SHA256(substr(auth_key, 88+x, 32) + plaintext).

    x = 0 for client->server, 8 for server->client.
    """
    x = 0 if from_client else 8
    return sha256(auth_key[88 + x:88 + x + 32] + plaintext)[8:24]


def kdf(auth_key: bytes, msg_key_: bytes, from_client: bool) -> tuple[bytes, bytes]:
    """Derive (aes_key, aes_iv) from auth_key + msg_key per MTProto 2.0.

    x = 0 for client->server, 8 for server->client.
    """
    x = 0 if from_client else 8
    sha256_a = sha256(msg_key_ + auth_key[x:x + 36])
    sha256_b = sha256(auth_key[40 + x:40 + x + 36] + msg_key_)
    aes_key = sha256_a[:8] + sha256_b[8:24] + sha256_a[24:32]
    aes_iv = sha256_b[:8] + sha256_a[8:24] + sha256_b[24:32]
    return aes_key, aes_iv
