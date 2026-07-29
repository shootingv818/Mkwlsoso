"""Browser lifecycle for capture.

Each Eitaa account gets its own persistent Chromium profile so sessions never
mix. The persistent context keeps cookies/localStorage between runs, which is
what lets the owner log in once (manually) and reuse the session afterwards.

A CDP session is attached to the page to observe raw Network/WebSocket traffic
that the high-level Playwright events do not always fully expose.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from playwright.async_api import BrowserContext, CDPSession, Page, async_playwright

from config import config


# Chromium launch flags. We deliberately do NOT add stealth/anti-detection
# flags: this tool automates the owner's own accounts, not evasion.
_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


class BrowserSession:
    """Wraps a persistent context + page + CDP session for one account."""

    def __init__(
        self,
        account: str,
        headed: bool | None = None,
        init_script_path: str | Path | None = None,
    ) -> None:
        self.account = account
        self.headed = config.HEADED if headed is None else headed
        self.profile_dir: Path = config.profile_dir(account)
        self.init_script_path = Path(init_script_path) if init_script_path else None
        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.cdp: CDPSession | None = None

    def _profile_in_use(self) -> int | None:
        """PID of a LIVE process already using this profile, else None.

        Deleting a lock that a running Chromium still holds would corrupt that
        browser's session, so the leftover-lock cleanup below must never guess.
        """
        target = str(self.profile_dir.resolve())
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return None  # not Linux / no procfs -> treat as "cannot prove it is dead"
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
            except OSError:
                continue
            if not cmd or "chrome" not in cmd.lower():
                continue
            if target in cmd or f"--user-data-dir={self.profile_dir}" in cmd:
                return int(pid)
        return None

    def _clear_stale_locks(self) -> list[str]:
        """Remove leftover Chromium singleton locks from a CRASHED run.

        A killed browser (OOM, force stop, service restart) leaves these behind
        and the next launch dies with "profile appears to be in use", taking the
        whole job with it. Only removed once no live process is using the
        profile -- verified through /proc, not assumed.
        """
        alive = self._profile_in_use()
        if alive is not None:
            print(f"[browser] profile {self.account} is really in use by pid "
                  f"{alive}; leaving its lock alone", flush=True)
            return []
        removed = []
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            p = self.profile_dir / name
            try:
                if p.is_symlink() or p.exists():
                    p.unlink()
                    removed.append(name)
            except OSError:
                pass
        return removed

    @staticmethod
    def _is_lock_error(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return any(s in msg for s in (
            "already in use", "existing browser session", "profile appears to be in use",
            "singletonlock", "cannot create a file when that file already exists"))

    async def start(self) -> "BrowserSession":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        try:
            self.context = await self._launch()
        except Exception as exc:  # noqa: BLE001
            if not self._is_lock_error(exc):
                await self._stop_playwright()
                raise
            removed = self._clear_stale_locks()
            if not removed:
                await self._stop_playwright()
                raise
            print(f"[browser] profile {self.account} had a stale lock; cleared "
                  f"{removed} and retrying", flush=True)
            try:
                self.context = await self._launch()
            except Exception:  # noqa: BLE001
                await self._stop_playwright()
                raise
        # From here a REAL browser is running: any failure must close it, or the
        # process survives holding the profile lock and blocks every later job.
        try:
            # Inject instrumentation BEFORE any page script runs (deep capture).
            if self.init_script_path and self.init_script_path.is_file():
                await self.context.add_init_script(
                    script=self.init_script_path.read_text(encoding="utf-8")
                )
            # Reuse an existing page if the profile restored one, else open one.
            pages = self.context.pages
            self.page = pages[0] if pages else await self.context.new_page()
            self.cdp = await self.context.new_cdp_session(self.page)
        except Exception:  # noqa: BLE001
            await self.close()
            raise
        return self

    async def goto(self, url: str | None = None) -> None:
        assert self.page is not None
        await self.page.goto(url or config.EITAA_WEB_URL, wait_until="domcontentloaded")

    async def _launch(self) -> BrowserContext:
        return await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=not self.headed,
            args=_LAUNCH_ARGS,
            viewport={"width": 1280, "height": 850},
            locale="fa-IR",
        )

    async def _stop_playwright(self) -> None:
        """Stop the Playwright driver process. Missing this leaks a node process
        per session; one was found alive 28 minutes after its browser was gone,
        which matters a lot on a 1 GB host."""
        if self._pw is None:
            return
        try:
            await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._pw = None

    async def close(self) -> None:
        try:
            if self.context is not None:
                await self.context.close()
        except Exception:  # noqa: BLE001
            pass  # a failed close must still stop the driver below
        finally:
            self.context = None
            self.page = None
            self.cdp = None
            await self._stop_playwright()


@asynccontextmanager
async def open_session(
    account: str,
    headed: bool | None = None,
    init_script_path: str | Path | None = None,
) -> AsyncIterator[BrowserSession]:
    session = BrowserSession(account, headed=headed, init_script_path=init_script_path)
    await session.start()
    try:
        yield session
    finally:
        await session.close()
