"""Live portal runtime status — a single in-memory snapshot.

This is the CURRENT state of the running tunnel/server (starting/running/failed/
off) plus the public URL and health fields. It is intentionally in memory: it
describes the live process, not history. History and per-attempt records live in
stats.py, which is on disk.
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.RLock()
_STATE: dict = {
    "status": "off",          # off | starting | running | failed
    "mode": "quick",          # quick | domain
    "url": "",
    "server": False,
    "tunnel": False,
    "dns": "unchecked",
    "ssl": "unchecked",
    "domain_ping": "unchecked",
    "detail": "",
    "since": 0.0,
}


def snapshot() -> dict:
    with _LOCK:
        return dict(_STATE)


def set_state(status: str, **fields) -> None:
    with _LOCK:
        _STATE["status"] = status
        _STATE["since"] = time.time()
        for key, value in fields.items():
            _STATE[key] = value


def update(**fields) -> None:
    with _LOCK:
        for key, value in fields.items():
            _STATE[key] = value


def clear_runtime(status: str, detail: str = "") -> None:
    """Reset the runtime-only fields when the tunnel/server is not up."""
    with _LOCK:
        _STATE.update({
            "status": status, "url": "", "server": False, "tunnel": False,
            "detail": detail, "since": time.time(),
        })
