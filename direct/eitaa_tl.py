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
    """upload.saveFilePart#b304a621 (pure Telegram TL)."""
    return (
        tl.int_bytes(SAVE_FILE_PART, signed=False)
        + tl.long_bytes(file_id, signed=True)
        + tl.int_bytes(file_part, signed=True)
        + tl.bytes_bytes(data)
    )


# Eitaa appends 24 bytes of upload metadata after the standard saveFilePart TL
# (confirmed from capture): flag(=3), a custom upload-peer ctor 0x59511722 +
# self user_id:long, the total file size:int, and a trailing int(0). Upload is
# peer-independent (route happens later in sendMedia), so this stays "self".
EITAA_UPLOAD_PEER = 0x59511722
EITAA_UPLOAD_FLAG = 3


def save_file_part_eitaa(file_id: int, file_part: int, data: bytes,
                         self_user_id: int, total_size: int) -> bytes:
    """upload.saveFilePart as Eitaa's worker actually sends it (with trailer)."""
    trailer = (
        tl.int_bytes(EITAA_UPLOAD_FLAG, signed=False)
        + tl.int_bytes(EITAA_UPLOAD_PEER, signed=False)
        + tl.long_bytes(self_user_id, signed=False)
        + tl.int_bytes(total_size, signed=True)
        + tl.int_bytes(0, signed=False)
    )
    return save_file_part(file_id, file_part, data) + trailer


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


# --- high-level file send ---------------------------------------------------
UPLOAD_PART_SIZE = 512 * 1024  # 512 KiB, standard Telegram upload chunk

# minimal, explicit MIME map for the extensions the user cares about; anything
# else falls back to a generic binary type.
_MIME = {
    "txt": "text/plain",
    "zip": "application/zip",
    "apk": "application/vnd.android.package-archive",
    "pdf": "application/pdf",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "mp4": "video/mp4", "mp3": "audio/mpeg",
}


def guess_mime(file_name: str) -> str:
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    return _MIME.get(ext, "application/octet-stream")


def build_file_send(peer: bytes, file_bytes: bytes, file_name: str,
                    self_user_id: int, caption: str = "", mime: str | None = None,
                    file_id: int | None = None, random_id: int | None = None,
                    part_size: int = UPLOAD_PART_SIZE) -> dict:
    """Prepare a complete browser-free file send.

    Returns {file_id, parts, total_parts, send_media} where `parts` is the list
    of upload.saveFilePart bodies to POST in order, and `send_media` is the
    messages.sendMedia body to POST afterwards. inputFile.name follows tweb's
    observed pattern ("document.<mime-subtype>"); the real name rides in the
    documentAttributeFilename attribute.
    """
    if mime is None:
        mime = guess_mime(file_name)
    if file_id is None:
        # tweb uses a POSITIVE file_id; a negative one breaks the server's temp
        # filename/path (observed: INTERNAL_SERVER_ERROR "filename: /var/www/...").
        file_id = int.from_bytes(os.urandom(8), "little") & 0x7FFFFFFFFFFFFFFF
    total = len(file_bytes)
    total_parts = max(1, (total + part_size - 1) // part_size)

    parts = []
    for i in range(total_parts):
        chunk = file_bytes[i * part_size:(i + 1) * part_size]
        parts.append(save_file_part_eitaa(file_id, i, chunk, self_user_id, total))

    subtype = mime.split("/")[-1]
    in_file = input_file(file_id, total_parts, f"document.{subtype}", "")
    media = input_media_uploaded_document(in_file, mime, file_name)
    send = send_media(peer, media, message=caption, random_id=random_id)
    return {"file_id": file_id, "parts": parts, "total_parts": total_parts, "send_media": send}


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


def extract_media_url(capture) -> str | None:
    """Return the exact endpoint URL the browser used for MEDIA (saveFilePart /
    sendMedia). CONFIRMED: media does NOT go to the regular API host (majid /
    bagher) but to a dedicated media host (e.g. fateme.eitaa.com); uploading a
    file part there + sending media there keeps them on the SAME storage, which
    is why the browser works and our majid attempts failed with 'part key: 0'."""
    targets = {SAVE_FILE_PART, SEND_MEDIA}
    for rec in _iter_records(capture):
        if rec.get("kind") not in ("fetch", "xhr"):
            continue
        head = _head_hex(rec)
        if not head.startswith("ed77be7a"):
            continue
        try:
            body = unwrap_eitaa(bytes.fromhex(head))["body"]
        except Exception:  # noqa: BLE001
            continue
        if len(body) >= 4 and int.from_bytes(body[:4], "little", signed=False) in targets:
            url = rec.get("url")
            if url:
                return url
    return None


IMPORTED_CONTACTS = 0x77D01C3B
# importedContact#c13e3c50 user_id:long client_id:long -- standard Telegram row,
# so the imported user_ids can be read out safely.
IMPORTED_CONTACT = 0xC13E3C50


def parse_import_result(body: bytes) -> dict:
    """Parse contacts.importedContacts#77d01c3b.

    The FIRST vector after the constructor is `imported` (the numbers that ARE
    on Eitaa). Returns {"ok", "imported": int, "imported_ids": [int, ...]}.

    `imported_ids` is best-effort: the row constructor is standard Telegram, so
    reading the user_ids is safe, but Eitaa's own `User` rows (which carry the
    access_hash) use an unknown constructor and are deliberately NOT guessed --
    peers are harvested through the page bridge instead, where tweb has already
    parsed them. `imported` stays exactly as before for existing callers.
    """
    out: dict = {"ok": False, "imported": 0, "imported_ids": []}
    try:
        r = tl.Reader(body)
        cid = r.int(signed=False)
        if cid != IMPORTED_CONTACTS:
            out["cid"] = cid
            return out
        r.int(signed=False)               # vector id 0x1cb5c415
        count = r.int(signed=False)
        out["ok"] = True
        out["imported"] = int(count)
    except Exception:  # noqa: BLE001
        return out

    # The user_ids are a bonus: never let a parse slip break the count above.
    try:
        ids = []
        for _ in range(out["imported"]):
            row_cid = r.int(signed=False)
            if row_cid != IMPORTED_CONTACT:
                break
            ids.append(r.long(signed=False))
            r.long(signed=True)           # client_id
        out["imported_ids"] = ids
    except Exception:  # noqa: BLE001
        pass
    return out


def find_message_peer(capture, marker: str) -> bytes | None:
    """Find the 20-byte target peer of a sendMessage whose text contains
    `marker`. Used to learn a CONTACT's peer from a controlled browser send."""
    want = marker.encode("utf-8")
    sm = tl.int_bytes(SEND_MESSAGE, signed=False)
    for rec in _iter_records(capture):
        if rec.get("kind") not in ("fetch", "xhr"):
            continue
        head = _head_hex(rec)
        if not head.startswith("ed77be7a"):
            continue
        raw = bytes.fromhex(head)
        req_len = int(rec.get("reqLen") or 0)
        if req_len and len(raw) < req_len:
            continue
        try:
            body = unwrap_eitaa(raw)["body"]
        except Exception:  # noqa: BLE001
            continue
        if not body.startswith(sm) or want not in body:
            continue
        r = tl.Reader(body)
        r.int(signed=False)  # ctor
        r.int(signed=False)  # flags
        return r.read(20)
    return None


# --- response classification -----------------------------------------------
RPC_ERROR = 0x2144CA19
# Eitaa's own error wrapper: eitaa_error#c4b9f9bb code:int message:string
# (e.g. code=500 "INTERNAL_SERVER_ERROR...", code=400 "RETRY_LIMIT10").
EITAA_ERROR = 0xC4B9F9BB


def classify_response(body: bytes) -> dict:
    """Read a response body's leading constructor and decide ok/not-ok.

    Returns {cid, ok, note, code?, message?}. Decodes Eitaa's eitaa_error and
    the standard rpc_error so failures are reported HONESTLY.
    """
    if len(body) < 4:
        return {"cid": None, "ok": False, "note": "empty/short response"}
    cid = int.from_bytes(body[:4], "little", signed=False)

    if cid in (EITAA_ERROR, RPC_ERROR):
        try:
            r = tl.Reader(body)
            r.int(signed=False)          # ctor
            code = r.int(signed=True)    # error code
            msg = r.bytes().decode("utf-8", "replace")
            return {"cid": cid, "ok": False, "code": code, "message": msg,
                    "note": f"error {code}: {msg}"}
        except Exception:  # noqa: BLE001
            return {"cid": cid, "ok": False, "note": "error (undecodable)"}

    # last-resort phrase scan for wrappers we haven't modelled
    text = body.decode("latin-1", "ignore")
    for phrase in ("FLOOD_WAIT", "RETRY_LIMIT", "INTERNAL_SERVER_ERROR",
                   "PEER_ID_INVALID", "AUTH_KEY", "USER_DEACTIVATED", "FILE_"):
        if phrase in text:
            return {"cid": cid, "ok": False, "note": f"error phrase: {phrase}"}
    return {"cid": cid, "ok": True, "note": f"result ctor 0x{cid:08x}"}
