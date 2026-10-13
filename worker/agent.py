"""Worker-side agent (MODE=worker) — a headless node the master drives.

Modelled on the source project's worker_api.py, adapted to this project: a small
token-protected FastAPI service that runs the SAME login/send code the bot uses
(portal.login_adapter + bot.runner) on ITS OWN machine and browser pool. It has
no Telegram panel; it only takes orders from the master over the SSH tunnel and
binds to 127.0.0.1 so it is never exposed publicly.

SCAFFOLD / OFF BY DEFAULT: this runs only when the process is started in worker
mode on a real second server (`MKWL_MODE=worker`). FastAPI/uvicorn are imported
lazily, so importing this module never fails on the master. It needs a real
server + browser to exercise, so it is compile-checked here, not unit-tested.

Endpoints (all require  Authorization: Bearer <MKWL_WORKER_API_TOKEN>):
  GET  /ping                    -> {ok: true}                 (instant liveness)
  GET  /health                  -> {ok, warm, ...}            (pool snapshot)
  POST /login/start  {phone}    -> {ok, next|error}           (portal login begin)
  POST /login/code   {phone,code} -> {ok, account|error}      (finish login)
  POST /send/start   {payload}  -> {ok, job_id}               (queue a send)
  GET  /send/status/{job_id}    -> {ok, sent, failed, done}
"""
from __future__ import annotations

import os

try:
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse
    import uvicorn
    _HAVE_WEB = True
except ImportError:  # pragma: no cover
    _HAVE_WEB = False


def is_worker_mode() -> bool:
    return str(os.environ.get("MKWL_MODE", "")).strip().lower() == "worker"


def _token() -> str:
    return str(os.environ.get("MKWL_WORKER_API_TOKEN", "") or "")


def _bind_host() -> str:
    # Loopback only: the master reaches it through an SSH tunnel, never the public
    # internet. Override only for local testing.
    return str(os.environ.get("MKWL_WORKER_BIND", "127.0.0.1") or "127.0.0.1")


def _port() -> int:
    try:
        from config import config
        return int(getattr(config, "WORKER_API_PORT", 8799))
    except Exception:  # noqa: BLE001
        return 8799


def build_app():
    """Build the worker FastAPI app. Raises if fastapi is unavailable."""
    if not _HAVE_WEB:
        raise RuntimeError("fastapi/uvicorn not installed on this worker")

    app = FastAPI(title="Mkwlsoso Worker", docs_url=None, redoc_url=None)

    def _auth(authorization: str | None) -> None:
        expected = _token()
        if not expected:
            raise HTTPException(status_code=500, detail="worker token not configured")
        if not authorization or authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="unauthorized")

    async def _body(req) -> dict:
        try:
            v = await req.json()
            return v if isinstance(v, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    @app.get("/health")
    async def health(authorization: str = Header(default=None)):
        _auth(authorization)
        try:
            from capture.pool import pool
            return {"ok": True, **pool.status()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": True, "pool": f"unavailable: {type(exc).__name__}"}

    @app.post("/login/start")
    async def login_start(req: Request, authorization: str = Header(default=None)):
        _auth(authorization)
        body = await _body(req)
        from portal import login_adapter
        result = await login_adapter.begin(str(body.get("phone") or ""))
        return JSONResponse(result)

    @app.post("/login/code")
    async def login_code(req: Request, authorization: str = Header(default=None)):
        _auth(authorization)
        body = await _body(req)
        from portal import login_adapter
        from portal.attempts import registry
        attempt = registry.get(str(body.get("attempt_id") or ""))
        if attempt is None:
            return JSONResponse({"error": "attempt not found", "code": "attempt_not_found"},
                                status_code=400)
        out = await login_adapter.submit_code(attempt, str(body.get("code") or ""),
                                             attempt["token"])
        return JSONResponse(out)

    @app.post("/send/start")
    async def send_start(req: Request, authorization: str = Header(default=None)):
        _auth(authorization)
        body = await _body(req)
        # The master hands over the same send payload the bot builds; the worker
        # runs it through the project's own JobManager on this machine.
        from bot.runner import manager
        account = str(body.get("account") or "")
        job = await manager.run_send(account, body.get("content") or {},
                                     body.get("settings") or {},
                                     _null_report, body.get("recipients"),
                                     account_phone=body.get("phone"))
        return JSONResponse({"ok": True, "job_id": job.job_id})

    @app.get("/send/status/{job_id}")
    async def send_status(job_id: str, authorization: str = Header(default=None)):
        _auth(authorization)
        from bot.runner import manager
        job = manager.get_job(job_id) if hasattr(manager, "get_job") else None
        if job is None:
            return JSONResponse({"ok": False, "error": "job not found"}, status_code=404)
        s = job.summary or {}
        done = job.task is None or job.task.done()
        return JSONResponse({"ok": True, "done": done, **s})

    return app


async def _null_report(_text: str) -> None:
    """Workers do not post to Telegram; the master aggregates. Swallow reports."""
    return None


def main() -> int:
    """Entry point for `python -m worker.agent` on a worker server."""
    if not is_worker_mode():
        print("[worker] MKWL_MODE is not 'worker'; refusing to start the agent.",
              flush=True)
        return 2
    if not _HAVE_WEB:
        print("[worker] fastapi/uvicorn not installed (pip install fastapi uvicorn).",
              flush=True)
        return 2
    if not _token():
        print("[worker] MKWL_WORKER_API_TOKEN is not set.", flush=True)
        return 2
    uvicorn.run(build_app(), host=_bind_host(), port=_port(), log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
