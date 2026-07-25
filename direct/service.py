"""MTProto core service-message parsing for the direct client.

These constructor ids are part of the MTProto CORE protocol (not the API
layer), so they are stable across layers and safe to hardcode + unit-test:

    msg_container#73f1f8dc, gzip_packed#3072cfa1, rpc_result#f35c6d01,
    rpc_error#2144ca19, bad_server_salt#edab447b, bad_msg_notification#a7eff811,
    new_session_created#9ec20908, pong#347773c5, msgs_ack#62d6b459,
    msgs_state_req/info etc. (ignored)

parse_body() takes the DECRYPTED inner body (the object after mtproto strips
the salt/session/msg_id/seqno/length header) and returns a flat list of event
dicts, unwrapping containers and gunzipping as needed.
"""

from __future__ import annotations

import gzip
import struct
from typing import Any

from .tl import Reader

MSG_CONTAINER = 0x73F1F8DC
GZIP_PACKED = 0x3072CFA1
RPC_RESULT = 0xF35C6D01
RPC_ERROR = 0x2144CA19
BAD_SERVER_SALT = 0xEDAB447B
BAD_MSG_NOTIFICATION = 0xA7EFF811
NEW_SESSION_CREATED = 0x9EC20908
PONG = 0x347773C5
MSGS_ACK = 0x62D6B459


def _gunzip(data: bytes) -> bytes:
    return gzip.decompress(data)


def parse_body(body: bytes, msg_id: int = 0) -> list[dict]:
    """Parse a decrypted message body into a list of event dicts."""
    events: list[dict] = []
    _parse_object(body, msg_id, events)
    return events


def _parse_object(body: bytes, msg_id: int, events: list[dict]) -> None:
    if len(body) < 4:
        events.append({"type": "unknown", "reason": "short", "msg_id": msg_id})
        return
    cid = struct.unpack_from("<I", body, 0)[0]

    if cid == MSG_CONTAINER:
        r = Reader(body)
        r.int(signed=False)                # container id
        count = r.int(signed=False)
        for _ in range(count):
            sub_msg_id = r.long(signed=False)
            r.int()                        # seqno
            length = r.int(signed=False)
            sub = r.read(length)
            _parse_object(sub, sub_msg_id, events)
        return

    if cid == GZIP_PACKED:
        r = Reader(body)
        r.int(signed=False)                # gzip id
        try:
            _parse_object(_gunzip(r.bytes()), msg_id, events)
        except Exception as exc:  # noqa: BLE001
            events.append({"type": "gzip_error", "detail": str(exc), "msg_id": msg_id})
        return

    if cid == RPC_RESULT:
        r = Reader(body)
        r.int(signed=False)
        req_msg_id = r.long(signed=False)
        rest = r.read(r.remaining())
        inner_cid = struct.unpack_from("<I", rest, 0)[0] if len(rest) >= 4 else 0
        if inner_cid == GZIP_PACKED:
            rr = Reader(rest)
            rr.int(signed=False)
            try:
                rest = _gunzip(rr.bytes())
                inner_cid = struct.unpack_from("<I", rest, 0)[0] if len(rest) >= 4 else 0
            except Exception:  # noqa: BLE001
                pass
        if inner_cid == RPC_ERROR:
            err = _parse_rpc_error(rest)
            events.append({"type": "rpc_error", "req_msg_id": req_msg_id, **err})
        else:
            events.append({"type": "rpc_result", "req_msg_id": req_msg_id,
                           "result_cid": inner_cid, "result": rest})
        return

    if cid == RPC_ERROR:
        events.append({"type": "rpc_error", "msg_id": msg_id, **_parse_rpc_error(body)})
        return

    if cid == BAD_SERVER_SALT:
        r = Reader(body)
        r.int(signed=False)
        events.append({
            "type": "bad_server_salt",
            "bad_msg_id": r.long(signed=False),
            "bad_msg_seqno": r.int(),
            "error_code": r.int(),
            "new_server_salt": r.read(8),
        })
        return

    if cid == BAD_MSG_NOTIFICATION:
        r = Reader(body)
        r.int(signed=False)
        events.append({
            "type": "bad_msg_notification",
            "bad_msg_id": r.long(signed=False),
            "bad_msg_seqno": r.int(),
            "error_code": r.int(),
        })
        return

    if cid == NEW_SESSION_CREATED:
        r = Reader(body)
        r.int(signed=False)
        events.append({
            "type": "new_session_created",
            "first_msg_id": r.long(signed=False),
            "unique_id": r.read(8),
            "server_salt": r.read(8),
        })
        return

    if cid == PONG:
        r = Reader(body)
        r.int(signed=False)
        events.append({"type": "pong", "msg_id": r.long(signed=False),
                       "ping_id": r.long(signed=False)})
        return

    if cid == MSGS_ACK:
        events.append({"type": "msgs_ack", "raw_msg_id": msg_id})
        return

    events.append({"type": "other", "cid": cid, "msg_id": msg_id, "body": body})


def _parse_rpc_error(body: bytes) -> dict:
    r = Reader(body)
    r.int(signed=False)          # rpc_error id
    code = r.int()
    try:
        message = r.string()
    except Exception:  # noqa: BLE001
        message = ""
    return {"error_code": code, "error_message": message}
