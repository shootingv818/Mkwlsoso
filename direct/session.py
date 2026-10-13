"""Session model + loader for the direct client.

A Session holds everything needed to talk to Eitaa without a browser:
the 256-byte auth_key (per DC), the current DC id + server address/port, the
server_salt, and the logged-in user id.

The loader is deliberately FLEXIBLE: it parses the JSON produced by the
session exporter (eitaa/session_export.js). Eitaa Web (tweb) stores these in
IndexedDB, and the exact key names are pinned from a real export before this
loader is finalized. Until then, load_export() accepts several common shapes
and reports what it could and couldn't find.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import crypto


def _to_bytes(value: Any) -> bytes | None:
    """Best-effort decode of an auth_key-like value (hex / base64 / list)."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    # The exporter hex-encodes binary as {"__hex": "...", "__len": n}.
    if isinstance(value, dict) and "__hex" in value:
        try:
            return bytes.fromhex(value["__hex"])
        except (ValueError, TypeError):
            return None
    if isinstance(value, list) and all(isinstance(x, int) for x in value):
        return bytes(value)
    if isinstance(value, str):
        s = value.strip()
        # hex?
        try:
            if len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
                return bytes.fromhex(s)
        except ValueError:
            pass
        # base64?
        for pad in ("", "=", "=="):
            try:
                return base64.b64decode(s + pad)
            except Exception:  # noqa: BLE001
                continue
    return None


@dataclass
class Session:
    dc_id: int = 0
    auth_key: bytes = b""
    server_salt: bytes = b""
    user_id: int = 0
    server_address: str = ""
    server_port: int = 443
    # All DCs whose auth keys tweb had stored (dc_id -> 256-byte key / 8-byte salt).
    # dc_id/auth_key/server_salt above are the HOME DC's, taken from these.
    auth_keys_by_dc: dict = field(default_factory=dict)
    salts_by_dc: dict = field(default_factory=dict)
    # extra fields we captured but don't model yet (kept for debugging)
    extra: dict = field(default_factory=dict)

    @property
    def auth_key_id(self) -> bytes:
        return crypto.auth_key_id(self.auth_key) if len(self.auth_key) == 256 else b""

    def is_valid(self) -> bool:
        return self.dc_id > 0 and len(self.auth_key) == 256 and self.user_id > 0

    def describe(self) -> str:
        return (
            f"Session(dc={self.dc_id}, user={self.user_id}, "
            f"auth_key={len(self.auth_key)}B, salt={len(self.server_salt)}B, "
            f"addr={self.server_address or '?'}:{self.server_port}, "
            f"valid={self.is_valid()})"
        )

    def to_json(self) -> dict:
        return {
            "dc_id": self.dc_id,
            "auth_key_b64": base64.b64encode(self.auth_key).decode() if self.auth_key else "",
            "server_salt_b64": base64.b64encode(self.server_salt).decode() if self.server_salt else "",
            "user_id": self.user_id,
            "server_address": self.server_address,
            "server_port": self.server_port,
            "auth_keys_by_dc": {str(k): base64.b64encode(v).decode()
                                for k, v in self.auth_keys_by_dc.items()},
            "salts_by_dc": {str(k): base64.b64encode(v).decode()
                            for k, v in self.salts_by_dc.items()},
        }

    @classmethod
    def from_json(cls, d: dict) -> "Session":
        return cls(
            dc_id=int(d.get("dc_id") or 0),
            auth_key=_to_bytes(d.get("auth_key_b64") or d.get("auth_key")) or b"",
            server_salt=_to_bytes(d.get("server_salt_b64") or d.get("server_salt")) or b"",
            user_id=int(d.get("user_id") or 0),
            server_address=str(d.get("server_address") or ""),
            server_port=int(d.get("server_port") or 443),
        )


_AUTH_KEY_RE = re.compile(r"^dc(\d+)_auth_key$")
_SALT_RE = re.compile(r"^dc(\d+)_server_salt$")


def load_export(path: str | Path) -> tuple[Session, dict]:
    """Load a raw session export and assemble a Session.

    Pinned to the confirmed Eitaa/tweb layout: the session lives in
    localStorage as `dc` (home dc), `user_auth` ({dcID,id}), and per-DC
    `dc<N>_auth_key` (512-hex = 256 bytes) + `dc<N>_server_salt` (16-hex =
    8 bytes). Falls back to a flat IndexedDB scan if localStorage lacks them.
    Returns (session, report).
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    ls = raw.get("localStorage") if isinstance(raw, dict) else None
    if isinstance(ls, dict) and any(_AUTH_KEY_RE.match(str(k)) for k in ls):
        return _load_from_localstorage(ls, raw)
    return _load_flat(raw)


def _load_from_localstorage(ls: dict, raw: dict) -> tuple[Session, dict]:
    report: dict = {"source": "localStorage", "found": {}, "missing": [], "dcs_with_keys": []}

    home_dc = 0
    try:
        home_dc = int(ls.get("dc") or 0)
    except (TypeError, ValueError):
        pass

    user_id = 0
    ua = ls.get("user_auth")
    if isinstance(ua, str):
        try:
            ua = json.loads(ua)
        except Exception:  # noqa: BLE001
            ua = None
    if isinstance(ua, dict):
        try:
            user_id = int(ua.get("id") or 0)
        except (TypeError, ValueError):
            pass
        if not home_dc:
            try:
                home_dc = int(ua.get("dcID") or 0)
            except (TypeError, ValueError):
                pass

    auth_keys: dict[int, bytes] = {}
    salts: dict[int, bytes] = {}
    for k, v in ls.items():
        mk = _AUTH_KEY_RE.match(str(k))
        if mk and isinstance(v, str):
            b = _to_bytes(v)
            if b and len(b) == 256:
                auth_keys[int(mk.group(1))] = b
                continue
        ms = _SALT_RE.match(str(k))
        if ms and isinstance(v, str):
            sb = _to_bytes(v)
            if sb and len(sb) == 8:
                salts[int(ms.group(1))] = sb

    report["dcs_with_keys"] = sorted(auth_keys.keys())
    report["found"] = {"home_dc": home_dc, "user_id": user_id,
                       "dcs": sorted(auth_keys.keys()), "salts": sorted(salts.keys())}

    sess = Session(
        dc_id=home_dc,
        auth_key=auth_keys.get(home_dc, b""),
        server_salt=salts.get(home_dc, b""),
        user_id=user_id,
        auth_keys_by_dc=auth_keys,
        salts_by_dc=salts,
        extra={"eitaa_auth": ls.get("eitaa_auth"), "token": bool(ls.get("token")),
               "imei": bool(ls.get("imei")), "state_id": ls.get("state_id")},
    )
    if not sess.auth_key:
        report["missing"].append(f"auth_key for home dc {home_dc}")
    if not sess.user_id:
        report["missing"].append("user_id")
    if not sess.dc_id:
        report["missing"].append("home dc")
    return sess, report


def _load_flat(raw: Any) -> tuple[Session, dict]:
    """Fallback: scan a flat IndexedDB/localStorage map for auth-shaped values."""
    report: dict = {"source": "flat-scan", "found": {}, "missing": [], "dc_keys": []}
    flat: dict = {}
    if isinstance(raw, dict):
        flat.update(raw.get("localStorage") or {})
        for db in (raw.get("indexeddb") or {}).values():
            for store, entries in (db.get("stores") or {}).items():
                if isinstance(entries, dict):
                    for k, v in entries.items():
                        flat[f"{store}/{k}"] = v

    best_key: bytes | None = None
    best_dc = 0
    user_id = 0
    salt = b""
    for k, v in flat.items():
        b = _to_bytes(v)
        kl = str(k).lower()
        if b and len(b) == 256 and ("auth" in kl or best_key is None):
            best_key = b
            report["dc_keys"].append(str(k))
            for ch in str(k):
                if ch.isdigit():
                    best_dc = best_dc or int(ch)
        if "user" in kl and "auth" in kl and isinstance(v, dict) and v.get("id"):
            try:
                user_id = int(v["id"])
            except Exception:  # noqa: BLE001
                pass
        if "salt" in kl:
            sb = _to_bytes(v)
            if sb and len(sb) == 8:
                salt = sb

    sess = Session(dc_id=best_dc, auth_key=best_key or b"", server_salt=salt, user_id=user_id)
    if not sess.auth_key:
        report["missing"].append("auth_key (256B) not found")
    if not sess.dc_id:
        report["missing"].append("dc_id not identified")
    if not sess.user_id:
        report["missing"].append("user_id not found")
    return sess, report
