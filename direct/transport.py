"""HTTPS transport for MTProto (the browser-free wire).

Eitaa Web (tweb) uses MTProto's HTTP transport: the encrypted payload
(auth_key_id | msg_key | ige_body) is POSTed as the raw HTTP request body to
the DC URL, and the response body is the encrypted reply in the same format.
No socket obfuscation is used for the HTTP transport.

Uses only the Python standard library (http.client + ssl) so the direct client
has no hard third-party dependency for networking.
"""

from __future__ import annotations

import ssl
from urllib.parse import urlparse
import http.client

from .errors import TransportError

# ---------------------------------------------------------------------------
# Eitaa transport envelope (CONFIRMED by worker capture, all requests share it)
#
#   ed77be7a                 4-byte constant magic
#   <1 byte>  len1           length of the ASCII routing token
#   <len1 B>  token1         e.g. "9179.c756a2d10f.e41c4e_<userid>"  (session/route token)
#   <1 byte>  len2           length of the ASCII session-instance id
#   <len2 B>  token2         e.g. "mrtpgmi2y9fm222__web"            (client session id)
#   <4 bytes> bodyLen (BE)   big-endian length of the payload that follows
#   <bodyLen> body           the (plaintext) TL payload — NOT AES-encrypted
#   0000008700000020000000   11-byte constant trailer (contains layer=135, 32)
#
# The body being plaintext (msg_ids in an ack request matched the ack response
# byte-for-byte) means this transport needs NO auth_key / AES-IGE: auth is the
# token. We only replicate the envelope and serialize the TL body.
# ---------------------------------------------------------------------------
EITAA_MAGIC = bytes.fromhex("ed77be7a")
EITAA_TRAILER = bytes.fromhex("0000008700000020000000")


def _as_bytes(tok) -> bytes:
    if isinstance(tok, bytes):
        return tok
    return str(tok).encode("ascii")


def wrap_eitaa(token1, token2, body: bytes) -> bytes:
    """Build the exact on-wire request Eitaa's worker sends."""
    t1 = _as_bytes(token1)
    t2 = _as_bytes(token2)
    if len(t1) > 255 or len(t2) > 255:
        raise TransportError("eitaa token too long for a 1-byte length prefix")
    return (
        EITAA_MAGIC
        + bytes([len(t1)]) + t1
        + bytes([len(t2)]) + t2
        + len(body).to_bytes(4, "big")
        + body
        + EITAA_TRAILER
    )


def unwrap_eitaa(raw: bytes) -> dict:
    """Parse an Eitaa envelope (request or response-shaped) into its fields.

    Returns {token1, token2, body, trailer, ok}. Raises TransportError on a
    structurally invalid envelope.
    """
    if raw[:4] != EITAA_MAGIC:
        raise TransportError("not an eitaa envelope (bad magic)")
    p = 4
    l1 = raw[p]; p += 1
    t1 = raw[p:p + l1]; p += l1
    l2 = raw[p]; p += 1
    t2 = raw[p:p + l2]; p += l2
    body_len = int.from_bytes(raw[p:p + 4], "big"); p += 4
    body = raw[p:p + body_len]; p += body_len
    trailer = raw[p:]
    return {
        "token1": t1.decode("ascii", "replace"),
        "token2": t2.decode("ascii", "replace"),
        "body": body,
        "trailer": trailer,
        "ok": len(body) == body_len,
    }


class HttpTransport:
    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout
        p = urlparse(url)
        self.host = p.hostname or ""
        self.port = p.port or (443 if p.scheme == "https" else 80)
        self.path = p.path or "/"
        self.secure = p.scheme != "http"

    def _conn(self) -> http.client.HTTPConnection:
        if self.secure:
            ctx = ssl.create_default_context()
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def post(self, payload: bytes) -> bytes:
        """POST the raw MTProto payload; return the raw response bytes."""
        conn = self._conn()
        try:
            headers = {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(payload)),
                "Connection": "keep-alive",
            }
            conn.request("POST", self.path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                raise TransportError(f"HTTP {resp.status} {resp.reason} "
                                     f"({len(data)} bytes)")
            return data
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"transport POST failed: {exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
