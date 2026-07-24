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


def load_export(path: str | Path) -> tuple[Session, dict]:
    """Load a raw session export and best-effort assemble a Session.

    Returns (session, report) where report explains what was found/missing so
    we can pin the exact tweb IndexedDB keys from a real export.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    report: dict = {"found": {}, "missing": [], "dc_keys": []}

    # The exporter returns a flat dict of "store/key" -> value plus a
    # convenience "flat" map. We scan for auth-key-shaped values (256 bytes).
    flat: dict = {}
    if isinstance(raw, dict):
        flat.update(raw.get("localStorage") or {})
        for db in (raw.get("indexeddb") or {}).values():
            for store, entries in (db.get("stores") or {}).items():
                if isinstance(entries, dict):
                    for k, v in entries.items():
                        flat[f"{store}/{k}"] = v
    flat.update(raw if isinstance(raw, dict) else {})

    best_key: bytes | None = None
    best_dc = 0
    user_id = 0
    salt = b""
    for k, v in flat.items():
        b = _to_bytes(v)
        kl = str(k).lower()
        if b and len(b) == 256 and ("auth" in kl or best_key is None):
            best_key = b
            report["found"][str(k)] = "auth_key(256B)"
            report["dc_keys"].append(str(k))
            # dc id often embedded in the key name, e.g. dc2_auth_key
            for ch in str(k):
                if ch.isdigit():
                    best_dc = best_dc or int(ch)
        if "user" in kl and "auth" in kl:
            try:
                if isinstance(v, dict) and v.get("id"):
                    user_id = int(v["id"])
                    report["found"]["user_auth"] = user_id
            except Exception:  # noqa: BLE001
                pass
        if "salt" in kl:
            sb = _to_bytes(v)
            if sb and len(sb) == 8:
                salt = sb
                report["found"][str(k)] = "server_salt(8B)"

    sess = Session(dc_id=best_dc, auth_key=best_key or b"", server_salt=salt, user_id=user_id)
    if not sess.auth_key:
        report["missing"].append("auth_key (256B) not found")
    if not sess.dc_id:
        report["missing"].append("dc_id not identified")
    if not sess.user_id:
        report["missing"].append("user_id not found")
    return sess, report
