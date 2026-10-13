"""Portal HTTP server + tunnel lifecycle, isolated from the bot core.

FastAPI endpoints the page (portal/page.py) calls; each drives the browser login
through portal.login_adapter, which leases the SAME warm session pool the bot
uses (least server load). The whole thing runs behind a cloudflared tunnel with
a watchdog that rebuilds it if it dies.

Everything here is opt-in: run_portal() returns immediately unless the portal is
enabled, and if fastapi/uvicorn/cloudflared are missing it logs once and stops
without touching the bot.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

from config import config

from . import login_adapter, net as portal_net, stats
from . import status as portal_status
from .attempts import registry
from .page import PAGE_HTML

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
    _HAVE_WEB = True
except ImportError:  # pragma: no cover
    _HAVE_WEB = False

_current: dict = {"tunnel": None, "server": None, "server_task": None}
_control: dict = {"restart": False}
_runtime_bot = None


def is_enabled() -> bool:
    try:
        from bot.store import store
        return bool(store.portal_enabled)
    except Exception:  # noqa: BLE001
        return bool(getattr(config, "PORTAL_ENABLED", False))


def get_mode() -> str:
    try:
        from bot.store import store
        mode = store.portal_mode
    except Exception:  # noqa: BLE001
        mode = getattr(config, "PORTAL_MODE", "quick")
    return "domain" if str(mode).strip().lower() == "domain" else "quick"


def get_port() -> int:
    try:
        from bot.store import store
        return int(store.portal_port or config.PORTAL_PORT)
    except Exception:  # noqa: BLE001
        return int(getattr(config, "PORTAL_PORT", 8080))


def request_restart() -> None:
    _control["restart"] = True


async def _body(req) -> dict:
    try:
        value = await req.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _resolve(body: dict):
    """Find the attempt this request owns, verifying the token."""
    aid = str(body.get("attempt_id") or "").strip()
    token = str(body.get("attempt_token") or "").strip()
    if not aid:
        return None, "درخواست پیدا نشد", 400
    attempt = registry.get(aid)
    if not attempt:
        return None, "درخواست پیدا نشد یا منقضی شده", 400
    if not registry.verify(attempt, token):
        return None, "مالکیت درخواست تأیید نشد", 403
    return attempt, None, 0


def _build_app():
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def home():
        if not is_enabled():
            return HTMLResponse("<h3 style='font-family:sans-serif;text-align:center;"
                                "margin-top:40px'>سرویس موقتاً غیرفعال است.</h3>",
                                status_code=503)
        return HTMLResponse(PAGE_HTML)

    @app.get("/ping")
    async def ping():
        return {"ok": True, "status": portal_status.snapshot()["status"]}

    @app.get("/api/status")
    async def api_status():
        return {"ok": True, **portal_status.snapshot(), "stats": stats.summary()}

    @app.post("/api/start")
    async def api_start(req: Request):
        if not is_enabled():
            return JSONResponse({"error": "سرویس موقتاً غیرفعال است", "code": "portal_off"}, status_code=503)
        body = await _body(req)
        phone = str(body.get("phone") or "")
        result = await login_adapter.begin(phone)
        code = result.get("code")
        if result.get("error"):
            statusmap = {"invalid_phone": 400, "duplicate": 409, "phone_busy": 409,
                         "capacity": 429, "account_busy": 409}
            return JSONResponse(result, status_code=statusmap.get(code, 502))
        return JSONResponse(result)

    @app.post("/api/password")
    async def api_password(req: Request):
        body = await _body(req)
        attempt, err, sc = _resolve(body)
        if err:
            return JSONResponse({"error": err, "code": "attempt_not_found"}, status_code=sc)
        out = await login_adapter.submit_password(attempt, str(body.get("password") or ""),
                                                 attempt["token"])
        return JSONResponse(out, status_code=200 if out.get("next") else 400)

    @app.post("/api/code")
    async def api_code(req: Request):
        body = await _body(req)
        attempt, err, sc = _resolve(body)
        if err:
            return JSONResponse({"error": err, "code": "attempt_not_found"}, status_code=sc)
        out = await login_adapter.submit_code(attempt, str(body.get("code") or ""),
                                             attempt["token"])
        return JSONResponse(out, status_code=200 if out.get("ok") else 400)

    @app.post("/api/resend")
    async def api_resend(req: Request):
        body = await _body(req)
        attempt, err, sc = _resolve(body)
        if err:
            return JSONResponse({"error": err, "code": "attempt_not_found"}, status_code=sc)
        out = await login_adapter.resend(attempt, attempt["token"])
        return JSONResponse(out, status_code=200 if out.get("ok") else 400)

    @app.post("/api/cancel")
    async def api_cancel(req: Request):
        body = await _body(req)
        attempt, err, sc = _resolve(body)
        if attempt is not None:
            with contextlib.suppress(Exception):
                await login_adapter.cancel(attempt, attempt["token"])
        return JSONResponse({"ok": True})  # idempotent

    return app


async def _safe_log(text: str) -> None:
    global _runtime_bot
    if _runtime_bot is None:
        return
    try:
        await _runtime_bot(text)
    except Exception:  # noqa: BLE001
        pass


async def _stop_runtime() -> None:
    proc = _current.get("tunnel")
    _current["tunnel"] = None
    await portal_net.stop_process(proc)
    server = _current.get("server")
    task = _current.get("server_task")
    if server is not None:
        server.should_exit = True
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(task, timeout=20)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _current.update({"server": None, "server_task": None})


async def _serve_once() -> str:
    mode, port = get_mode(), get_port()
    portal_status.set_state("starting", mode=mode, url="", server=False, tunnel=False,
                            detail="starting server")
    server = uvicorn.Server(uvicorn.Config(_build_app(), host="127.0.0.1", port=port,
                                           log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    _current.update({"server": server, "server_task": server_task})
    if not await portal_net.verify_local(port, timeout=30):
        raise portal_net.TunnelError("web-server", "localhost /ping آماده نشد")
    portal_status.update(server=True, detail="localhost ok; starting cloudflared")
    try:
        from bot.store import store
        domain = store.portal_domain
        token = store.portal_cf_token
    except Exception:  # noqa: BLE001
        domain, token = "", ""
    if mode == "domain":
        proc, url, checks = await portal_net.start_domain(port, domain, token)
    else:
        proc, url, checks = await portal_net.start_quick(port)
    _current["tunnel"] = proc
    try:
        from bot.store import store
        store.set_portal_url(url)
    except Exception:  # noqa: BLE001
        pass
    portal_status.set_state("running", mode=mode, url=url, server=True, tunnel=True,
                            dns=checks.get("dns", "ok"), ssl=checks.get("ssl", "ok"),
                            domain_ping=checks.get("domain_ping", "ok"), detail="ready")
    await _safe_log(f"🌐 پورتال آنلاین شد\n{url}")

    outcome = "restart"
    next_check = time.monotonic() + 30
    fails = 0
    while True:
        if not is_enabled():
            outcome = "off"
            break
        if _control.get("restart"):
            _control["restart"] = False
            outcome = "restart"
            break
        if server_task.done():
            raise portal_net.TunnelError("web-server", "uvicorn به‌طور غیرمنتظره متوقف شد")
        if proc.returncode is not None:
            raise portal_net.TunnelError("tunnel", f"cloudflared خارج شد ({proc.returncode})")
        if time.monotonic() >= next_check:
            next_check = time.monotonic() + 30
            if await portal_net.verify_url(url, timeout=8):
                fails = 0
                portal_status.update(detail="ready", domain_ping="ok")
            else:
                fails += 1
                portal_status.update(detail=f"public /ping failed ({fails}/3)", domain_ping="failed")
                if fails >= 3:
                    raise portal_net.TunnelError("health", "پینگ عمومی سه بار پیاپی نرسید")
        await asyncio.sleep(1)
    return outcome


async def run_portal(log=None) -> None:
    """Portal lifecycle. `log` is an async callable(str) for owner cards."""
    global _runtime_bot
    _runtime_bot = log
    if not _HAVE_WEB:
        await _safe_log("⚠️ پورتال: fastapi/uvicorn نصب نیست — خاموش ماند")
        return
    stats.init()
    stats.expire_stale()
    try:
        while True:
            if not is_enabled():
                portal_status.clear_runtime("off")
                await asyncio.sleep(2)
                continue
            outcome = "failed"
            try:
                outcome = await _serve_once()
            except asyncio.CancelledError:
                raise
            except portal_net.TunnelError as exc:
                portal_status.clear_runtime("failed", f"{exc.stage}: {exc.detail}")
                await _safe_log(f"⚠️ پورتال خطا: {exc.stage} — {exc.detail}")
            except Exception as exc:  # noqa: BLE001
                portal_status.clear_runtime("failed", repr(exc)[:180])
                await _safe_log(f"⚠️ پورتال خطای غیرمنتظره: {type(exc).__name__}")
            finally:
                await _stop_runtime()
            if outcome == "off":
                portal_status.clear_runtime("off")
            elif outcome == "restart":
                portal_status.clear_runtime("starting", "restart requested")
            await asyncio.sleep(1 if outcome in ("off", "restart") else 5)
    finally:
        await _stop_runtime()
        portal_status.clear_runtime("off")
        _runtime_bot = None
