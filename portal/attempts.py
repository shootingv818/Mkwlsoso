"""The attempt registry: one login-in-progress, its ownership token and locks.

This is the concurrency core, ported from Makiioo's app.py but pulled into its
own module so it can be tested without a web server or a browser. It keeps the
patterns that made the original correct:

  * a GLOBAL lock guards only the registry dict (add/remove);
  * a PER-ATTEMPT lock serialises the multi-step work on one attempt;
  * a retired per-attempt lock is dropped only after its owner AND every queued
    waiter has left, so two coroutines can never act on the same attempt;
  * every check is re-done after the lock is taken (state can change while you
    wait for the lock).

Ownership: each attempt carries a random token. Every follow-up call must
present it (compared with hmac.compare_digest), so knowing an attempt_id is not
enough to drive someone else's login.

Capacity: at most config.PORTAL_MAX_LOGINS live attempts (one Chromium each on a
2-core host). Over that, `capacity_position` reports how many are ahead so the
page can say "you are 2nd in line" instead of a bare "busy".
"""
from __future__ import annotations

import asyncio
import hmac
import secrets
import time

from config import config

from . import stats


class Attempts:
    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._gate: asyncio.Lock | None = None

    # ---- locks ----
    def gate(self) -> asyncio.Lock:
        if self._gate is None:
            self._gate = asyncio.Lock()
        return self._gate

    def lock_for(self, attempt_id: str) -> asyncio.Lock:
        return self._locks.setdefault(attempt_id, asyncio.Lock())

    async def retire_lock(self, attempt_id: str, lock: asyncio.Lock) -> None:
        """Drop a lock only once its owner and all queued waiters have gone."""
        while lock.locked():
            await asyncio.sleep(0.05)
        if self._locks.get(attempt_id) is lock and attempt_id not in self._items:
            self._locks.pop(attempt_id, None)

    # ---- introspection ----
    def get(self, attempt_id: str) -> dict | None:
        return self._items.get(attempt_id)

    def is_current(self, attempt: dict) -> bool:
        return self._items.get(attempt["id"]) is attempt

    def count(self) -> int:
        return len(self._items)

    def by_phone(self, phone: str) -> list:
        return [a for a in self._items.values() if a["phone"] == phone]

    def all_ids(self) -> list:
        return list(self._items)

    # ---- lifecycle ----
    def ttl(self) -> int:
        return max(60, int(getattr(config, "PORTAL_TTL_SECONDS", 600)))

    def capacity(self) -> int:
        return max(1, int(getattr(config, "PORTAL_MAX_LOGINS", 2)))

    def at_capacity(self) -> bool:
        return len(self._items) >= self.capacity()

    def capacity_position(self) -> int:
        """How many live attempts are already ahead (0 means a slot is free)."""
        return max(0, len(self._items) - self.capacity() + 1)

    def new_id_token(self) -> tuple[str, str]:
        return secrets.token_urlsafe(18), secrets.token_urlsafe(32)

    def create(self, attempt_id: str, token: str, phone: str) -> dict:
        created = time.time()
        attempt = {
            "id": attempt_id, "token": token, "phone": phone, "ctx": None,
            "created_at": created, "expires_at": created + self.ttl(),
            "stage": "starting", "tries": 0,
        }
        self._items[attempt_id] = attempt
        self.lock_for(attempt_id)
        return attempt

    def remaining(self, attempt: dict) -> int:
        return max(0, int(attempt["expires_at"] - time.time() + 0.999))

    def expired(self, attempt: dict) -> bool:
        return time.time() >= float(attempt["expires_at"])

    def verify(self, attempt: dict, token: str) -> bool:
        return bool(token) and hmac.compare_digest(attempt["token"], token)

    def pop(self, attempt_id: str) -> dict | None:
        return self._items.pop(attempt_id, None)

    def owner_hash(self, token: str) -> str:
        import hashlib
        return hashlib.sha256(token.encode()).hexdigest()


# One registry for the process.
registry = Attempts()
