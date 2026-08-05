"""Drives an Eitaa browser login for the portal — on the WARM session pool.

Why this shape (the answer to "least load / fastest"): Eitaa login has no
browser-free path, so a browser is mandatory. The cheapest way to run one is the
project's existing standby pool (capture/pool.py) plus Warm Path: Chromium is
booted once and REUSED, not launched per attempt. A separate worker fleet would
only help across MORE machines; on one 2-core box it just competes for RAM. So
this adapter leases from the same pool every other job uses and adds no new
browser weight beyond a normal "Add Account".

It mirrors bot.runner._bridge_login_job (the proven pattern: hold the lease and
await the code future INSIDE it), but is isolated and portal-shaped: a two-call
API the web layer drives.

    begin(phone)            -> {"next": "code"|"password"} or {"error", "code"}
    submit_password(a, pw)  -> {"next": "code"} or {"error"}
    submit_code(a, code)    -> {"ok": True, "account": ...} or {"error", "code"}
    resend(a)               -> re-request the code
    cancel(a)               -> abandon

Nothing here modifies the base project; it calls existing functions only. The
externally-driven pieces (pool.lease, the driver class, the login_flow calls,
the manager's confirm/collect helpers) are looked up through _deps() so a test
can substitute fakes without a browser.
"""
from __future__ import annotations

import asyncio
import re
import time

from config import config

from . import attempts as attempts_mod
from . import stats

registry = attempts_mod.registry


# --------------------------------------------------------------------------
# Dependency lookup — one place, so tests can inject fakes for the browser.
# --------------------------------------------------------------------------
def _deps():
    from capture.pool import pool
    from eitaa.driver import EitaaDriver
    from eitaa import login_flow
    from bot.runner import manager
    return pool, EitaaDriver, login_flow, manager


def _account_name(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def _wrong_code(text: str) -> bool:
    t = str(text or "").upper().replace("-", "_")
    if any(k in t for k in ("INVALID_AUTH", "AUTH_FROM_ANOTHER", "NOT_REGISTERED")):
        return False
    return any(k in t for k in (
        "PHONE_CODE_INVALID", "CODE_INVALID", "INVALID_CODE", "WRONG_CODE",
        "CODE_EMPTY", "کد اشتباه", "کد نامعتبر"))


def _transient(text: str) -> bool:
    t = str(text or "").upper()
    return any(k in t for k in ("TIMEOUT", "NETWORK", "CONNECTION", "TEMPORAR",
                                "FLOOD", "TRANSPORT"))


async def begin(phone: str, *, on_result=None) -> dict:
    """Start a login. Returns once the code has been sent (or password needed),
    or an error. `on_result(dict)` is called later with the final outcome."""
    pool, Driver, login_flow, manager = _deps()
    intl = login_flow.normalize_phone_intl(phone)
    account = _account_name(intl)
    if not re.fullmatch(r"98\d{10}", intl):
        return {"error": "شماره موبایل معتبر نیست", "code": "invalid_phone"}

    # Already a known account? Discover from the profiles dir directly (the same
    # source bot.app.list_accounts uses) so this stays free of the telethon-heavy
    # app module and can be unit-tested.
    try:
        d = config.PROFILES_DIR
        if d.is_dir() and account in {p.name for p in d.iterdir() if p.is_dir()}:
            return {"error": "این شماره قبلاً اضافه شده", "code": "duplicate"}
    except Exception:  # noqa: BLE001
        pass

    # Capacity (one Chromium per attempt).
    async with registry.gate():
        if registry.by_phone(intl):
            return {"error": "برای این شماره یک درخواست فعال هست", "code": "phone_busy"}
        if registry.at_capacity():
            pos = registry.capacity_position()
            return {"error": f"الان {pos} نفر جلوی شما هستند؛ کمی بعد دوباره امتحان کنید",
                    "code": "capacity", "position": pos}
        if manager.is_busy(account):
            return {"error": "این اکانت الان درگیر کار دیگری است", "code": "account_busy"}
        aid, token = registry.new_id_token()
        attempt = registry.create(aid, token, intl)

    attempt["intl"] = intl
    attempt["account"] = account
    attempt["code_future"] = asyncio.get_event_loop().create_future()
    attempt["password"] = None
    attempt["ready"] = asyncio.Event()
    attempt["on_result"] = on_result
    stats.create_attempt(aid, intl, registry.owner_hash(token), attempt["created_at"],
                         attempt["expires_at"])

    attempt["task"] = asyncio.create_task(_run(attempt))
    # Wait until the worker has either sent the code, asked for a password, or
    # failed — but never longer than the TTL.
    try:
        await asyncio.wait_for(attempt["ready"].wait(),
                               timeout=min(90, registry.remaining(attempt)))
    except asyncio.TimeoutError:
        pass
    stage = attempt.get("stage")
    if stage in ("code", "password"):
        stats.mark_started(aid)
        return _payload(attempt, next=stage)
    if attempt.get("error"):
        return {"error": attempt["error"], "code": attempt.get("error_code", "start_failed")}
    return {"error": "شروع ناموفق بود؛ دوباره تلاش کنید", "code": "start_failed"}


async def submit_code(attempt: dict, code: str, token: str) -> dict:
    if not registry.verify(attempt, token):
        return {"error": "مالکیت درخواست تأیید نشد", "code": "forbidden"}
    if registry.expired(attempt):
        return {"error": "مهلت تمام شد؛ دوباره شروع کنید", "code": "expired"}
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 5 and len(code) != 6:
        return {"error": "کد باید ۵ یا ۶ رقمی باشد", "code": "invalid_code_format"}
    fut = attempt.get("code_future")
    if not fut or fut.done():
        return {"error": "ابتدا کد جدید بگیرید", "code": "missing_context"}
    result_ready = attempt["result_ready"] = asyncio.Event()
    fut.set_result(code)
    try:
        await asyncio.wait_for(result_ready.wait(),
                               timeout=min(120, registry.remaining(attempt)))
    except asyncio.TimeoutError:
        return {"error": "پاسخ در مهلت نیامد؛ کمی صبر کنید", "code": "temporary_error",
                "retryable": True}
    outcome = attempt.get("outcome") or {}
    return outcome


async def submit_password(attempt: dict, password: str, token: str) -> dict:
    """2FA is not handled here (the bot points the owner to noVNC), so this is
    an honest refusal rather than a broken screen."""
    if not registry.verify(attempt, token):
        return {"error": "مالکیت درخواست تأیید نشد", "code": "forbidden"}
    return {"error": "این حساب رمز دو‌مرحله‌ای (2FA) دارد؛ فعلاً باید از noVNC وارد شوی",
            "code": "password_needed"}


async def resend(attempt: dict, token: str) -> dict:
    """No real re-send: auth.sendCode is heavily rate-limited on Eitaa (a resend
    is what earns a FLOOD_WAIT), and the code the app/call already delivered stays
    valid for the whole attempt window. So this just confirms it is still usable."""
    if not registry.verify(attempt, token):
        return {"error": "مالکیت درخواست تأیید نشد", "code": "forbidden"}
    if registry.expired(attempt):
        return {"error": "مهلت تمام شد؛ دوباره شروع کنید", "code": "expired"}
    return _payload(attempt, ok=True, next="code",
                    note="همان کدی که آمده هنوز معتبر است")


async def cancel(attempt: dict, token: str) -> dict:
    if not registry.verify(attempt, token):
        return {"error": "مالکیت درخواست تأیید نشد", "code": "forbidden"}
    task = attempt.get("task")
    if task is not None and not task.done():
        task.cancel()
    else:
        await _finish(attempt, {"error": "لغو شد", "code": "cancelled"})
    return {"ok": True}


def _payload(attempt: dict, **extra) -> dict:
    data = {"attempt_id": attempt["id"], "attempt_token": attempt["token"],
            "expires_in": registry.remaining(attempt)}
    data.update(extra)
    return data


async def _finish(attempt: dict, outcome: dict) -> None:
    """Record the terminal outcome once, wake any waiter, drop the attempt."""
    if attempt.get("_finished"):
        return
    attempt["_finished"] = True
    attempt["outcome"] = outcome
    aid = attempt["id"]
    if outcome.get("ok"):
        stats.finish(aid, "success", account=outcome.get("account"))
    elif outcome.get("code") == "expired":
        stats.finish(aid, "expired", error="ttl")
    else:
        stats.finish(aid, "failed", error=outcome.get("error", "")[:200])
    for key in ("ready", "result_ready"):
        ev = attempt.get(key)
        if ev is not None:
            ev.set()
    cb = attempt.get("on_result")
    if cb is not None:
        try:
            cb(outcome)
        except Exception:  # noqa: BLE001
            pass
    async with registry.gate():
        registry.pop(aid)
    lock = registry._locks.get(aid)
    if lock is not None:
        asyncio.create_task(registry.retire_lock(aid, lock))


async def _run(attempt: dict) -> None:
    """The whole login, holding one warm-pool lease for its duration."""
    pool, Driver, login_flow, manager = _deps()
    intl = attempt["intl"]
    account = attempt["account"]
    aid = attempt["id"]
    engine = "bridge"
    try:
        from bot.runner import effective_engine, _worker_capture_script
        engine = effective_engine(manager.settings_provider())
        init_script = _worker_capture_script(engine)
    except Exception:  # noqa: BLE001
        init_script = None

    api_id, api_hash = login_flow.resolve_api_creds()
    try:
        async with pool.lease(account, headed=config.HEADED_JOBS,
                              init_script_path=init_script) as session:
            driver = Driver(session)
            await driver.open()
            if await driver.is_logged_in():
                await _save_and_finish(attempt, driver, session, manager, login_flow)
                return
            api_id, api_hash = await login_flow.resolve_creds_with_page(driver, api_id, api_hash)
            if not api_id or not api_hash:
                attempt["error"] = "api_id/api_hash تنظیم نشده"
                attempt["error_code"] = "no_creds"
                attempt["ready"].set()
                await _finish(attempt, {"error": attempt["error"], "code": "no_creds"})
                return

            sc = await login_flow.send_code(driver, intl, api_id, api_hash)
            if sc.get("needs_password") or "SESSION_PASSWORD_NEEDED" in str(sc.get("code", "")).upper():
                # The base project does not do 2FA passwords over the bridge
                # (it points the owner to noVNC). The portal is honest about the
                # same limit instead of pretending.
                attempt["ready"].set()
                await _finish(attempt, {
                    "error": "این حساب رمز دو‌مرحله‌ای (2FA) دارد؛ فعلاً باید از noVNC وارد شوی",
                    "code": "password_needed"})
                return
            if not sc.get("ok"):
                code = str(sc.get("code", ""))
                attempt["error"] = code
                attempt["error_code"] = "sendcode_failed"
                attempt["ready"].set()
                await _finish(attempt, {"error": "ارسال کد ناموفق بود", "code": "sendcode_failed"})
                return
            phch = sc.get("phone_code_hash")
            if not phch:
                attempt["ready"].set()
                await _finish(attempt, {"error": "phone_code_hash نیامد", "code": "no_hash"})
                return
            attempt["phch"] = phch
            attempt["stage"] = "code"
            attempt["ready"].set()

            # Wait for the user's code (bounded by the TTL), then retry loop for
            # wrong codes up to the limit.
            max_wrong = int(getattr(config, "PORTAL_MAX_WRONG_CODES", 3))
            wrong = 0
            while True:
                remaining = registry.remaining(attempt)
                if remaining <= 0:
                    await _finish(attempt, {"error": "مهلت تمام شد", "code": "expired"})
                    return
                try:
                    code = await asyncio.wait_for(attempt["code_future"], timeout=remaining)
                except asyncio.TimeoutError:
                    await _finish(attempt, {"error": "مهلت تمام شد", "code": "expired"})
                    return
                si = await login_flow.sign_in(driver, intl, phch, code)
                if si.get("needs_password"):
                    outcome = {"error": "این حساب رمز دو‌مرحله‌ای (2FA) دارد؛ فعلاً باید از noVNC وارد شوی",
                               "code": "password_needed"}
                    rr = attempt.get("result_ready")
                    if rr is not None:
                        attempt["outcome"] = outcome
                        rr.set()
                    await _finish(attempt, outcome)
                    return
                if si.get("ok"):
                    await _save_and_finish(attempt, driver, session, manager, login_flow)
                    return
                detail = str(si.get("code", ""))
                if _wrong_code(detail):
                    wrong += 1
                    stats.wrong_code(aid, detail)
                    if wrong >= max_wrong:
                        await _finish(attempt, {"error": "سقف کد اشتباه پر شد", "code": "wrong_code_limit"})
                        return
                    # arm for the next code and tell the waiter
                    attempt["code_future"] = asyncio.get_event_loop().create_future()
                    rr = attempt.get("result_ready")
                    attempt["outcome"] = {"error": f"کد اشتباه؛ {max_wrong - wrong} فرصت مانده",
                                          "code": "wrong_code", "wrong_code_events": wrong,
                                          "retryable": True}
                    if rr is not None:
                        rr.set()
                    continue
                if _transient(detail):
                    attempt["code_future"] = asyncio.get_event_loop().create_future()
                    attempt["outcome"] = {"error": "ارتباط موقتاً قطع شد؛ همان کد را دوباره بزنید",
                                          "code": "temporary_error", "retryable": True}
                    rr = attempt.get("result_ready")
                    if rr is not None:
                        rr.set()
                    continue
                await _finish(attempt, {"error": "ورود ناموفق بود", "code": "login_failed"})
                return
    except asyncio.CancelledError:
        await _finish(attempt, {"error": "لغو شد", "code": "cancelled"})
        raise
    except Exception as exc:  # noqa: BLE001
        if not attempt.get("ready", asyncio.Event()).is_set():
            attempt.get("ready", asyncio.Event()).set()
        await _finish(attempt, {"error": f"{type(exc).__name__}", "code": "exception"})


async def _save_and_finish(attempt, driver, session, manager, login_flow) -> None:
    """Confirm the app is logged in, save contacts + meta, finish success."""
    intl = attempt["intl"]
    account = attempt["account"]
    try:
        logged = await manager._wait_logged_in(driver, session)
    except Exception:  # noqa: BLE001
        logged = False
    if not logged:
        await _finish(attempt, {"error": "برنامه در مهلت مقرر وارد نشد", "code": "not_confirmed"})
        return
    from bot import contacts_store
    try:
        collected, _src = await manager._collect_contacts(driver, account)
        record = contacts_store.save(account, collected)
        contacts = record.get("count", 0)
    except Exception:  # noqa: BLE001 - login still succeeded
        contacts = 0
    try:
        from bot.store import store as _store
        _store.set_account_meta(account, phone=account, contacts=contacts, pvs=None)
    except Exception:  # noqa: BLE001
        pass
    await _finish(attempt, {"ok": True, "account": account, "phone": intl,
                            "contacts": contacts})
