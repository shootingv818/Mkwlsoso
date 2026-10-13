"""MTProto 2.0 message envelope (encrypt/decrypt) for the direct client.

This assembles the plaintext message
    server_salt(8) | session_id(8) | msg_id(8) | seq_no(4) | length(4) | body | padding
computes the msg_key, derives the AES key/iv (crypto.kdf), and wraps it as the
on-the-wire encrypted payload:
    auth_key_id(8) | msg_key(16) | ige(encrypted plaintext)

Decrypt reverses it and VERIFIES the recomputed msg_key (a security invariant).
This is transport-independent; the HTTPS/socket transport is added and verified
live in a later step.
"""

from __future__ import annotations

import os
import struct
import time

from . import crypto
from .errors import SecurityError


def gen_msg_id(last: int = 0) -> int:
    """Monotonic message id derived from the current time (Unix<<32)."""
    now = time.time()
    msg_id = int(now) << 32 | (int((now % 1) * (1 << 32)) & 0xFFFFFFFC)
    if msg_id <= last:
        msg_id = last + 4
    return msg_id


def _pad_len(body_len: int) -> int:
    # MTProto 2.0: 12..1024 random padding bytes, total a multiple of 16.
    min_pad = 12
    total = body_len + min_pad
    extra = (-total) % 16
    return min_pad + extra


def encrypt_message(auth_key: bytes, server_salt: bytes, session_id: bytes,
                    msg_id: int, seq_no: int, body: bytes,
                    from_client: bool = True) -> bytes:
    """Build the encrypted MTProto 2.0 payload for `body` (already TL-serialized)."""
    if len(auth_key) != 256:
        raise SecurityError("auth_key must be 256 bytes")
    if len(server_salt) != 8 or len(session_id) != 8:
        raise SecurityError("server_salt and session_id must be 8 bytes")

    inner = (server_salt + session_id + struct.pack("<q", msg_id)
             + struct.pack("<i", seq_no) + struct.pack("<i", len(body)) + body)
    inner += os.urandom(_pad_len(len(inner)))

    mkey = crypto.msg_key(auth_key, inner, from_client=from_client)
    aes_key, aes_iv = crypto.kdf(auth_key, mkey, from_client=from_client)
    enc = crypto.ige_encrypt(inner, aes_key, aes_iv)
    return crypto.auth_key_id(auth_key) + mkey + enc


def decrypt_message(auth_key: bytes, payload: bytes,
                    from_client: bool = False) -> dict:
    """Decrypt an incoming payload and verify the msg_key. from_client=False
    means server->client (the usual direction for responses).

    Returns {salt, session_id, msg_id, seq_no, body}.
    """
    if len(auth_key) != 256:
        raise SecurityError("auth_key must be 256 bytes")
    if len(payload) < 24:
        raise SecurityError("payload too short")

    key_id, mkey, enc = payload[:8], payload[8:24], payload[24:]
    if key_id != crypto.auth_key_id(auth_key):
        raise SecurityError("auth_key_id mismatch")
    if len(enc) % 16:
        raise SecurityError("encrypted body not block-aligned")

    aes_key, aes_iv = crypto.kdf(auth_key, mkey, from_client=from_client)
    inner = crypto.ige_decrypt(enc, aes_key, aes_iv)

    # Verify the message key was computed over exactly this plaintext.
    if crypto.msg_key(auth_key, inner, from_client=from_client) != mkey:
        raise SecurityError("msg_key mismatch (tampered or wrong key)")

    salt = inner[0:8]
    session_id = inner[8:16]
    msg_id = struct.unpack("<q", inner[16:24])[0]
    seq_no = struct.unpack("<i", inner[24:28])[0]
    length = struct.unpack("<i", inner[28:32])[0]
    body = inner[32:32 + length]
    if len(body) != length:
        raise SecurityError("declared body length exceeds payload")
    return {"salt": salt, "session_id": session_id, "msg_id": msg_id,
            "seq_no": seq_no, "body": body}
