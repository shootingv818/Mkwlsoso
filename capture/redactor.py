"""Redaction layer.

Every piece of captured data passes through here BEFORE it is written to disk.
The goal is to keep captures useful for protocol analysis (field names, shapes,
lengths, stable hashes, status codes) while never persisting real secrets such
as cookies, tokens, OTP codes, passwords, phone numbers, or private content.

The design principle: redact on the way in, not on the way out. A redacted
value is replaced with a small descriptor object so analysis can still compare
shape and length across runs.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Header names whose values must never be stored in clear text.
SENSITIVE_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-auth-token",
    "x-auth",
    "x-api-key",
    "x-access-token",
    "x-csrf-token",
    "x-xsrf-token",
    "sec-websocket-key",
    "sec-websocket-accept",
}

# JSON/body key fragments (case-insensitive substring match) that indicate a
# secret or personal value. The key name is preserved; the value is redacted.
SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "auth",
    "password",
    "passwd",
    "secret",
    "otp",
    "code",
    "session",
    "cookie",
    "hash",
    "signature",
    "sign",
    "key",
    "phone",
    "mobile",
    "msisdn",
    "email",
    "imei",
    "device_id",
    "deviceid",
    "credential",
    "bearer",
)

# Keys that are structurally important for protocol analysis and safe to keep
# even though a fragment above might otherwise match them.
ALLOWLIST_KEYS = {
    "access_hash",  # not a secret on its own; needed to understand peer refs
    "status_code",
    "error_code",
}

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?98|0)?9\d{9}(?!\d)")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]


def _redacted(value: Any, reason: str) -> dict[str, Any]:
    """Return a descriptor that preserves shape without leaking the value."""
    text = value if isinstance(value, str) else repr(value)
    return {
        "__redacted__": True,
        "reason": reason,
        "len": len(text),
        "sha256_16": _digest(text),
    }


def _key_is_sensitive(key: str) -> bool:
    low = key.lower()
    if low in ALLOWLIST_KEYS:
        return False
    return any(frag in low for frag in SENSITIVE_KEY_FRAGMENTS)


def redact_headers(headers: dict[str, str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in (headers or {}).items():
        if name.lower() in SENSITIVE_HEADERS or _key_is_sensitive(name):
            out[name] = _redacted(value, f"sensitive-header:{name.lower()}")
        else:
            out[name] = scrub_text(value)
    return out


def scrub_text(value: str) -> str:
    """Mask obvious phone numbers and long token-like blobs inside free text."""
    if not isinstance(value, str):
        return value
    scrubbed = _PHONE_RE.sub("<PHONE>", value)
    scrubbed = _LONG_TOKEN_RE.sub(
        lambda m: f"<TOKEN len={len(m.group(0))} h={_digest(m.group(0))}>",
        scrubbed,
    )
    return scrubbed


def redact_value(value: Any, key_hint: str | None = None, depth: int = 0) -> Any:
    """Recursively redact a decoded JSON-like structure.

    - dict: redact values whose key looks sensitive; recurse otherwise.
    - list: recurse element-wise.
    - str: scrub phone numbers / long tokens.
    - other scalars: keep as-is.
    """
    if depth > 40:
        return _redacted(value, "max-depth")

    if key_hint and _key_is_sensitive(key_hint) and not isinstance(value, (dict, list)):
        return _redacted(value, f"sensitive-key:{key_hint.lower()}")

    if isinstance(value, dict):
        return {k: redact_value(v, key_hint=k, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, key_hint=key_hint, depth=depth + 1) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def summarize_body(raw: bytes | str | None, content_type: str | None) -> dict[str, Any]:
    """Produce a safe, analysis-friendly summary of a request/response body.

    Never returns raw secret content. For JSON it returns a redacted structure;
    for anything else it returns type/length/hash metadata only.
    """
    import json

    if raw is None:
        return {"present": False}

    if isinstance(raw, bytes):
        size = len(raw)
        raw_hash = hashlib.sha256(raw).hexdigest()[:16]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "present": True,
                "kind": "binary",
                "size": size,
                "sha256_16": raw_hash,
                "content_type": content_type,
            }
    else:
        text = raw
        size = len(text.encode("utf-8", "replace"))
        raw_hash = _digest(text)

    ct = (content_type or "").lower()
    looks_json = "json" in ct or text[:1] in {"{", "["}
    if looks_json:
        try:
            parsed = json.loads(text)
            return {
                "present": True,
                "kind": "json",
                "size": size,
                "sha256_16": raw_hash,
                "content_type": content_type,
                "json": redact_value(parsed),
            }
        except (ValueError, TypeError):
            pass

    return {
        "present": True,
        "kind": "text",
        "size": size,
        "sha256_16": raw_hash,
        "content_type": content_type,
        "preview": scrub_text(text[:200]),
    }
