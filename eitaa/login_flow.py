"""Bridge-based login flow for Eitaa (phone + code, no noVNC).

Thin, reusable orchestration on top of eitaa/login_bridge.js so both the CLI
test and (later) the Telegram bot can drive login the same way:

    send_code(driver, phone, ...)  ->  user reads code  ->  sign_in(driver, ...)

SAFETY: send_code issues exactly ONE auth.sendCode. Nothing here loops or
auto-retries -- requesting login codes repeatedly is what gets a phone number
rate-limited (FLOOD_WAIT). Callers surface FLOOD verbatim and stop.

Each account uses its own isolated Chromium profile (config.profile_dir), so
adding many accounts never mixes sessions.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Any

from config import config

_LOGIN_JS_PATH = Path(__file__).with_name("login_bridge.js")
try:
    _LOGIN_SRC = _LOGIN_JS_PATH.read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _LOGIN_SRC = ""


def normalize_phone_intl(raw: str) -> str:
    """Normalize a phone to international digits (no '+').

    Iranian local numbers like 0930... become 98930...; a leading '+' is
    dropped; a leading 0 (with no country code) is treated as Iran (98).
    Anything already in country-code form is left as digits.
    """
    s = re.sub(r"[^\d+]", "", raw or "")
    s = s.lstrip("+")
    if s.startswith("0098"):
        s = s[2:]
    elif s.startswith("098"):
        s = s[1:]
    elif s.startswith("0") and len(s) == 11:
        s = "98" + s[1:]
    return s


def resolve_api_creds() -> tuple[int, str]:
    """Resolve Eitaa Web's api_id/api_hash without hardcoding secrets in the repo.

    Order: env (EITAA_API_ID/EITAA_API_HASH) -> any artifacts params.json from a
    prior capture -> (0, "") if unknown. The in-page bridge can also read them
    from Eitaa's own config as a further fallback.
    """
    aid = (os.environ.get("EITAA_API_ID") or "").strip()
    ah = (os.environ.get("EITAA_API_HASH") or "").strip()
    if aid.isdigit() and ah:
        return int(aid), ah
    try:
        pattern = str(config.ARTIFACTS_DIR / "**" / "params.json")
        for p in glob.glob(pattern, recursive=True):
            try:
                d = json.loads(Path(p).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            cid = d.get("api_id") or d.get("apiId")
            ch = d.get("api_hash") or d.get("apiHash")
            if cid and ch:
                try:
                    return int(cid), str(ch)
                except (TypeError, ValueError):
                    continue
    except Exception:  # noqa: BLE001
        pass
    return 0, ""


async def ensure_login_bridge(driver: Any) -> bool:
    """Inject login_bridge.js if needed and wait for apiManager to be ready."""
    try:
        has = await driver.page.evaluate("() => typeof window.__MKWL_sendCode === 'function'")
    except Exception:  # noqa: BLE001
        has = False
    if not has:
        if not _LOGIN_SRC:
            return False
        try:
            await driver.page.evaluate(_LOGIN_SRC)
        except Exception:  # noqa: BLE001
            return False
    # tweb builds the unauthorized DC auth_key shortly after load; wait for it.
    for _ in range(30):
        try:
            if await driver.page.evaluate("() => !!(window.apiManager && window.apiManager.invokeApi)"):
                return True
        except Exception:  # noqa: BLE001
            pass
        await driver.page.wait_for_timeout(500)
    return False


async def resolve_creds_with_page(driver: Any, api_id: int, api_hash: str) -> tuple[int, str]:
    """Fill in missing creds from Eitaa's own in-page config if we don't have them."""
    if api_id and api_hash:
        return api_id, api_hash
    try:
        c = await driver.page.evaluate("() => window.__MKWL_authCreds ? window.__MKWL_authCreds() : null")
        if c and c.get("id") and c.get("hash"):
            return int(c["id"]), str(c["hash"])
    except Exception:  # noqa: BLE001
        pass
    return api_id, api_hash


async def send_code(driver: Any, phone: str, api_id: int, api_hash: str) -> dict:
    """Request ONE login code via the bridge. Never retries."""
    if not await ensure_login_bridge(driver):
        return {"ok": False, "code": "auth bridge unavailable"}
    api_id, api_hash = await resolve_creds_with_page(driver, api_id, api_hash)
    if not api_id or not api_hash:
        return {"ok": False, "code": "missing api_id/api_hash "
                                     "(set EITAA_API_ID/EITAA_API_HASH or run a capture first)"}
    try:
        res = await driver.page.evaluate(
            "(a) => window.__MKWL_sendCode(a.p, a.i, a.h)",
            {"p": phone, "i": api_id, "h": api_hash},
        )
        return res if isinstance(res, dict) else {"ok": False, "code": "bad sendCode result"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"sendCode evaluate error: {exc}"}


async def sign_in(driver: Any, phone: str, phone_code_hash: str, code: str) -> dict:
    """Complete sign-in with the code the user received."""
    try:
        res = await driver.page.evaluate(
            "(a) => window.__MKWL_signIn(a.p, a.h, a.c)",
            {"p": phone, "h": phone_code_hash, "c": str(code)},
        )
        return res if isinstance(res, dict) else {"ok": False, "code": "bad signIn result"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"signIn evaluate error: {exc}"}
