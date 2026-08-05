"""Master -> worker transport: an SSH tunnel to the worker's loopback API.

Modelled on the source project's worker.py tunnel layer, adapted to this
project. The worker API binds to 127.0.0.1 on the remote server (never public);
the master opens an SSH local-port-forward to it and calls it over that tunnel.

SCAFFOLD / OFF BY DEFAULT: nothing here runs unless a real remote worker is
added and the fleet is used. asyncssh and httpx are imported lazily INSIDE the
functions, so importing this module never fails on a box without them (the
master needs `pip install asyncssh httpx` only when it actually drives remote
workers). This layer needs a real second server to exercise, so it is not unit
-tested here; the pure-Python registry/selection/health it sits on top of are.
"""
from __future__ import annotations

import asyncio
import time

# worker_id -> {"conn", "listener", "local_port"}
_tunnels: dict = {}
_locks: dict = {}


class TransportError(Exception):
    pass


def _lock_for(worker_id: int) -> asyncio.Lock:
    lock = _locks.get(worker_id)
    if lock is None:
        lock = _locks[worker_id] = asyncio.Lock()
    return lock


async def _ssh_connect(worker: dict, keepalive: bool = True):
    """Open an SSH connection to a worker with hard timeouts (never hangs)."""
    import asyncssh  # lazy
    base = dict(
        host=worker["ip"], port=int(worker.get("ssh_port") or 22),
        username=worker.get("ssh_user") or "root",
        password=worker.get("ssh_pass") or None,
        known_hosts=None,           # personal tool: trust on first use
        login_timeout=8,
    )
    if keepalive:
        base["keepalive_interval"] = 15
        base["keepalive_count_max"] = 3

    async def _do():
        try:
            return await asyncssh.connect(connect_timeout=8, **base)
        except TypeError:           # very old asyncssh without connect_timeout
            return await asyncssh.connect(**base)

    return await asyncio.wait_for(_do(), timeout=10)


async def open_tunnel(worker: dict) -> int:
    """Open (or reuse) an SSH local-port-forward to the worker API. Returns the
    LOCAL port on the master that maps to the worker's api_port."""
    wid = int(worker["id"])
    async with _lock_for(wid):
        existing = _tunnels.get(wid)
        if existing:
            return existing["local_port"]
        conn = await _ssh_connect(worker, keepalive=True)
        listener = await conn.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1", int(worker["api_port"]))
        local_port = listener.get_port()
        _tunnels[wid] = {"conn": conn, "listener": listener, "local_port": local_port}
        return local_port


async def close_tunnel(worker_id: int) -> None:
    t = _tunnels.pop(int(worker_id), None)
    if not t:
        return
    for key in ("listener", "conn"):
        try:
            t[key].close()
        except Exception:  # noqa: BLE001
            pass


async def api_call(worker: dict, method: str, path: str, payload: dict | None = None,
                   timeout: int = 120) -> dict:
    """Call the worker API through the tunnel. Raises TransportError on failure."""
    import httpx  # lazy
    try:
        local_port = await open_tunnel(worker)
    except Exception as exc:  # noqa: BLE001
        raise TransportError(f"tunnel: {type(exc).__name__}: {exc}") from exc
    token = worker.get("api_token") or ""
    url = f"http://127.0.0.1:{local_port}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001
        # a broken tunnel is the usual cause -> drop it so the next call reopens
        await close_tunnel(int(worker["id"]))
        raise TransportError(f"api: {type(exc).__name__}: {str(exc)[:120]}") from exc


async def api_ping(worker: dict, timeout: float = 8.0) -> bool:
    """The health probe used by worker/health.py. True iff /ping answered ok."""
    try:
        data = await api_call(worker, "GET", "/ping", timeout=int(timeout))
        return bool(data.get("ok"))
    except Exception:  # noqa: BLE001
        return False


async def shutdown() -> None:
    """Close every open tunnel (call on master shutdown)."""
    for wid in list(_tunnels):
        await close_tunnel(wid)


def tunnel_status() -> dict:
    return {wid: {"local_port": t["local_port"]} for wid, t in _tunnels.items()}


# ---- convenience wrappers the dispatcher will use (thin, faithful to the API) ----

async def remote_login_start(worker: dict, phone: str) -> dict:
    return await api_call(worker, "POST", "/login/start", {"phone": phone}, timeout=90)


async def remote_login_code(worker: dict, phone: str, code: str) -> dict:
    return await api_call(worker, "POST", "/login/code",
                          {"phone": phone, "code": code}, timeout=120)


async def remote_send(worker: dict, payload: dict) -> dict:
    return await api_call(worker, "POST", "/send/start", payload, timeout=120)


async def remote_send_status(worker: dict, job_id: str) -> dict:
    return await api_call(worker, "GET", f"/send/status/{job_id}", timeout=30)
