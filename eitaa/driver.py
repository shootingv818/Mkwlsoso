"""Eitaa browser-driver.

Drives the real Eitaa Web client through its UI to perform actions like sending
a text message. Uses the logged-in persistent profile (from `cli.py login`).

Design notes:
- Resilient selectors: each UI element has several candidate selectors; the
  first that matches wins (see eitaa/selectors.py).
- Verification: after sending, we confirm the text appears as an outgoing
  bubble before reporting success.
- No protocol/crypto work: this only clicks and types like a human would.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Locator, Page, TimeoutError as PWTimeout

from config import config
from capture.browser import BrowserSession
from eitaa import selectors as S


class DriverError(Exception):
    pass


@dataclass
class SendResult:
    ok: bool
    to: str
    detail: str


async def _first_visible(page: Page, candidates: list[str], timeout: int = 8000) -> Optional[Locator]:
    """Return the first candidate locator that becomes visible, else None."""
    # Race all candidates by polling briefly.
    deadline = timeout
    step = 400
    waited = 0
    while waited <= deadline:
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if await loc.is_visible():
                    return loc
            except Exception:  # noqa: BLE001
                continue
        await page.wait_for_timeout(step)
        waited += step
    return None


class EitaaDriver:
    """High-level UI operations on top of a BrowserSession."""

    def __init__(self, session: BrowserSession) -> None:
        self.session = session

    @property
    def page(self) -> Page:
        assert self.session.page is not None
        return self.session.page

    async def open(self) -> None:
        await self.session.goto()
        # Give the SPA time to boot and restore the session.
        await self.page.wait_for_timeout(4000)

    async def is_logged_in(self) -> bool:
        """Heuristic: the chat list / search input exists when logged in."""
        loc = await _first_visible(self.page, S.SEARCH_INPUT, timeout=8000)
        return loc is not None

    async def list_chat_titles(self, limit: int = 20) -> list[str]:
        """Return the titles of currently visible chats (owner's own data).

        Printed on the owner's own machine so they can pick a name for --to.
        """
        js = """
        (limit) => {
          const nodes = document.querySelectorAll('.chatlist-chat');
          const out = [];
          for (const n of nodes) {
            const t = n.querySelector('.peer-title, .user-title, .dialog-title');
            const title = (t ? t.textContent : n.textContent || '').trim();
            if (title) out.push(title.slice(0, 60));
            if (out.length >= limit) break;
          }
          return out;
        }
        """
        try:
            return await self.page.evaluate(js, limit)
        except Exception:  # noqa: BLE001
            return []

    async def open_chat(self, query: str) -> None:
        search = await _first_visible(self.page, S.SEARCH_INPUT, timeout=10000)
        if search is None:
            raise DriverError("search input not found (are you logged in?)")
        await search.click()
        await search.fill("")
        await search.type(query, delay=40)
        # Search results load asynchronously; give them time.
        await self.page.wait_for_timeout(2500)

        result = await _first_visible(self.page, S.CHAT_RESULT, timeout=10000)
        if result is None:
            # Fallback: keyboard-select the top result.
            try:
                await search.press("ArrowDown")
                await search.press("Enter")
                await self.page.wait_for_timeout(1500)
                # If a composer appeared, treat as opened.
                box = await _first_visible(self.page, S.MESSAGE_INPUT, timeout=3000)
                if box is not None:
                    return
            except Exception:  # noqa: BLE001
                pass
            raise DriverError(f"no chat result for query: {query!r}")
        await result.click()
        await self.page.wait_for_timeout(1800)  # let the chat open

    async def send_text(self, query: str, text: str, verify: bool = True) -> SendResult:
        try:
            await self.open_chat(query)
        except DriverError as exc:
            return SendResult(ok=False, to=query, detail=str(exc))

        box = await _first_visible(self.page, S.MESSAGE_INPUT, timeout=8000)
        if box is None:
            return SendResult(ok=False, to=query, detail="message input not found")

        await box.click()
        await box.type(text, delay=15)
        await self.page.wait_for_timeout(300)

        sent = await self._click_send_or_enter(box)
        if not sent:
            return SendResult(ok=False, to=query, detail="could not trigger send")

        await self.page.wait_for_timeout(1200)

        if verify:
            ok = await self._verify_sent(text)
            return SendResult(
                ok=ok,
                to=query,
                detail="verified outgoing bubble" if ok else "sent but not verified in DOM",
            )
        return SendResult(ok=True, to=query, detail="sent (no verification)")

    async def _click_send_or_enter(self, box: Locator) -> bool:
        btn = await _first_visible(self.page, S.SEND_BUTTON, timeout=2500)
        if btn is not None:
            try:
                await btn.click()
                return True
            except Exception:  # noqa: BLE001
                pass
        # Fallback: press Enter in the composer.
        try:
            await box.press("Enter")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _verify_sent(self, text: str) -> bool:
        snippet = text.strip()[:40]
        for sel in S.MESSAGE_TEXT:
            try:
                loc = self.page.locator(sel, has_text=snippet)
                if await loc.count() > 0:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False


async def inspect_dom(page: Page) -> dict:
    """Return a safe, structural snapshot of key UI elements.

    Reports class names / counts / placeholders only. Deliberately does NOT
    return message text, contact names, or any personal content.
    """
    js = """
    () => {
      const out = {};
      const classesOf = (nodes) => Array.from(nodes).slice(0, 12).map(n => n.className || null);
      out.url = location.href;
      out.title = document.title;
      out.contenteditable = classesOf(document.querySelectorAll('[contenteditable="true"]'));
      out.inputs = Array.from(document.querySelectorAll('input')).slice(0, 20).map(i => ({
        type: i.type || null, cls: i.className || null, placeholder: i.placeholder || null
      }));
      out.sendLikeButtons = Array.from(document.querySelectorAll('button'))
        .filter(b => /send|paper|submit/i.test((b.className || '') + ' ' + (b.getAttribute('aria-label') || '')))
        .slice(0, 12)
        .map(b => ({ cls: b.className || null, aria: b.getAttribute('aria-label') || null }));
      const count = (sel) => document.querySelectorAll(sel).length;
      out.counts = {
        'a.chatlist-chat': count('a.chatlist-chat'),
        '.chatlist-chat': count('.chatlist-chat'),
        '.input-message-input': count('.input-message-input'),
        '.btn-send': count('.btn-send'),
        '.bubbles': count('.bubbles'),
        '.bubble.is-out': count('.bubble.is-out'),
      };
      return out;
    }
    """
    return await page.evaluate(js)
