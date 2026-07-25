"""Browser-free sender (isolated, deletable) — the reusable form of the
already-proven `cli.py direct-send` / `direct-send-file` paths.

Nothing new is invented here: the envelope (`wrap_eitaa`), the serializers
(`eitaa_tl.send_message` / `build_file_send` / `send_media`) and the response
classifier are exactly the ones whose bytes are verified against real captures
in `direct/tests/test_direct.py`. This module only turns the one-shot CLI
commands into something a long-running job can call repeatedly:

  * ONE keep-alive connection per host, reused for every recipient.
  * A file is uploaded ONCE and then re-sent to each recipient with `sendMedia`
    (same trick the browser file bridge uses — no re-upload per recipient).
  * Results come back in the SAME dict shape as the browser bridge
    ({ok, method, msg_id, limit, code}) so callers can treat both identically.

Hosts (confirmed by `cli.py direct-inspect-capture`):
  * text / contacts -> a regular API host (bagher / majid)
  * media (saveFilePart + sendMedia) -> a DEDICATED media host (fateme)
Both are read out of the account's own capture when possible, so we always talk
to the same hosts the browser did.

Isolation: imports only `config` + siblings inside `direct/`.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path

from config import config

from . import eitaa_tl as E
from .transport import HttpTransport, unwrap_eitaa, wrap_eitaa

# Fallbacks used only when a capture doesn't reveal the host (same defaults the
# CLI commands already used).
DEFAULT_API_URL = os.environ.get("MKWL_DIRECT_API_URL", "https://majid.eitaa.com/eitaa/")
DEFAULT_MEDIA_URL = os.environ.get("MKWL_DIRECT_MEDIA_URL", "https://fateme.eitaa.com/eitaa/")

# Server phrases that mean "you are being rate limited", not "this failed".
_LIMIT_PHRASES = ("FLOOD", "TOO_MANY", "RETRY_LIMIT", "LIMIT")


class SenderError(Exception):
    """Raised when the account has no usable browser-free session context."""


# --- session context (moved from cli.py, same files, same precedence) -------

def newest_capture(account: str):
    """(path, parsed) of the account's newest capture, or (None, None).

    Reads the same gitignored artifacts the CLI reads:
    `artifacts/sessions/capall_<acct>_*.json` and `worker_tx_<acct>_*.json`,
    newest by real modification time.
    """
    cap_dir = config.ARTIFACTS_DIR / "sessions"
    files = (glob.glob(str(cap_dir / f"capall_{account}_*.json"))
             + glob.glob(str(cap_dir / f"worker_tx_{account}_*.json")))
    if not files:
        return None, None
    path = max(files, key=os.path.getmtime)
    try:
        return path, json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return path, None


def load_cookies(account: str) -> dict:
    """Exported browser cookies for the account (harmless if empty).

    Eitaa turned out not to use cookies for auth, but the jar is kept as
    belt-and-suspenders exactly as the CLI does.
    """
    p = config.ARTIFACTS_DIR / "sessions" / f"cookies_{account}.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _url_for_ctors(capture, ctors: set[int]) -> str | None:
    """The URL the browser used for a request whose TL body starts with one of
    `ctors`. Same technique as `eitaa_tl.extract_media_url`, generalized."""
    for rec in E._iter_records(capture):  # noqa: SLF001 - sibling module helper
        if rec.get("kind") not in ("fetch", "xhr"):
            continue
        head = E._head_hex(rec)  # noqa: SLF001
        if not head.startswith("ed77be7a"):
            continue
        try:
            body = unwrap_eitaa(bytes.fromhex(head))["body"]
        except Exception:  # noqa: BLE001
            continue
        if len(body) >= 4 and int.from_bytes(body[:4], "little", signed=False) in ctors:
            url = rec.get("url")
            if url:
                return url
    return None


def extract_api_url(capture) -> str | None:
    """The API host the browser used for sendMessage / importContacts."""
    return _url_for_ctors(capture, {E.SEND_MESSAGE, E.IMPORT_CONTACTS})


def _is_limit(text: str) -> bool:
    up = str(text or "").upper()
    return any(p in up for p in _LIMIT_PHRASES)


class DirectSender:
    """Send text and files to Eitaa peers with no browser.

    Usage:
        s = DirectSender(account)
        s.send_text(peer, "hi")             # per recipient
        s.upload_file("/tmp/a.zip")         # ONCE
        s.send_uploaded_file(peer, "cap")   # per recipient, no re-upload
        s.close()
    """

    def __init__(self, account: str, api_url: str | None = None,
                 media_url: str | None = None) -> None:
        self.account = account
        self.capture_path, capture = newest_capture(account)
        if not capture:
            raise SenderError(
                f"no browser-free session capture for '{account}'. Build contacts "
                "or run a capture once, or switch the engine to bridge.")
        try:
            self.ctx = E.extract_context(capture)
        except Exception as exc:  # noqa: BLE001
            raise SenderError(f"cannot extract session context: {exc}") from exc

        self.api_url = api_url or extract_api_url(capture) or DEFAULT_API_URL
        self.media_url = media_url or E.extract_media_url(capture) or DEFAULT_MEDIA_URL
        self.cookies = load_cookies(account)
        self._api_tx: HttpTransport | None = None
        self._media_tx: HttpTransport | None = None
        # State of the single upload reused for every recipient.
        self._file: dict | None = None

    # ---- plumbing ----
    @property
    def self_peer(self) -> bytes | None:
        """The account's own (Saved Messages) peer, if the capture revealed it."""
        return self.ctx.get("self_peer")

    def _tx(self, media: bool) -> HttpTransport:
        if media:
            if self._media_tx is None:
                self._media_tx = HttpTransport(self.media_url, timeout=60.0,
                                               cookies=dict(self.cookies))
            return self._media_tx
        if self._api_tx is None:
            self._api_tx = HttpTransport(self.api_url, timeout=30.0,
                                         cookies=dict(self.cookies))
        return self._api_tx

    def close(self) -> None:
        for tx in (self._api_tx, self._media_tx):
            if tx is not None:
                try:
                    tx.close()
                except Exception:  # noqa: BLE001
                    pass
        self._api_tx = self._media_tx = None

    def _rpc(self, body: bytes, media: bool = False) -> dict:
        """Wrap a bare TL body, POST it, and classify the reply.

        Returns the same shape the browser bridge returns so callers can treat
        the two engines identically.
        """
        raw = wrap_eitaa(self.ctx["token1"], self.ctx["token2"], body)
        try:
            resp = self._tx(media).post(raw)
        except Exception as exc:  # noqa: BLE001
            # A dropped connection is worth one clean retry on a fresh socket.
            try:
                if media:
                    self._media_tx = None
                else:
                    self._api_tx = None
                resp = self._tx(media).post(raw)
            except Exception as exc2:  # noqa: BLE001
                return {"ok": False, "code": f"transport: {exc2 or exc}"}

        resp_body = resp
        try:
            if resp[:4] == bytes.fromhex("ed77be7a"):
                resp_body = unwrap_eitaa(resp)["body"]
        except Exception:  # noqa: BLE001
            pass
        verdict = E.classify_response(resp_body)
        if verdict.get("ok"):
            return {"ok": True, "method": "direct", "detail": verdict.get("note"),
                    "head": resp_body[:16].hex()}
        note = verdict.get("note") or ""
        return {"ok": False, "code": note, "limit": _is_limit(note),
                "head": resp_body[:32].hex()}

    # ---- text ----
    def send_text(self, peer: bytes, text: str) -> dict:
        """messages.sendMessage to one peer (browser-free)."""
        if not peer:
            return {"ok": False, "code": "no peer"}
        res = self._rpc(E.send_message(peer, text))
        if res.get("ok"):
            res["method"] = "direct/sendMessage"
        return res

    # ---- file: upload once, send many ----
    def upload_file(self, file_path: str, caption: str = "") -> dict:
        """Upload the file ONCE to the media host. Call before send_uploaded_file.

        Reuses `eitaa_tl.build_file_send` for the byte-exact part bodies; the
        peer passed to it is irrelevant for the upload itself (uploads are
        peer-independent), so the account's own peer is used.
        """
        fp = Path(file_path)
        if not fp.is_file():
            return {"ok": False, "code": f"file not found: {file_path}"}
        peer = self.self_peer
        if not peer:
            return {"ok": False, "code": "self peer unknown in capture"}
        data = fp.read_bytes()
        mime = E.guess_mime(fp.name)
        plan = E.build_file_send(peer, data, fp.name, self.ctx["user_id"],
                                 caption=caption, mime=mime)

        for i, part_body in enumerate(plan["parts"]):
            res = self._rpc(part_body, media=True)
            if not res.get("ok"):
                return {"ok": False, "code": f"part {i + 1}/{plan['total_parts']}: "
                                             f"{res.get('code')}",
                        "limit": res.get("limit", False)}
        self._file = {
            "file_id": plan["file_id"],
            "total_parts": plan["total_parts"],
            "name": fp.name,
            "mime": mime,
            "size": len(data),
        }
        return {"ok": True, "parts": plan["total_parts"], "name": fp.name,
                "size": len(data), "host": self.media_url}

    def send_uploaded_file(self, peer: bytes, caption: str = "") -> dict:
        """messages.sendMedia re-using the already-uploaded document (no re-upload)."""
        if not peer:
            return {"ok": False, "code": "no peer"}
        f = self._file
        if not f:
            return {"ok": False, "code": "upload_file() was not called first"}
        subtype = f["mime"].split("/")[-1]
        in_file = E.input_file(f["file_id"], f["total_parts"], f"document.{subtype}", "")
        media = E.input_media_uploaded_document(in_file, f["mime"], f["name"])
        res = self._rpc(E.send_media(peer, media, message=caption), media=True)
        if res.get("ok"):
            res["method"] = "direct/sendMedia"
        return res

    # ---- contacts (reuses the same envelope + classifier) ----
    def import_contacts(self, entries, plus_prefix: bool = True) -> dict:
        """contacts.importContacts for a batch of {phone, first, last} dicts.

        Returns {ok, imported, imported_ids, batch, parse_ok, cid, head, code}.

        `plus_prefix` selects the phone format on the wire ("+98..." vs "98...").
        A wrong format makes the server match NOBODY and answer with no error at
        all, so the caller probes both instead of reporting a silent zero.

        `parse_ok` says whether the reply really was `contacts.importedContacts`.
        Without it an unexpected reply constructor would also look like
        "imported 0", which is a very different problem; `cid` + `head` are
        included so it can be identified instead of guessed.

        The imported COUNT and the user_ids are trustworthy; the access_hash each
        peer needs is NOT parsed here (Eitaa's User row constructor is unknown
        and guessing it would produce silently-wrong peers). Peers are harvested
        through the page bridge instead, where tweb has already parsed them.
        """
        def _fmt(phone: str) -> str:
            digits = str(phone or "").lstrip("+")
            return ("+" + digits) if plus_prefix else digits

        trips = [(_fmt(e.get("phone", "")), e.get("first", ""), e.get("last", ""))
                 for e in entries]
        if not trips:
            return {"ok": True, "imported": 0, "batch": 0, "imported_ids": [],
                    "parse_ok": True}
        raw = wrap_eitaa(self.ctx["token1"], self.ctx["token2"],
                         E.import_contacts(trips))
        try:
            resp = self._tx(False).post(raw)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"transport: {exc}", "batch": len(trips)}
        body = resp
        try:
            if resp[:4] == bytes.fromhex("ed77be7a"):
                body = unwrap_eitaa(resp)["body"]
        except Exception:  # noqa: BLE001
            pass
        verdict = E.classify_response(body)
        if not verdict.get("ok"):
            note = verdict.get("note") or ""
            return {"ok": False, "code": note, "limit": _is_limit(note),
                    "batch": len(trips)}
        parsed = E.parse_import_result(body)
        return {"ok": True, "imported": int(parsed.get("imported", 0)),
                "imported_ids": parsed.get("imported_ids") or [],
                "batch": len(trips),
                "parse_ok": bool(parsed.get("ok")),
                "cid": parsed.get("cid"),
                "head": body[:32].hex(),
                "phone_format": "+98" if plus_prefix else "98"}
