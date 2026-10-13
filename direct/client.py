"""DirectClient — browser-free MTProto client for Eitaa.

Loads the session exported from the browser (session.load_export), connects to
the home DC over HTTPS (transport.HttpTransport), and invokes API methods with
the existing auth_key: it builds the encrypted envelope (mtproto.encrypt_message),
POSTs it, decrypts the reply (mtproto.decrypt_message) and parses the MTProto
service messages (service.parse_body). Handles server-salt correction
(bad_server_salt) and msg-id/seq bookkeeping.

This is the first end-to-end path; it is exercised live by the CLI `direct-probe`
against the user's authorized account. Deterministic pieces are unit-tested.
"""

from __future__ import annotations

import os
import struct

from . import dc as dccfg
from . import mtproto, schema, service
from .errors import DirectError, RpcError, SecurityError, rpc_error_from
from .session import Session, load_export
from .transport import HttpTransport


class DirectClient:
    def __init__(self, session: Session, url: str | None = None,
                 api_id: int = 0, api_hash: str = "", timeout: float = 30.0) -> None:
        self.session = session
        self.url = url or dccfg.dc_url(session.dc_id)
        aid, ah = dccfg.resolve_api_creds()
        self.api_id = api_id or aid
        self.api_hash = api_hash or ah
        self.transport = HttpTransport(self.url, timeout=timeout)
        self.session_id = os.urandom(8)
        self._salt = session.server_salt or (b"\x00" * 8)
        self._last_msg_id = 0
        self._seq_no = 0
        self._inited = False

    # ---- id/seq bookkeeping ---------------------------------------------
    def _next_msg_id(self) -> int:
        mid = mtproto.gen_msg_id(self._last_msg_id)
        self._last_msg_id = mid
        return mid

    def _next_seq(self, content_related: bool = True) -> int:
        if content_related:
            seq = self._seq_no * 2 + 1
            self._seq_no += 1
            return seq
        return self._seq_no * 2

    # ---- low-level single round-trip ------------------------------------
    def _round_trip(self, query: bytes) -> list[dict]:
        if len(self.session.auth_key) != 256:
            raise SecurityError("session has no valid 256-byte auth_key")
        msg_id = self._next_msg_id()
        seq = self._next_seq(True)
        payload = mtproto.encrypt_message(
            self.session.auth_key, self._salt, self.session_id,
            msg_id, seq, query, from_client=True)
        resp = self.transport.post(payload)
        if len(resp) < 24:
            # Could be a plaintext transport error (e.g. 4-byte error code).
            if len(resp) == 4:
                code = struct.unpack("<i", resp)[0]
                raise DirectError(f"transport error code {code}")
            raise DirectError(f"short/invalid response ({len(resp)} bytes)")
        dec = mtproto.decrypt_message(self.session.auth_key, resp, from_client=False)
        return service.parse_body(dec["body"], dec["msg_id"])

    def invoke(self, query: bytes, retries: int = 2) -> dict:
        """Invoke a raw TL method; return the winning event (rpc_result or rpc_error).

        Applies initConnection+invokeWithLayer on the first call, updates the
        server salt on bad_server_salt and retries.
        """
        wrapped = query
        if not self._inited:
            wrapped = schema.wrap_initial(self.api_id, dccfg.LAYER, query)

        last_events: list[dict] = []
        for _ in range(max(1, retries)):
            events = self._round_trip(wrapped)
            last_events = events
            salt_fixed = False
            for ev in events:
                t = ev.get("type")
                if t == "new_session_created":
                    self._salt = ev["server_salt"]
                elif t == "bad_server_salt":
                    self._salt = ev["new_server_salt"]
                    salt_fixed = True
            for ev in events:
                if ev.get("type") == "rpc_result":
                    self._inited = True
                    return ev
                if ev.get("type") == "rpc_error":
                    self._inited = True
                    raise rpc_error_from(ev.get("error_code", 0), ev.get("error_message", ""))
            if salt_fixed:
                # Re-send the SAME logical query with the corrected salt.
                continue
            # No rpc result yet (e.g. only new_session/ack) -> try once more bare.
            wrapped = query
        return {"type": "no_result", "events": last_events}

    # ---- convenience -----------------------------------------------------
    def get_config(self) -> dict:
        return self.invoke(schema.help_get_config())

    def get_self(self) -> dict:
        return self.invoke(schema.users_get_users_self())

    def close(self) -> None:
        pass


def from_export(path: str, url: str | None = None) -> "DirectClient":
    """Build a DirectClient from an exported-session JSON file."""
    sess, report = load_export(path)
    if not sess.is_valid():
        raise DirectError(f"invalid session: {report.get('missing')}")
    return DirectClient(sess, url=url)
