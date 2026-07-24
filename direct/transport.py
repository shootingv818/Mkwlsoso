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
