"""Worker health checks — TCP reachability + API /ping, cached with backoff.

Modelled on the source project's worker.check_worker/check_all, adapted to this
project's JSON store and made fully testable: the two probes (a TCP connect and
the worker-API /ping) are injectable, so the classification, parallelism, the
per-worker deadline and the failure backoff can all be exercised with no network
and no second server.

Status meaning:
  ok       — SSH port reachable AND the worker API answered /ping
  blocked  — reachable but the API did not answer (agent down / wrong token)
  down     — the SSH port itself is unreachable
  local    — the master worker (runs in-process; always ok, never probed)

A worker that keeps failing is checked less and less often (store.next_check_due
doubles the interval per consecutive failure up to 10 min), so a dead server can
never turn the health loop into a hot loop.
"""
from __future__ import annotations

import asyncio
import time

from . import store


async def _tcp_ping(host: str, port: int, timeout: float = 3.0) -> int:
    """ms to open a TCP connection to host:port, or -1 on failure."""
    start = time.monotonic()
    try:
        fut = asyncio.open_connection(host, int(port or 22))
        _reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return int((time.monotonic() - start) * 1000)
    except Exception:  # noqa: BLE001
        return -1


async def _api_ping(worker: dict, timeout: float = 8.0) -> bool:
    """Ask the worker API /ping through the tunnel. Real implementation lives in
    worker/transport.py; imported lazily so health works (and tests run) without
    asyncssh/httpx present."""
    try:
        from . import transport
        return await transport.api_ping(worker, timeout=timeout)
    except Exception:  # noqa: BLE001
        return False


async def check_worker(worker: dict, *, tcp_ping=None, api_ping=None,
                       persist: bool = True) -> dict:
    """Check ONE worker. `tcp_ping`/`api_ping` can be injected (tests). Returns a
    summary dict and (by default) records it in the store."""
    if store.is_local(worker):
        summary = {"id": worker["id"], "tag": worker["tag"], "status": "ok",
                   "ping_ms": 1, "detail": ""}
        return summary

    tcp = tcp_ping or _tcp_ping
    api = api_ping or _api_ping
    ping = await tcp(worker["ip"], worker.get("ssh_port") or 22)
    if ping < 0:
        status, detail = "down", "ssh unreachable"
    elif await api(worker):
        status, detail = "ok", ""
    else:
        status, detail = "blocked", "worker API did not answer"
    if persist:
        store.set_health(int(worker["id"]), status, ping, detail)
    return {"id": worker["id"], "tag": worker["tag"], "status": status,
            "ping_ms": ping, "detail": detail}


async def check_all(*, only_due: bool = True, tcp_ping=None, api_ping=None,
                    per_worker_timeout: float = 15.0) -> list:
    """Check every enabled remote worker IN PARALLEL, each with its own deadline
    so one slow/dead worker can never stall the whole cycle. With only_due=True
    a worker is skipped until its backoff window has elapsed."""
    workers = [w for w in store.list_enabled() if not store.is_local(w)]
    if only_due:
        workers = [w for w in workers if store.due_for_check(w)]
    if not workers:
        return []

    async def _guarded(w):
        try:
            return await asyncio.wait_for(
                check_worker(w, tcp_ping=tcp_ping, api_ping=api_ping),
                timeout=per_worker_timeout)
        except asyncio.TimeoutError:
            store.set_health(int(w["id"]), "down", -1, "check timed out")
            return {"id": w["id"], "tag": w["tag"], "status": "down",
                    "ping_ms": -1, "detail": "timeout"}
        except Exception as exc:  # noqa: BLE001
            store.set_health(int(w["id"]), "down", -1, f"check error: {type(exc).__name__}")
            return {"id": w["id"], "tag": w["tag"], "status": "down",
                    "ping_ms": -1, "detail": repr(exc)[:120]}

    return await asyncio.gather(*[_guarded(w) for w in workers])


def healthy(worker: dict) -> bool:
    """A worker usable for new work: the local master always, a remote only when
    its last check said 'ok'. Never checked yet = tentatively usable if enabled."""
    if store.is_local(worker):
        return True
    status = worker.get("status")
    if status is None or status == "unchecked":
        return bool(worker.get("enabled"))
    return status == "ok"
