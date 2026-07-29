"""Keep the browser-free engine's session context fresh, using the browser.

The direct engine (see `direct/README.md`) talks to Eitaa over plain HTTPS with
no Chromium at all. It needs three session constants that only the real web app
knows: the two envelope routing tokens and the account's own 20-byte peer. Those
are lifted out of a capture of the app's own traffic.

Until now that capture had to be produced by hand with a CLI command, so the
engine went stale and reported "no browser-free session capture" - which is why
it was taken out of the panel. This module closes that gap: whenever a bridge
session is open anyway (login, Update Contacts, a bridge send), the app's traffic
is dumped and saved, so the direct engine always has a current context.

Nothing here talks to the direct engine; it only writes the artifact the direct
engine reads. `direct/` stays importable-free of `bot/`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from config import config

# The envelope every real Eitaa MTProto request starts with. Records that do not
# start with it carry no session context and are dropped, which keeps the saved
# artifact small (a full worker dump is mostly noise).
_ENVELOPE_PREFIX = "ed77be7a"


def sessions_dir() -> Path:
    return config.ARTIFACTS_DIR / "sessions"


def capture_files(account: str) -> list[Path]:
    d = sessions_dir()
    if not d.is_dir():
        return []
    return sorted(
        list(d.glob(f"capall_{account}_*.json")) + list(d.glob(f"worker_tx_{account}_*.json")),
        key=lambda p: p.stat().st_mtime,
    )


def newest_capture_age_hours(account: str) -> float | None:
    """How old the freshest capture is, or None when there is none."""
    files = capture_files(account)
    if not files:
        return None
    return max(0.0, (time.time() - files[-1].stat().st_mtime) / 3600.0)


def has_context(account: str) -> bool:
    """True when a saved capture yields a usable context for the direct engine."""
    return bool(read_context(account))


def read_context(account: str) -> dict | None:
    """Parse the newest capture into {token1, token2, user_id...} or None."""
    files = capture_files(account)
    if not files:
        return None
    try:
        from direct import eitaa_tl as E
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        ctx = E.extract_context(data)
        return ctx if ctx.get("token1") else None
    except Exception:  # noqa: BLE001 - a stale/corrupt capture is simply unusable
        return None


def _useful(records: list) -> list[dict]:
    out = []
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") not in ("fetch", "xhr"):
            continue
        head = rec.get("reqHead")
        if isinstance(head, dict):
            head = head.get("hex")
        if not head or not str(head).startswith(_ENVELOPE_PREFIX):
            continue
        out.append(rec)
    return out


def save_capture(account: str, records: list) -> tuple[Path | None, int]:
    """Persist the useful records as a capture. Returns (path, kept_count)."""
    kept = _useful(records)
    if not kept:
        return None, 0
    d = sessions_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"capall_{account}_{int(time.time())}.json"
    path.write_text(json.dumps({"bridge_refresh": kept}, ensure_ascii=False),
                    encoding="utf-8")
    _prune(account)
    return path, len(kept)


def _prune(account: str, keep: int = 3) -> None:
    """Keep only the newest few captures; they are pure cache."""
    files = capture_files(account)
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


async def refresh_from_driver(driver, account: str) -> dict:
    """Dump the live app's traffic and save it as the direct engine's context.

    Requires the session to have been opened with `eitaa/worker_capture.js` as
    its init script (the hook has to wrap Worker before the app creates one), so
    callers that may need the direct engine open their session that way.

    Returns {ok, kept, path, code} and never raises: a failed refresh only means
    the direct engine keeps whatever context it already had.
    """
    try:
        records = await driver.dump_worker_requests()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"dump failed: {exc}"}
    if records and isinstance(records[0], dict) and records[0].get("kind") == "no_hook":
        return {"ok": False, "code": "worker_capture.js was not injected as init script"}
    path, kept = save_capture(account, records)
    if not kept:
        return {"ok": False, "code": "no Eitaa envelope in the dump (no traffic yet?)"}
    ctx = read_context(account)
    if not ctx:
        return {"ok": False, "kept": kept, "path": str(path),
                "code": "capture saved but no context could be extracted"}
    return {"ok": True, "kept": kept, "path": str(path),
            "user_id": ctx.get("user_id"), "has_self_peer": bool(ctx.get("self_peer"))}
