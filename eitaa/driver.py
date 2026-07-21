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

    async def _snapshot_chats(self) -> list[dict]:
        """Read currently-rendered chat rows: clean title + peer type.

        Title is taken from `.peer-title` only (avoids the appended time). Type
        is inferred from the row's data-peer-id: negative -> group/channel.
        """
        js = """
        () => {
          const rows = document.querySelectorAll('.chatlist-chat');
          const out = [];
          for (const n of rows) {
            const t = n.querySelector('.peer-title');
            const title = (t ? t.textContent : '').trim();
            if (!title) continue;
            const pid = n.dataset.peerId || n.getAttribute('data-peer-id') || '';
            let kind = 'user';
            if (pid.startsWith('-')) kind = 'group_or_channel';
            out.push({ title: title.slice(0, 80), peer_id: pid, kind });
          }
          return out;
        }
        """
        try:
            return await self.page.evaluate(js)
        except Exception:  # noqa: BLE001
            return []

    async def list_chat_titles(self, limit: int = 20) -> list[str]:
        rows = await self._snapshot_chats()
        return [r["title"] for r in rows[:limit]]

    async def collect_all_chats(self, max_scrolls: int = 60) -> list[dict]:
        """Scroll the chat list and collect all rendered chats (deduped).

        tweb virtualizes the list, so we scroll the container repeatedly and
        accumulate rows until no new ones appear.
        """
        seen: dict[str, dict] = {}
        # Find a scrollable chat-list container.
        container = None
        for sel in [".chatlist-container", ".sidebar-content .scrollable", "#column-left .scrollable"]:
            loc = self.page.locator(sel).first
            try:
                if await loc.count() > 0:
                    container = loc
                    break
            except Exception:  # noqa: BLE001
                continue

        stagnant = 0
        for _ in range(max_scrolls):
            for r in await self._snapshot_chats():
                key = r.get("peer_id") or r.get("title")
                if key and key not in seen:
                    seen[key] = r
            before = len(seen)
            try:
                if container is not None:
                    await container.evaluate("el => el.scrollBy(0, el.clientHeight)")
                else:
                    await self.page.mouse.wheel(0, 800)
            except Exception:  # noqa: BLE001
                await self.page.mouse.wheel(0, 800)
            await self.page.wait_for_timeout(600)
            for r in await self._snapshot_chats():
                key = r.get("peer_id") or r.get("title")
                if key and key not in seen:
                    seen[key] = r
            if len(seen) == before:
                stagnant += 1
                if stagnant >= 4:
                    break
            else:
                stagnant = 0
        return list(seen.values())

    async def open_contacts_view(self, labels: list[str] | None = None) -> None:
        """Open the Contacts view via the sidebar menu.

        Opens the hamburger menu, then clicks the Contacts item by its icon
        class (tgico-user), which is language-independent and reliable.
        """
        labels = labels or S.CONTACTS_LABELS

        # Return to the chat-list root first (a subview may be open).
        try:
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

        # Open the menu with whichever toggle makes the contacts item appear.
        contacts_item = None
        for btn_sel in S.MENU_BUTTON:
            btn = self.page.locator(btn_sel).first
            try:
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.click()
                await self.page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001
                continue
            contacts_item = await _first_visible(self.page, S.CONTACTS_MENU_ITEM, timeout=1500)
            if contacts_item is not None:
                break

        # Fallback: match by visible text.
        if contacts_item is None:
            for label in labels:
                try:
                    it = self.page.get_by_text(label, exact=True).first
                    if await it.is_visible():
                        contacts_item = it
                        break
                except Exception:  # noqa: BLE001
                    continue

        if contacts_item is None:
            raise DriverError("contacts menu item not found (run: inspect --menu)")

        await contacts_item.click()
        await self.page.wait_for_timeout(1800)

    async def _snapshot_contacts(self, container_sel: str | None) -> list[dict]:
        """Snapshot contact rows, scoped to the contacts container if known."""
        js = """
        (containerSel) => {
          const root = containerSel ? document.querySelector(containerSel) : document;
          const scope = root || document;
          const rows = scope.querySelectorAll('.chatlist-chat');
          const out = [];
          for (const n of rows) {
            // Only currently-visible rows (avoids pulling the hidden chat list).
            if (n.offsetParent === null) continue;
            const t = n.querySelector('.peer-title');
            const title = (t ? t.textContent : '').trim();
            if (!title) continue;
            const pid = n.dataset.peerId || n.getAttribute('data-peer-id') || '';
            out.push({ title: title.slice(0, 80), peer_id: pid });
          }
          return out;
        }
        """
        try:
            return await self.page.evaluate(js, container_sel)
        except Exception:  # noqa: BLE001
            return []

    async def collect_all_contacts(self, max_scrolls: int = 120) -> list[dict]:
        """Open the Contacts view and scroll-collect ALL contacts (deduped)."""
        await self.open_contacts_view()

        # Find the contacts scroll container.
        container_sel = None
        for sel in S.CONTACTS_CONTAINER:
            try:
                if await self.page.locator(sel).first.count() > 0:
                    container_sel = sel
                    break
            except Exception:  # noqa: BLE001
                continue

        seen: dict[str, dict] = {}
        stagnant = 0
        for _ in range(max_scrolls):
            for r in await self._snapshot_contacts(container_sel):
                key = r.get("peer_id") or r.get("title")
                if key and key not in seen:
                    seen[key] = r
            before = len(seen)
            try:
                if container_sel is not None:
                    await self.page.locator(container_sel).first.evaluate(
                        "el => el.scrollBy(0, el.clientHeight)"
                    )
                else:
                    await self.page.mouse.wheel(0, 800)
            except Exception:  # noqa: BLE001
                await self.page.mouse.wheel(0, 800)
            await self.page.wait_for_timeout(500)
            for r in await self._snapshot_contacts(container_sel):
                key = r.get("peer_id") or r.get("title")
                if key and key not in seen:
                    seen[key] = r
            if len(seen) == before:
                stagnant += 1
                if stagnant >= 5:
                    break
            else:
                stagnant = 0
        return list(seen.values())

    async def get_stats(self) -> dict:
        """Return {contacts: N, pvs: M}.

        contacts = total entries in the Contacts view.
        pvs       = private chats (user peers, positive id) in the chat list.
        Groups/channels are intentionally not counted.
        """
        # PV count from the chat list (scroll all, keep only user peers).
        chats = await self.collect_all_chats()
        pvs = sum(1 for c in chats if c.get("kind") == "user")

        # Contacts count from the Contacts view (scroll all).
        try:
            contacts = await self.collect_all_contacts()
            contacts_count = len(contacts)
        except DriverError:
            contacts_count = -1  # could not open contacts view

        return {"contacts": contacts_count, "pvs": pvs}

    async def add_contacts_batch(self, entries: list[dict]) -> list[dict]:
        """Add many contacts. Opens the Contacts view ONCE, then uses the '+'
        button for each contact (avoids re-navigating the menu every time).

        entries: [{"phone": "+98...", "first": "...", "last": "..."}]
        Each result gets status: added | not_on_eitaa | invalid_number | error
        """
        results: list[dict] = []
        try:
            await self.open_contacts_view()
        except DriverError as exc:
            for e in entries:
                results.append({**e, "status": "error", "detail": str(exc)})
            return results

        for e in entries:
            r = await self._add_one(e["phone"], e.get("first", ""), e.get("last", ""))
            results.append(r)
            # Return to the contacts view for the next '+' click.
            try:
                await self.page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            await self.page.wait_for_timeout(1000)
        return results

    async def _add_one(self, phone: str, first: str, last: str = "") -> dict:
        result = {"phone": phone, "first": first, "last": last, "status": "error", "detail": ""}
        try:
            btn = await _first_visible(self.page, S.ADD_CONTACT_BUTTON, timeout=6000)
            if btn is None:
                result["detail"] = "add-contact (+) button not found"
                return result
            await btn.click()
            popup = await _first_visible(self.page, S.NEW_CONTACT_POPUP, timeout=6000)
            if popup is None:
                result["detail"] = "new-contact popup did not open"
                return result

            fields = self.page.locator(".popup.active .input-field-input")
            count = await fields.count()
            if count == 0:
                result["detail"] = "no fields in popup"
                return result

            labels = await self.page.evaluate(
                """
                () => Array.from(document.querySelectorAll('.popup.active .input-field-input'))
                  .map(n => {
                    const f = n.closest('.input-field');
                    return ((f && f.querySelector('label') ? f.querySelector('label').textContent : '') || '').trim();
                  })
                """
            )

            def _match(keywords, avoid=None):
                for i, lab in enumerate(labels or []):
                    if avoid is not None and i == avoid:
                        continue
                    low = lab.lower()
                    if any(k in low for k in keywords):
                        return i
                return None

            first_idx = _match(["نام (", "نام(", "first", "نام"])
            last_idx = _match(["خانوادگ", "last", "family"])
            phone_idx = _match(["تلفن", "شماره", "phone", "موبایل", "mobile"])
            if first_idx is None:
                first_idx = 0
            if phone_idx is None:
                phone_idx = count - 1

            await self._type_into(fields.nth(first_idx), first)
            if last and last_idx is not None and last_idx != first_idx:
                await self._type_into(fields.nth(last_idx), last)
            await self._type_into(fields.nth(phone_idx), phone)
            await self.page.wait_for_timeout(500)

            confirm = await _first_visible(self.page, S.NEW_CONTACT_CONFIRM, timeout=3000)
            if confirm is None:
                for label in S.ADD_CONTACT_LABELS:
                    try:
                        c = self.page.get_by_text(label, exact=False).first
                        if await c.is_visible():
                            confirm = c
                            break
                    except Exception:  # noqa: BLE001
                        continue
            if confirm is None:
                result["detail"] = "confirm button not found"
                return result
            await confirm.click()

            result["status"], result["detail"] = await self._detect_add_result()
            return result
        except Exception as exc:  # noqa: BLE001
            result["detail"] = f"exception: {exc}"
            return result

    async def _type_into(self, loc: Locator, value: str) -> None:
        """Reliable typing for contenteditable fields (clear then type)."""
        await loc.click()
        try:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Delete")
        except Exception:  # noqa: BLE001
            pass
        await loc.type(value, delay=25)

    async def _detect_add_result(self) -> tuple[str, str]:
        """Wait for the popup to close (=added) or read the inline error."""
        # Poll up to ~5s for the popup to disappear.
        for _ in range(10):
            await self.page.wait_for_timeout(500)
            popup = await _first_visible(self.page, S.NEW_CONTACT_POPUP, timeout=300)
            if popup is None:
                return "added", "popup closed"
        # Still open -> read any error / description text.
        try:
            txt = await self.page.evaluate(
                "() => (document.querySelector('.popup.active .error, .popup.active .input-field-input-error, "
                ".toast, .popup-description') || {}).textContent || ''"
            )
        except Exception:  # noqa: BLE001
            txt = ""
        low = (txt or "").lower()
        not_on = ["not on", "isn't on", "not registered", "یافت نشد", "عضو نیست", "ثبت نشده", "وجود ندارد", "پیدا نشد"]
        invalid = ["invalid", "نامعتبر", "معتبر نیست", "اشتباه", "صحیح نیست"]
        if any(m in low for m in not_on):
            return "not_on_eitaa", txt.strip()[:80]
        if any(m in low for m in invalid):
            return "invalid_number", txt.strip()[:80]
        return "error", (txt.strip()[:80] or "popup stayed open")

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


async def inspect_menu(page: Page) -> dict:
    """Open the sidebar menu and dump its item labels + button classes.

    Helps pin down the exact selector/label for the Contacts entry. Menu item
    TEXT is the owner's own UI chrome (not personal data), safe to print.
    """
    from eitaa import selectors as S

    out: dict = {"menu_buttons": [], "menu_items": []}
    js_buttons = """
    () => Array.from(document.querySelectorAll('.sidebar-header button, button.btn-menu-toggle'))
      .slice(0, 12).map(b => b.className || null)
    """
    try:
        out["menu_buttons"] = await page.evaluate(js_buttons)
    except Exception:  # noqa: BLE001
        pass

    # Try clicking the first menu button candidate, then read menu items.
    btn = await _first_visible(page, S.MENU_BUTTON, timeout=6000)
    if btn is not None:
        try:
            await btn.click()
            await page.wait_for_timeout(700)
        except Exception:  # noqa: BLE001
            pass
    js_items = """
    () => Array.from(document.querySelectorAll('.btn-menu-item, .btn-menu .menu-item, [class*="menu-item"]'))
      .slice(0, 30)
      .map(n => ({ cls: n.className || null, text: (n.textContent || '').trim().slice(0, 40) }))
    """
    try:
        out["menu_items"] = await page.evaluate(js_items)
    except Exception:  # noqa: BLE001
        pass
    return out


async def inspect_add_contact(driver: "EitaaDriver") -> dict:
    """Open Contacts -> add-contact popup and dump the popup's inputs/buttons.

    Reveals the exact selectors for the new-contact form. Only structural info
    (classes/placeholders/button text) is returned, not personal data.
    """
    from eitaa import selectors as S

    page = driver.page
    out: dict = {"add_button_found": False, "popup_found": False, "inputs": [], "buttons": []}

    await driver.open_contacts_view()
    btn = await _first_visible(page, S.ADD_CONTACT_BUTTON, timeout=6000)
    out["add_button_found"] = btn is not None
    # Also dump round/corner button candidates so we can find the right one.
    try:
        out["corner_buttons"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('.btn-circle, .btn-corner, .sidebar-header .btn-icon'))"
            ".slice(0,12).map(b => b.className || null)"
        )
    except Exception:  # noqa: BLE001
        out["corner_buttons"] = []

    if btn is not None:
        try:
            await btn.click()
            await page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001
            pass

    popup = await _first_visible(page, S.NEW_CONTACT_POPUP, timeout=4000)
    out["popup_found"] = popup is not None
    try:
        # tweb popups use .input-field-input (often contenteditable divs), each
        # inside an .input-field with a <label>. Dump tag/class/label/order.
        out["fields"] = await page.evaluate(
            """
            () => Array.from(document.querySelectorAll('.popup.active .input-field-input, .popup.active [contenteditable]'))
              .slice(0, 12)
              .map((n, idx) => {
                const field = n.closest('.input-field');
                const label = field ? (field.querySelector('label') || {}).textContent : '';
                return {
                  idx: idx,
                  tag: n.tagName,
                  cls: n.className || null,
                  editable: n.getAttribute('contenteditable'),
                  label: (label || '').trim().slice(0, 30),
                };
              })
            """
        )
        out["buttons"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('.popup.active button, .popup-container.active button'))"
            ".slice(0,10).map(b => ({cls:b.className||null, text:(b.textContent||'').trim().slice(0,30)}))"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    return out
