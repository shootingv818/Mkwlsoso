"""Worker registry + account→worker affinity — JSON on disk, atomic writes.

Makiioo kept workers in SQLite; this project is JSON everywhere, so the registry
is a JSON file written atomically (tmp + replace), like contacts_store and the
portal stats.

    DATA_DIR/workers.json
    {
      "workers": [
        {"id": 1, "tag": "#W0_100", "is_master": true,  "enabled": true,
         "ip": "local", "ssh_port": 0, "api_port": 0, "created": ...,
         "status": "ok", "ping_ms": 1, "health_ts": ...},
        {"id": 2, "tag": "#W3_412", "is_master": false, "enabled": true,
         "ip": "1.2.3.4", "ssh_port": 22, "api_port": 8799, ...}
      ],
      "assign": {"989123456789": 2}     # account -> worker id (session affinity)
    }

Secrets (ssh password, api token) are NOT stored here in plaintext: the master
stores them via the project's own encrypted settings; this file holds only
non-secret routing state. (Kept simple for the tested core; the transport layer
adds the encrypted fields when a real worker is provisioned.)
"""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

from config import config

_LOCK = threading.RLock()


def _path() -> Path:
    return config.DATA_DIR / "workers.json"


def _load() -> dict:
    p = _path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("workers"), list):
                data.setdefault("assign", {})
                return data
        except Exception:  # noqa: BLE001
            pass
    return {"workers": [], "assign": {}}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _next_id(workers: list) -> int:
    return (max((int(w.get("id") or 0) for w in workers), default=0)) + 1


def gen_tag(existing: set, is_master: bool = False) -> str:
    """Random worker tag like '#W3_412'; master uses the '#W0_xxx' family."""
    for _ in range(200):
        lead = "0" if is_master else str(random.randint(1, 9))
        tag = f"#W{lead}_{random.randint(100, 999)}"
        if tag not in existing:
            return tag
    return f"#W{'0' if is_master else '9'}_{random.randint(1000, 9999)}"


# ---- reads ---------------------------------------------------------------

def list_workers() -> list:
    return list(_load()["workers"])


def list_enabled() -> list:
    return [w for w in _load()["workers"] if w.get("enabled")]


def get(worker_id: int) -> dict | None:
    for w in _load()["workers"]:
        if int(w.get("id") or 0) == int(worker_id):
            return w
    return None


def master() -> dict | None:
    for w in _load()["workers"]:
        if w.get("is_master"):
            return w
    return None


def is_local(worker: dict | None) -> bool:
    return bool(worker and worker.get("is_master"))


def count_accounts_on(worker_id: int) -> int:
    assign = _load()["assign"]
    return sum(1 for wid in assign.values() if int(wid) == int(worker_id))


# ---- writes --------------------------------------------------------------

def ensure_master() -> dict:
    """Create the local master-worker row once. It runs jobs in-process, so a
    setup with no remote workers behaves exactly like the bot always has."""
    with _LOCK:
        data = _load()
        for w in data["workers"]:
            if w.get("is_master"):
                return w
        tags = {w["tag"] for w in data["workers"]}
        row = {"id": _next_id(data["workers"]), "tag": gen_tag(tags, is_master=True),
               "is_master": True, "enabled": True, "ip": "local",
               "ssh_port": 0, "api_port": 0, "created": time.time(),
               "status": "ok", "ping_ms": 1, "health_ts": time.time()}
        data["workers"].append(row)
        _save(data)
        return row


def add_remote(ip: str, ssh_port: int, api_port: int, tag: str | None = None) -> int:
    with _LOCK:
        data = _load()
        tags = {w["tag"] for w in data["workers"]}
        row = {"id": _next_id(data["workers"]),
               "tag": tag or gen_tag(tags, is_master=False),
               "is_master": False, "enabled": True, "ip": ip,
               "ssh_port": int(ssh_port or 22), "api_port": int(api_port),
               "created": time.time(), "status": "unchecked",
               "ping_ms": -1, "health_ts": 0.0}
        data["workers"].append(row)
        _save(data)
        return row["id"]


def set_enabled(worker_id: int, enabled: bool) -> None:
    with _LOCK:
        data = _load()
        for w in data["workers"]:
            if int(w.get("id") or 0) == int(worker_id):
                w["enabled"] = bool(enabled)
        _save(data)


def set_health(worker_id: int, status: str, ping_ms: int = -1) -> None:
    with _LOCK:
        data = _load()
        for w in data["workers"]:
            if int(w.get("id") or 0) == int(worker_id):
                w["status"] = status
                w["ping_ms"] = int(ping_ms)
                w["health_ts"] = time.time()
        _save(data)


def remove(worker_id: int) -> bool:
    with _LOCK:
        data = _load()
        before = len(data["workers"])
        data["workers"] = [w for w in data["workers"]
                           if int(w.get("id") or 0) != int(worker_id)]
        # Unpin any accounts that were on it (they fall back to the master).
        data["assign"] = {a: wid for a, wid in data["assign"].items()
                          if int(wid) != int(worker_id)}
        changed = len(data["workers"]) != before
        if changed:
            _save(data)
        return changed


# ---- account affinity ----------------------------------------------------

def assigned_worker_id(account: str) -> int | None:
    wid = _load()["assign"].get(str(account))
    return int(wid) if wid is not None else None


def assign(account: str, worker_id: int) -> None:
    with _LOCK:
        data = _load()
        data["assign"][str(account)] = int(worker_id)
        _save(data)


def unassign(account: str) -> None:
    with _LOCK:
        data = _load()
        if str(account) in data["assign"]:
            del data["assign"][str(account)]
            _save(data)


def worker_for_account(account: str) -> dict | None:
    """The worker that owns an account (session affinity), or the master."""
    wid = assigned_worker_id(account)
    if wid is not None:
        w = get(wid)
        if w:
            return w
    return master()
