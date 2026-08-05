"""Durable portal attempt statistics — JSON on disk, atomic writes.

The original (Makiioo) kept this in SQLite. This project stores everything as
JSON files written atomically (see bot/contacts_store.py, contacts_boost/
numbers.py), so the portal follows the same convention: one file,
DATA_DIR/portal_attempts.json, replaced via a .tmp rename so a crash mid-write
never corrupts it.

Fixing the original's weakness: attempt records are on disk, not only in memory,
so a restart keeps the history and `expire_stale()` closes any attempt that was
still 'pending' when the process died (a browser login cannot survive a restart,
so those are genuinely over).

An attempt moves: pending -> (started) -> success | expired | failed.
Only 'started' counts toward the success-rate denominator, so a phone typed but
abandoned before a code was requested does not drag the rate down.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from config import config

TERMINAL = {"success", "expired", "failed"}
_LOCK = threading.RLock()
#: Keep the file bounded: only the most recent N attempts are retained.
_MAX_ROWS = 500


def _path() -> Path:
    return config.DATA_DIR / "portal_attempts.json"


def _load() -> dict:
    p = _path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("attempts"), list):
                return data
        except Exception:  # noqa: BLE001 - a corrupt file must never break the portal
            pass
    return {"attempts": []}


def _save(data: dict) -> None:
    rows = data.get("attempts") or []
    if len(rows) > _MAX_ROWS:
        data["attempts"] = rows[-_MAX_ROWS:]
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _find(rows: list, attempt_id: str) -> dict | None:
    for row in rows:
        if row.get("attempt_id") == attempt_id:
            return row
    return None


def init() -> None:
    # Nothing to create for JSON; kept for API parity with the SQLite original.
    if not _path().is_file():
        _save({"attempts": []})


def create_attempt(attempt_id: str, phone: str, owner_hash: str,
                   created_at: float, expires_at: float) -> None:
    with _LOCK:
        data = _load()
        if _find(data["attempts"], attempt_id):
            return
        data["attempts"].append({
            "attempt_id": attempt_id, "phone": phone, "owner_hash": owner_hash,
            "status": "pending", "created_at": created_at,
            "expires_at": expires_at, "started_at": None, "finished_at": None,
            "wrong_code_events": 0, "account": None, "last_error": "",
        })
        _save(data)


def mark_started(attempt_id: str, now: float | None = None) -> bool:
    now = now or time.time()
    with _LOCK:
        data = _load()
        row = _find(data["attempts"], attempt_id)
        if not row or row["status"] != "pending" or row.get("started_at"):
            return False
        row["started_at"] = now
        _save(data)
        return True


def wrong_code(attempt_id: str, detail: str = "", now: float | None = None) -> int:
    with _LOCK:
        data = _load()
        row = _find(data["attempts"], attempt_id)
        if not row:
            return 0
        if row["status"] == "pending":
            row["wrong_code_events"] = int(row.get("wrong_code_events") or 0) + 1
            row["last_error"] = str(detail)[:240]
            _save(data)
        return int(row.get("wrong_code_events") or 0)


def finish(attempt_id: str, status: str, *, account=None,
           error: str = "", now: float | None = None) -> bool:
    if status not in TERMINAL:
        raise ValueError(f"invalid portal terminal status: {status}")
    now = now or time.time()
    with _LOCK:
        data = _load()
        row = _find(data["attempts"], attempt_id)
        if not row or row["status"] != "pending":
            return False
        row["status"] = status
        row["finished_at"] = now
        row["account"] = account
        row["last_error"] = str(error)[:240]
        _save(data)
        return True


def expire_stale(now: float | None = None) -> int:
    """Close attempts left 'pending' by a previous process. A browser login
    cannot survive a restart, so a pending row at boot is genuinely over."""
    now = now or time.time()
    with _LOCK:
        data = _load()
        closed = 0
        for row in data["attempts"]:
            if row.get("status") == "pending":
                row["status"] = "expired"
                row["finished_at"] = now
                row["last_error"] = "process restart"
                closed += 1
        if closed:
            _save(data)
        return closed


def _summary_over(rows: list, since: float | None) -> dict:
    started = success = expired = failed = wrong = 0
    now = time.time()
    pending = 0
    for row in rows:
        st = row.get("status")
        if st == "pending" and float(row.get("expires_at") or 0) > now:
            pending += 1
        in_window_start = since is None or float(row.get("started_at") or 0) >= since
        in_window_fin = since is None or float(row.get("finished_at") or 0) >= since
        if row.get("started_at") and in_window_start:
            started += 1
        if st == "success" and in_window_fin:
            success += 1
        elif st == "expired" and in_window_fin:
            expired += 1
        elif st == "failed" and in_window_fin:
            failed += 1
        if in_window_fin or since is None:
            wrong += int(row.get("wrong_code_events") or 0)
    rate = round(success * 100 / started, 1) if started else 0.0
    return {"started": started, "success": success, "expired": expired,
            "failed": failed, "wrong_code_events": wrong, "pending": pending,
            "rate": rate}


def summary() -> dict:
    init()
    with _LOCK:
        rows = _load()["attempts"]
    midnight = _local_midnight()
    return {"today": _summary_over(rows, midnight),
            "total": _summary_over(rows, None)}


def recent(limit: int = 20) -> list:
    with _LOCK:
        rows = _load()["attempts"]
    return list(reversed(rows[-max(1, min(int(limit), 100)):]))


def pending_count() -> int:
    now = time.time()
    with _LOCK:
        rows = _load()["attempts"]
    return sum(1 for r in rows
               if r.get("status") == "pending" and float(r.get("expires_at") or 0) > now)


def _local_midnight() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                        lt.tm_wday, lt.tm_yday, lt.tm_isdst))
