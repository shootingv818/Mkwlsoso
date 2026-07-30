"""Standby pool for browser sessions.

Opening Chromium on the target host was measured at 158-203 SECONDS. Every job
paid that: a login, then Update Contacts, then a send, three launches for one
sitting. This pool pays it once and keeps the session on STANDBY - like a guard at
the gate, not a parade: nothing is opened until a job asks, and what is kept warm
is bounded and recycled.

Rules (each one exists because of a measured problem or a known Playwright
pitfall - long-lived contexts leak, so reuse must be bounded [1][2]):

* A session is created ONLY when a job asks for one.
* After release it stays warm for `idle_ttl` seconds, then closes itself.
* At most `max_open` sessions are warm at once (this host has 961 MB of RAM, so
  the default is 1); the least recently used one is closed to make room.
* A session is recycled after `max_uses` leases or `max_age` seconds even if it
  looks fine, because a browser kept alive forever grows.
* Before reuse it is health-checked; a dead page is discarded, not handed out.
* A lease that ends in cancellation (Force Stop) is discarded, never reused: the
  page may be in the middle of an operation.
* Sessions are keyed by (account, headed, init-script), because a session opened
  without the worker hook cannot serve a job that needs the hook.
* One lease per account at a time, so two jobs can never drive the same page.

Set MKWL_SESSION_POOL=0 to go back to a fresh browser per job.

[1] https://instantproxies.com/blog/browser-context-reuse-vs-relaunch-stability-at-scale/
[2] https://webscraping.ai/faq/playwright/what-are-the-memory-management-best-practices-when-running-long-playwright-sessions
Content from both was rephrased for compliance with licensing restrictions.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from capture.browser import BrowserSession, open_session


def _flag(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _enabled() -> bool:
    return _flag("MKWL_SESSION_POOL", "1").lower() not in ("0", "false", "no", "off")


class _Entry:
    __slots__ = ("session", "key", "created", "last_used", "uses", "leased")

    def __init__(self, session: BrowserSession, key: tuple) -> None:
        self.session = session
        self.key = key
        self.created = time.time()
        self.last_used = time.time()
        self.uses = 0
        self.leased = False


class SessionPool:
    """Keeps a small number of browser sessions on standby."""

    def __init__(self, idle_ttl: float | None = None, max_open: int | None = None,
                 max_uses: int | None = None, max_age: float | None = None) -> None:
        self.idle_ttl = float(idle_ttl if idle_ttl is not None
                              else _flag("MKWL_POOL_IDLE_TTL", "240"))
        self.max_open = int(max_open if max_open is not None
                            else _flag("MKWL_POOL_MAX_OPEN", "1"))
        self.max_uses = int(max_uses if max_uses is not None
                            else _flag("MKWL_POOL_MAX_USES", "25"))
        self.max_age = float(max_age if max_age is not None
                             else _flag("MKWL_POOL_MAX_AGE", "1800"))
        self._entries: dict[str, _Entry] = {}          # account -> entry
        self._locks: dict[str, asyncio.Lock] = {}      # one lease per account
        self._reaper: asyncio.Task | None = None
        # Hard cap on LIVE browsers, held for the whole lease. A count of
        # `self._entries` was not enough: an entry is only registered after
        # start() finishes, so two leases could both look at an empty pool and
        # both launch - which is exactly how two Chromiums ended up on a 961 MB
        # host and 'load Eitaa web' went from ~60s to 272s.
        self._slots: asyncio.Semaphore | None = None
        self.stats = {"created": 0, "reused": 0, "expired": 0, "evicted": 0,
                      "recycled": 0, "discarded": 0, "saved_launches": 0}

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _key(headed, init_script_path) -> tuple:
        script = str(init_script_path) if init_script_path else ""
        return (bool(headed), script)

    def _lock(self, account: str) -> asyncio.Lock:
        lock = self._locks.get(account)
        if lock is None:
            lock = self._locks[account] = asyncio.Lock()
        return lock

    async def _close(self, entry: _Entry, why: str) -> None:
        self.stats[why] = self.stats.get(why, 0) + 1
        try:
            await entry.session.close()
        except Exception:  # noqa: BLE001 - closing must never raise into a job
            pass

    async def _healthy(self, entry: _Entry) -> bool:
        page = getattr(entry.session, "page", None)
        if page is None:
            return False
        try:
            if getattr(page, "is_closed", None) and page.is_closed():
                return False
            # Cheapest possible proof that the renderer still answers.
            await asyncio.wait_for(page.evaluate("() => 1"), timeout=10)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _wait_for_slot(self, keep: str, timeout: float = 600.0) -> None:
        """Block until this host can afford another browser.

        The cap used to be advisory: when every other session was leased,
        `_make_room` gave up and the caller launched anyway. On 961 MB that
        produced two Chromiums at once and 'load Eitaa web' went from ~60s to
        272s. Waiting is faster than sharing.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            await self._make_room(keep)
            others = [a for a in self._entries if a != keep]
            if len(others) < self.max_open:
                return
            await asyncio.sleep(1.0)

    async def _make_room(self, keep: str) -> None:
        while len([e for a, e in self._entries.items() if a != keep]) >= max(
                0, self.max_open - 1) and len(self._entries) >= self.max_open:
            idle = [(a, e) for a, e in self._entries.items()
                    if a != keep and not e.leased]
            if not idle:
                return
            account, entry = min(idle, key=lambda kv: kv[1].last_used)
            self._entries.pop(account, None)
            await self._close(entry, "evicted")

    # ---- the lease -----------------------------------------------------

    @asynccontextmanager
    async def lease(self, account: str, headed=None,
                    init_script_path: str | Path | None = None
                    ) -> AsyncIterator[BrowserSession]:
        """Borrow a session for `account`, warm if one is on standby."""
        if not _enabled():
            async with open_session(account, headed=headed,
                                    init_script_path=init_script_path) as session:
                yield session
            return

        key = self._key(headed, init_script_path)
        # The lock is held for the WHOLE lease, not just while picking a session.
        # Holding it only during acquisition let two jobs drive the same page at
        # the same time (caught by scenario 9 in bot/tests/test_scenarios.py).
        if self._slots is None:
            self._slots = asyncio.Semaphore(max(1, self.max_open))
        lock = self._lock(account)
        await lock.acquire()
        slot_held = False
        try:
            self._start_reaper()
            entry = self._entries.get(account)

            if entry is not None and entry.key != key:
                # A different session shape (e.g. needs the worker hook now).
                self._entries.pop(account, None)
                await self._close(entry, "discarded")
                entry = None
            if entry is not None and (entry.uses >= self.max_uses
                                      or (time.time() - entry.created) > self.max_age):
                self._entries.pop(account, None)
                await self._close(entry, "recycled")
                entry = None
            if entry is not None and not await self._healthy(entry):
                self._entries.pop(account, None)
                await self._close(entry, "discarded")
                entry = None

            if entry is None:
                # Wait for a real slot before launching anything.
                await self._slots.acquire()
                slot_held = True
                await self._make_room(account)
                session = BrowserSession(account, headed=headed,
                                         init_script_path=init_script_path)
                await session.start()
                entry = _Entry(session, key)
                self._entries[account] = entry
                self.stats["created"] += 1
            else:
                await self._slots.acquire()
                slot_held = True
                self.stats["reused"] += 1
                self.stats["saved_launches"] += 1

            entry.leased = True
            entry.uses += 1
            entry.last_used = time.time()

            try:
                yield entry.session
            except BaseException as exc:  # noqa: BLE001
                # A cancelled or failed job may leave the page mid-operation; do
                # not hand that to the next job.
                self._entries.pop(account, None)
                entry.leased = False
                await self._close(entry, "discarded")
                raise exc
            else:
                entry.leased = False
                entry.last_used = time.time()
        finally:
            if slot_held:
                self._slots.release()
            lock.release()

    # ---- housekeeping --------------------------------------------------

    def _start_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        # The reaper is started BEFORE a session finishes launching (that takes
        # 158-203s here), so it must not exit just because the pool looks empty on
        # its first tick - that left standby browsers open forever.
        empty_ticks = 0
        try:
            while True:
                await asyncio.sleep(15)
                if not self._entries:
                    empty_ticks += 1
                    if empty_ticks >= 40:   # ~10 minutes with nothing to watch
                        return
                    continue
                empty_ticks = 0
                now = time.time()
                for account, entry in list(self._entries.items()):
                    if entry.leased:
                        continue
                    if (now - entry.last_used) >= self.idle_ttl:
                        self._entries.pop(account, None)
                        await self._close(entry, "expired")
                        print(f"[pool] {account}: standby session closed after "
                              f"{self.idle_ttl:.0f}s idle", flush=True)
        except asyncio.CancelledError:
            return

    def warm_accounts(self) -> list[str]:
        return [a for a, e in self._entries.items() if not e.leased]

    def status(self) -> dict:
        now = time.time()
        return {
            "enabled": _enabled(),
            "warm": len(self._entries),
            "max_open": self.max_open,
            "idle_ttl": self.idle_ttl,
            "accounts": {a: {"leased": e.leased, "uses": e.uses,
                             "idle": round(now - e.last_used, 1),
                             "age": round(now - e.created, 1)}
                         for a, e in self._entries.items()},
            **self.stats,
        }

    async def close_all(self, account: str | None = None) -> int:
        """Close standby sessions (all of them, or one account's). Returns count."""
        closed = 0
        for acc, entry in list(self._entries.items()):
            if account is not None and acc != account:
                continue
            if entry.leased:
                continue
            self._entries.pop(acc, None)
            await self._close(entry, "expired")
            closed += 1
        if not self._entries and self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        return closed


#: The panel uses one pool for every job.
pool = SessionPool()
