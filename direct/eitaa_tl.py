"""Eitaa method (de)serialization for the browser-free direct client.

Reversed from REAL worker captures (see direct/tests/test_direct.py, which
reproduces the captured bytes of every method below byte-for-byte). Findings:

  * The POST body is BARE, UN-encrypted TL using standard Telegram layer-135
    constructors. Auth is the routing token in the transport envelope
    (direct/transport.py wrap_eitaa) -- there is NO AES / auth_key on this path.
  * "Saved Messages" / self peer is a custom inputPeerUser whose constructor id
    is 0xdde8a54c, followed by user_id:long and access_hash:long (same shape as
    Telegram's inputPeerUser#... just a different ctor id).

Confirmed method constructors (little-endian on the wire):
  messages.sendMessage         0x520c3870
  messages.sendMedia           0x3491eba9
  contacts.importContacts      0x2c800be5
  inputPhoneContact            0xf392b7f4
  upload.saveFilePart          0xb304a621
  inputMediaUploadedDocument   0x5b38c6c1
  inputFile                    0xf52ff27f
  documentAttributeFilename    0x15590068
  (vector)                     0x1cb5c415
"""

from __future__ import annotations

import os
from typing import Iterable

from . import tl
from .transport import unwrap_eitaa

# --- constructor ids --------------------------------------------------------
SEND_MESSAGE = 0x520C3870
SEND_MEDIA = 0x3491EBA9
IMPORT_CONTACTS = 0x2C800BE5
INPUT_PHONE_CONTACT = 0xF392B7F4
SAVE_FILE_PART = 0xB304A621
INPUT_MEDIA_UPLOADED_DOCUMENT = 0x5B38C6C1
INPUT_FILE = 0xF52FF27F
DOC_ATTR_FILENAME = 0x15590068
EITAA_INPUT_PEER_USER = 0xDDE8A54C

# flags observed on the wire (tweb sets clear_draft; sendMedia also sets
# entities so it always carries an entities vector, even when empty).
SEND_MESSAGE_FLAGS = 0x80  # clear_draft
SEND_MEDIA_FLAGS = 0x88    # clear_draft | entities


def input_peer_self(user_id: int, access_hash: int) -> bytes:
    """The 20-byte Eitaa self/Saved-Messages peer."""
    return (
        tl.int_bytes(EITAA_INPUT_PEER_USER, signed=False)
        + tl.long_bytes(user_id, signed=False)
        + tl.long_bytes(access_hash, signed=False)
    )


def new_random_id() -> int:
    """A fresh 64-bit random_id (message de-dup key)."""
    return int.from_bytes(os.urandom(8), "little", signed=True)


# --- methods ----------------------------------------------------------------
def send_message(peer: bytes, message: str, random_id: int | None = None,
                 flags: int = SEND_MESSAGE_FLAGS) -> bytes:
    """messages.sendMessage#520c3870 (bare TL body)."""
    if random_id is None:
        random_id = new_random_id()
    return (
        tl.int_bytes(SEND_MESSAGE, signed=False)
        + tl.int_bytes(flags, signed=False)
        + peer
        + tl.string_bytes(message)
        + tl.long_bytes(random_id, signed=True)
    )


def import_contacts(contacts: Iterable[tuple[str, str, str]],
                    client_ids: Iterable[int] | None = None) -> bytes:
    """contacts.importContacts#2c800be5 with a Vector<inputPhoneContact>.

    contacts: iterable of (phone, first_name, last_name).
    """
    contacts = list(contacts)
    if client_ids is None:
        client_ids = [0] * len(contacts)
    client_ids = list(client_ids)

    def enc_one(i_c):
        i, (phone, first, last) = i_c
        return (
            tl.int_bytes(INPUT_PHONE_CONTACT, signed=False)
            + tl.long_bytes(client_ids[i], signed=True)
            + tl.string_bytes(phone)
            + tl.string_bytes(first)
            + tl.string_bytes(last)
        )

    body = tl.int_bytes(IMPORT_CONTACTS, signed=False)
    body += tl.int_bytes(tl._VECTOR_ID, signed=False) + tl.int_bytes(len(contacts), signed=False)
    for pair in enumerate(contacts):
        body += enc_one(pair)
    return body


def save_file_part(file_id: int, file_part: int, data: bytes) -> bytes:
    """upload.saveFilePart#b304a621."""
    return (
        tl.int_bytes(SAVE_FILE_PART, signed=False)
        + tl.long_bytes(file_id, signed=True)
        + tl.int_bytes(file_part, signed=True)
        + tl.bytes_bytes(data)
    )


def input_file(file_id: int, parts: int, name: str, md5: str = "") -> bytes:
    """inputFile#f52ff27f id:long parts:int name:string md5_checksum:string."""
    return (
        tl.int_bytes(INPUT_FILE, signed=False)
        + tl.long_bytes(file_id, signed=True)
        + tl.int_bytes(parts, signed=True)
        + tl.string_bytes(name)
        + tl.string_bytes(md5)
    )


def input_media_uploaded_document(file: bytes, mime_type: str, file_name: str,
                                  flags: int = 0x10) -> bytes:
    """inputMediaUploadedDocument#5b38c6c1 with a single filename attribute.

    `file` is a serialized inputFile. flags 0x10 matches the captured document
    upload (nosound_video off, force_file etc. off).
    """
    attributes = (
        tl.int_bytes(tl._VECTOR_ID, signed=False) + tl.int_bytes(1, signed=False)
        + tl.int_bytes(DOC_ATTR_FILENAME, signed=False) + tl.string_bytes(file_name)
    )
    return (
        tl.int_bytes(INPUT_MEDIA_UPLOADED_DOCUMENT, signed=False)
        + tl.int_bytes(flags, signed=False)
        + file
        + tl.string_bytes(mime_type)
        + attributes
    )


def send_media(peer: bytes, media: bytes, message: str = "",
               random_id: int | None = None, flags: int = SEND_MEDIA_FLAGS) -> bytes:
    """messages.sendMedia#3491eba9 (bare TL body).

    Field order on the wire (confirmed): flags, peer, media, message,
    random_id, entities(empty vector when flags&0x08).
    """
    if random_id is None:
        random_id = new_random_id()
    body = (
        tl.int_bytes(SEND_MEDIA, signed=False)
        + tl.int_bytes(flags, signed=False)
        + peer
        + media
        + tl.string_bytes(message)
        + tl.long_bytes(random_id, signed=True)
    )
    if flags & 0x08:  # entities present -> empty vector
        body += tl.int_bytes(tl._VECTOR_ID, signed=False) + tl.int_bytes(0, signed=False)
    return body


# --- session-constant extraction from a real capture ------------------------
def _iter_records(capture):
    """Yield records from either a worker_tx list or a capall {op: [recs]} dict."""
    if isinstance(capture, dict):
        for recs in capture.values():
            if isinstance(recs, list):
                yield from recs
    elif isinstance(capture, list):
        yield from capture


def _head_hex(rec) -> str:
    v = rec.get("reqHead")
    if isinstance(v, dict):
        return v.get("hex") or ""
    return v or ""


def extract_context(capture) -> dict:
    """Pull the session-constant wire context out of a real capture:
    token1, token2 (envelope routing) and the 20-byte self peer.

    Returns {token1, token2, self_peer, user_id, access_hash}. Raises
    ValueError if no usable request is present.
    """
    token1 = token2 = None
    self_peer = None
    peer_marker = tl.int_bytes(EITAA_INPUT_PEER_USER, signed=False)  # dde8a54c LE

    for rec in _iter_records(capture):
        if rec.get("kind") not in ("fetch", "xhr"):
            continue
        head = _head_hex(rec)
        if not head.startswith("ed77be7a"):
            continue
        raw = bytes.fromhex(head)
        req_len = int(rec.get("reqLen") or 0)
        if req_len and (len(raw) < req_len):
            continue  # truncated capture -> body may be incomplete
        try:
            env = unwrap_eitaa(raw)
        except Exception:  # noqa: BLE001
            continue
        if token1 is None:
            token1, token2 = env["token1"], env["token2"]
        body = env["body"]
        if self_peer is None:
            idx = body.find(peer_marker)
            if idx != -1 and len(body) >= idx + 20:
                self_peer = body[idx:idx + 20]
        if token1 is not None and self_peer is not None:
            break

    if token1 is None:
        raise ValueError("no Eitaa envelope found in capture")
    result = {"token1": token1, "token2": token2, "self_peer": self_peer}
    if self_peer is not None:
        r = tl.Reader(self_peer)
        r.int(signed=False)  # ctor
        result["user_id"] = r.long(signed=False)
        result["access_hash"] = r.long(signed=False)
    return result


# --- response classification -----------------------------------------------
RPC_ERROR = 0x2144CA19


def classify_response(body: bytes) -> dict:
    """Best-effort read of a response body's leading constructor.

    Returns {cid, ok, note}. `ok` is False for an obvious rpc_error or a
    recognizable error phrase; True otherwise. Full parsing of the Updates
    result is done by the caller when needed.
    """
    if len(body) < 4:
        return {"cid": None, "ok": False, "note": "empty/short response"}
    cid = int.from_bytes(body[:4], "little", signed=False)
    text = body.decode("latin-1", "ignore")
    for phrase in ("FLOOD_WAIT", "RETRY_LIMIT", "PEER_ID_INVALID",
                   "AUTH_KEY", "SESSION", "USER_DEACTIVATED"):
        if phrase in text:
            return {"cid": cid, "ok": False, "note": f"error phrase: {phrase}"}
    if cid == RPC_ERROR:
        return {"cid": cid, "ok": False, "note": "rpc_error"}
    return {"cid": cid, "ok": True, "note": f"result ctor 0x{cid:08x}"}
