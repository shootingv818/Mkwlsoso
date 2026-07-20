"""Browser lifecycle for capture.

Each Eitaa account gets its own persistent Chromium profile so sessions never
mix. The persistent context keeps cookies/localStorage between runs, which is
what lets the owner log in once (manually) and reuse the session afterwards.

A CDP session is attached to the page to observe raw Network/WebSocket traffic
that the high-level Playwright events do not always fully expose.
"""

from __future__ import annotations

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

    def __init__(self, account: str, headed: bool | None = None) -> None:
        self.account = account
        self.headed = config.HEADED if headed is None else headed
        self.profile_dir: Path = config.profile_dir(account)
        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.cdp: CDPSession | None = None

    async def start(self) -> "BrowserSession":
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=not self.headed,
            args=_LAUNCH_ARGS,
            viewport={"width": 1280, "height": 850},
            locale="fa-IR",
        )
        # Reuse an existing page if the profile restored one, else open a page.
        pages = self.context.pages
        self.page = pages[0] if pages else await self.context.new_page()
        self.cdp = await self.context.new_cdp_session(self.page)
        return self

    async def goto(self, url: str | None = None) -> None:
        assert self.page is not None
        await self.page.goto(url or config.EITAA_WEB_URL, wait_until="domcontentloaded")

    async def close(self) -> None:
        try:
            if self.context is not None:
                await self.context.close()
        finally:
            if self._pw is not None:
                await self._pw.stop()


@asynccontextmanager
async def open_session(account: str, headed: bool | None = None) -> AsyncIterator[BrowserSession]:
    session = BrowserSession(account, headed=headed)
    await session.start()
    try:
        yield session
    finally:
        await session.close()
