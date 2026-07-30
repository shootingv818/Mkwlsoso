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

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import Locator, Page, TimeoutError as PWTimeout

from config import config
from capture.browser import BrowserSession
from eitaa import selectors as S


# Source of the in-page bridge send function (window.__MKWL_send). Loaded once
# at import; injected on demand by EitaaDriver.ensure_bridge().
try:
    _BRIDGE_SEND_SRC = (Path(__file__).with_name("bridge_send.js")).read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _BRIDGE_SEND_SRC = ""

# Source of the in-page file bridge (window.__MKWL_fileInit / __MKWL_fileSend).
try:
    _BRIDGE_FILE_SRC = (Path(__file__).with_name("bridge_file_send.js")).read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _BRIDGE_FILE_SRC = ""

# Source of the in-page contact-import bridge (window.__MKWL_importContacts).
try:
    _BRIDGE_CONTACTS_SRC = (Path(__file__).with_name("contacts_bridge.js")).read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _BRIDGE_CONTACTS_SRC = ""

# Source of the in-page stats bridge (window.__MKWL_stats).
try:
    _BRIDGE_STATS_SRC = (Path(__file__).with_name("stats_bridge.js")).read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _BRIDGE_STATS_SRC = ""

# Source of the in-page contacts LIST bridge (window.__MKWL_contactsList).
try:
    _BRIDGE_CONTACTS_LIST_SRC = (
        Path(__file__).with_name("contacts_list.js")).read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _BRIDGE_CONTACTS_LIST_SRC = ""

# Source of the in-page session exporter (window.__MKWL_exportSession).
try:
    _SESSION_EXPORT_SRC = (Path(__file__).with_name("session_export.js")).read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _SESSION_EXPORT_SRC = ""

# Source of the in-page transport probe (window.__MKWL_txDump).
try:
    _TRANSPORT_PROBE_SRC = (Path(__file__).with_name("transport_probe.js")).read_text(encoding="utf-8")
except Exception:  # noqa: BLE001
    _TRANSPORT_PROBE_SRC = ""


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
    step = 200
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

    async def open(self, settle_timeout: float = 25.0) -> None:
        """Navigate to Eitaa and wait until the app is ACTUALLY usable.

        This used to be `goto()` plus a flat `wait_for_timeout(4000)`. A fixed 4s
        is wrong in both directions: an app that booted in 0.8s still cost 4s
        (paid on every navigation of every job), and one that needed 8s was used
        too early. Now it polls a real readiness signal - Eitaa's own apiManager,
        which is what every bridge call needs - and returns the moment it appears.
        """
        await self.session.goto()
        deadline = time.monotonic() + settle_timeout
        # Poll fast at first (a warm profile is ready almost immediately), then
        # back off so a slow boot does not burn CPU we do not have.
        wait = 0.15
        while time.monotonic() < deadline:
            try:
                if await self.page.evaluate(
                        "() => !!(window.apiManager && window.apiManager.invokeApi)"):
                    return
            except Exception:  # noqa: BLE001 - navigation in flight
                pass
            await self.page.wait_for_timeout(int(wait * 1000))
            wait = min(1.0, wait * 1.6)
        # Not ready in time: callers already handle "not logged in" / bridge
        # unavailable, so let them report it instead of hanging here.

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

    async def collect_all_chats(self, max_scrolls: int = 60,
                                should_stop: Callable[[], bool] | None = None) -> list[dict]:
        """Scroll the chat list and collect all rendered chats (deduped).

        tweb virtualizes the list, so we scroll the container repeatedly and
        accumulate rows until no new ones appear. `should_stop` is polled between
        scrolls so the walk can be interrupted.
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
            if should_stop is not None and should_stop():
                print(f"[chats] collection interrupted at {len(seen)} chats", flush=True)
                break
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

    async def collect_all_contacts(self, max_scrolls: int = 120,
                                   should_stop: Callable[[], bool] | None = None) -> list[dict]:
        """Open the Contacts view and scroll-collect ALL contacts (deduped).

        `should_stop` is polled between scrolls so a long collection can be
        interrupted; whatever was gathered so far is returned.
        """
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
            if should_stop is not None and should_stop():
                print(f"[contacts] collection interrupted at {len(seen)} contacts",
                      flush=True)
                break
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

        for idx, e in enumerate(entries):
            # Before every contact (except the first, where the Contacts view
            # was just opened) make sure any leftover popup is closed and the
            # '+' add button is actually available again. A stuck popup from a
            # previous contact otherwise blocks the next '+' click.
            if idx > 0:
                ready = await self._reset_to_contacts_view()
                if not ready:
                    results.append({
                        **e,
                        "status": "error",
                        "detail": "could not return to contacts view before next add",
                    })
                    continue
            r = await self._add_one(e["phone"], e.get("first", ""), e.get("last", ""))
            results.append(r)
        # Leave a clean state for whatever runs next.
        await self._reset_to_contacts_view()
        return results

    async def _new_contact_popup_open(self) -> bool:
        return await _first_visible(self.page, S.NEW_CONTACT_POPUP, timeout=200) is not None

    async def _popup_has_message(self) -> bool:
        """True if the popup/page shows any inline error or toast text."""
        try:
            return await self.page.evaluate(
                """
                () => {
                  const p = document.querySelector('.popup.active');
                  const m = p && p.querySelector(
                    '.error, .input-field-input-error, .popup-description, [role="alert"]'
                  );
                  const t = document.querySelector('.toast, .toast-body, [class*=toast]');
                  return !!((m && (m.textContent || '').trim())
                    || (t && (t.textContent || '').trim()));
                }
                """
            )
        except Exception:  # noqa: BLE001
            return False

    async def _reset_to_contacts_view(self) -> bool:
        """Close any open popup and guarantee we are back on the Contacts view
        with the correct add-contact '+' button available.

        After a successful add, Eitaa navigates into the new contact's private
        chat and the left column can revert to the main chat list, whose corner
        button is the "new message" composer -- NOT the add-contact button. To
        never click the wrong button, we ALWAYS re-open the Contacts view
        explicitly and then require the add button scoped to the active
        contacts tab.

        Returns True when the scoped add-contact button is available.
        """
        # 1) Close any lingering popup first.
        for _ in range(5):
            if not await self._new_contact_popup_open():
                break
            try:
                closed = await self.page.evaluate(
                    """
                    () => {
                      const p = document.querySelector('.popup.active');
                      if (!p) return true;
                      const b = p.querySelector(
                        '.btn-icon.tgico-close, .popup-close, .popup-header .btn-icon'
                      );
                      if (b) { b.click(); return true; }
                      return false;
                    }
                    """
                )
            except Exception:  # noqa: BLE001
                closed = False
            if not closed:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            await self.page.wait_for_timeout(500)

        # 2) Always re-open the Contacts view. This guarantees the active tab
        #    is the contacts list (never the chat list), so its corner button
        #    is the add-contact button rather than the message composer.
        try:
            await self.open_contacts_view()
        except DriverError:
            return False

        # 3) Require the add button scoped strictly to the active contacts tab.
        btn = await _first_visible(self.page, S.CONTACTS_ADD_BUTTON, timeout=2000)
        return btn is not None

    async def _add_one(
        self,
        phone: str,
        first: str,
        last: str = "",
        before_submit: Callable[[], Awaitable[None]] | None = None,
        after_submit: Callable[[], Awaitable[None]] | None = None,
    ) -> dict:
        result = {"phone": phone, "first": first, "last": last, "status": "error", "detail": ""}
        try:
            # Scoped to the active contacts tab so we never click the chat-list
            # "new message" composer by mistake.
            btn = await _first_visible(self.page, S.CONTACTS_ADD_BUTTON, timeout=6000)
            if btn is None:
                result["detail"] = "add-contact (+) button not found in contacts tab"
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

            # The live form has exactly one observed Add action. Do not fall
            # back to generic popup buttons: closing a dialog could otherwise
            # be misreported as a successful contact creation.
            confirm = await _first_visible(
                self.page,
                [".popup.active .btn-primary.btn-color-primary"],
                timeout=3000,
            )
            if confirm is None:
                result["detail"] = "confirm button not found"
                return result
            confirm_text = " ".join((await confirm.inner_text()).split())
            allowed_labels = {" ".join(label.split()) for label in S.ADD_CONTACT_LABELS}
            if confirm_text not in allowed_labels:
                result["detail"] = (
                    "primary popup button label was not recognized; submit aborted"
                )
                return result

            # Wait until the confirm button is actually enabled (Eitaa validates
            # the phone asynchronously and enables the button a beat later).
            disabled = False
            for _ in range(15):
                try:
                    disabled = await confirm.evaluate(
                        "b => b.disabled === true || (b.className || '').includes('disable') "
                        "|| b.getAttribute('disabled') !== null"
                    )
                except Exception:  # noqa: BLE001
                    disabled = False
                if not disabled:
                    break
                await self.page.wait_for_timeout(400)
            if disabled:
                result["detail"] = "confirm button stayed disabled after validation wait"
                return result

            await confirm.scroll_into_view_if_needed()
            if before_submit is not None:
                await before_submit()

            # Click the Add button, then confirm it actually took effect. The
            # SPA re-renders the button as the phone validates, so a handle can
            # go stale and its click silently no-ops. We re-query a fresh button
            # each attempt and retry once if the popup stays open with no error.
            for attempt in range(2):
                fresh = await _first_visible(
                    self.page,
                    [".popup.active .btn-primary.btn-color-primary"],
                    timeout=1500,
                )
                click_target = fresh or confirm
                try:
                    await click_target.click(timeout=5000)
                except Exception:  # noqa: BLE001
                    pass
                if attempt == 0 and after_submit is not None:
                    await after_submit()

                closed = False
                for _ in range(5):
                    await self.page.wait_for_timeout(500)
                    if not await self._new_contact_popup_open():
                        closed = True
                        break
                if closed:
                    result["status"], result["detail"] = "added", "popup closed after click"
                    return result
                # Still open with a real error/toast -> stop; let the detailed
                # detector classify (not-on-eitaa, invalid, etc.).
                if await self._popup_has_message():
                    break

            # Deliberately do not press Enter if the popup remains open. Enter
            # can target the wrong SPA action and previously left Eitaa blank.
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
        """Wait for the popup to close (=added) or gather rich diagnostics."""
        # Poll up to ~5s for the popup to disappear.
        for _ in range(10):
            await self.page.wait_for_timeout(500)
            popup = await _first_visible(self.page, S.NEW_CONTACT_POPUP, timeout=300)
            if popup is None:
                return "added", "popup closed"

        # Still open: collect exact UI error/status text, but never persist the
        # names or phone typed into contenteditable fields.
        try:
            diag = await self.page.evaluate(
                """
                () => {
                  const p = document.querySelector('.popup.active');
                  if (!p) return null;
                  const fields = Array.from(p.querySelectorAll('.input-field-input'))
                    .map(n => ({
                      tag: n.tagName,
                      chars: ((n.textContent || n.value || '')).length,
                      editable: n.getAttribute('contenteditable') === 'true',
                    }));
                  const btn = p.querySelector('.btn-primary.btn-color-primary, .btn-primary, .popup-button');
                  const disabled = btn ? (btn.disabled === true
                    || (btn.className || '').includes('disable')
                    || btn.getAttribute('disabled') !== null) : null;
                  const messages = Array.from(p.querySelectorAll(
                    '.error, .input-field-input-error, .popup-description, [role="alert"]'
                  )).map(n => (n.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter(Boolean).slice(0, 10);
                  return {
                    messages,
                    confirm_disabled: disabled,
                    confirm_class: btn ? (btn.className || '') : '',
                    field_shapes: fields,
                  };
                }
                """
            )
        except Exception as exc:  # noqa: BLE001
            return "error", f"diagnostic-failed: {exc}"

        if not diag:
            return "error", "popup vanished during diagnostics"

        # Scan document-wide toasts; these often carry the actual server result.
        try:
            toast = await self.page.evaluate(
                "() => Array.from(document.querySelectorAll('.toast, .toast-body, [class*=toast], [role=alert]'))"
                ".map(n => (n.textContent||'').replace(/\\s+/g,' ').trim())"
                ".filter(Boolean).join(' | ').slice(0, 500)"
            )
        except Exception:  # noqa: BLE001
            toast = ""

        from capture.redactor import scrub_text

        messages = [scrub_text(str(m)) for m in (diag.get("messages") or [])]
        toast = scrub_text(toast or "")
        low = (" ".join(messages) + " " + toast).lower()
        not_on = ["not on", "isn't on", "not registered", "یافت نشد", "عضو نیست", "ثبت نشده", "وجود ندارد", "پیدا نشد"]
        invalid = ["invalid", "نامعتبر", "معتبر نیست", "اشتباه", "صحیح نیست", "correct"]
        detail = (
            f"popup_stayed_open disabled={diag.get('confirm_disabled')} "
            f"confirm_class='{diag.get('confirm_class')}' "
            f"fields={diag.get('field_shapes')} "
            f"messages={messages} toast='{toast}'"
        )
        if any(m in low for m in not_on):
            return "not_on_eitaa", detail
        if any(m in low for m in invalid):
            return "invalid_number", detail

        # No visible error and no toast, the Add button was enabled, and the
        # phone field was populated. The diagnostic run proved that in this
        # state the click reaches the button and a request goes to the server,
        # yet the contact is not created. In practice this is Eitaa silently
        # declining an unregistered number (same as Telegram: you cannot add a
        # number that is not registered). Classify it honestly instead of a
        # generic error.
        field_shapes = diag.get("field_shapes") or []
        phone_filled = any((f.get("chars") or 0) >= 8 for f in field_shapes)
        if not diag.get("confirm_disabled") and phone_filled:
            return (
                "not_on_eitaa",
                "silently rejected: no error/toast, button enabled, number entered -> "
                "number is not registered on Eitaa | " + detail,
            )
        return "error", detail

    async def open_saved_messages(self) -> None:
        """Open the 'Saved Messages' chat via the sidebar menu.

        Saved Messages is the owner's own storage, so it is a completely safe
        target for testing file upload without bothering any real contact.
        """
        try:
            await self.page.keyboard.press("Escape")
            await self.page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

        for btn_sel in S.MENU_BUTTON:
            btn = self.page.locator(btn_sel).first
            try:
                if await btn.count() == 0 or not await btn.is_visible():
                    continue
                await btn.click()
                await self.page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001
                continue
            item = await _first_visible(self.page, [".btn-menu-item.tgico-saved"], timeout=1500)
            if item is not None:
                await item.click()
                await self.page.wait_for_timeout(1600)
                return
        raise DriverError("could not open Saved Messages (menu item tgico-saved not found)")

    async def _return_to_chat_list(self) -> None:
        """Close any open left-column subview (e.g. the Contacts view) so the
        main chat list + its search input are on screen again."""
        for _ in range(4):
            if await _first_visible(self.page, S.SEARCH_INPUT, timeout=800) is not None:
                return
            # Prefer the subview's back/close button, else press Escape.
            closed = False
            for sel in [
                "#column-left .sidebar-close-button",
                "#column-left .btn-icon.tgico-left",
                ".sidebar-slider .sidebar-close-button",
            ]:
                loc = self.page.locator(sel).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click()
                        closed = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not closed:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            await self.page.wait_for_timeout(500)

    async def open_chat(self, query: str) -> None:
        _t0 = time.monotonic()
        search = await _first_visible(self.page, S.SEARCH_INPUT, timeout=6000)
        healed = False
        if search is None:
            # We may be inside a subview (e.g. the Contacts view after a
            # collect). Return to the main chat list and retry.
            healed = True
            await self._return_to_chat_list()
            search = await _first_visible(self.page, S.SEARCH_INPUT, timeout=6000)
        if search is None:
            raise DriverError("search input not found (are you logged in?)")
        t_find = time.monotonic() - _t0
        if healed:
            print(f"[timing] open_chat: search-input needed self-heal ({t_find:.1f}s)", flush=True)
        await search.click()
        await search.fill("")
        await search.type(query, delay=8)
        # Results load asynchronously; poll for them instead of a fixed wait.
        await self.page.wait_for_timeout(500)

        _t1 = time.monotonic()
        result = await _first_visible(self.page, S.CHAT_RESULT, timeout=6000)
        used_fallback = False
        if result is None:
            # Fallback: keyboard-select the top result.
            used_fallback = True
            try:
                await search.press("ArrowDown")
                await search.press("Enter")
                await self.page.wait_for_timeout(700)
                # If a composer appeared, treat as opened.
                box = await _first_visible(self.page, S.MESSAGE_INPUT, timeout=2500)
                if box is not None:
                    print(f"[timing] open_chat({query!r}): find={t_find:.1f}s "
                          f"results={time.monotonic()-_t1:.1f}s fallback=Y total={time.monotonic()-_t0:.1f}s",
                          flush=True)
                    return
            except Exception:  # noqa: BLE001
                pass
            raise DriverError(f"no chat result for query: {query!r}")
        await result.click()
        await self.page.wait_for_timeout(700)  # let the chat open
        print(f"[timing] open_chat({query!r}): find={t_find:.1f}s "
              f"results={time.monotonic()-_t1:.1f}s fallback={'Y' if used_fallback else 'N'} "
              f"total={time.monotonic()-_t0:.1f}s", flush=True)

    async def ensure_bridge(self) -> bool:
        """Make sure window.__MKWL_send is defined in the page.

        Injected on demand (idempotent) so callers don't have to thread an
        init script through session creation. Returns True if the bridge send
        function is available.
        """
        try:
            has = await self.page.evaluate("() => typeof window.__MKWL_send === 'function'")
        except Exception:  # noqa: BLE001
            has = False
        if has:
            return True
        if not _BRIDGE_SEND_SRC:
            return False
        try:
            await self.page.evaluate(_BRIDGE_SEND_SRC)  # IIFE defines window.__MKWL_send
            return await self.page.evaluate("() => typeof window.__MKWL_send === 'function'")
        except Exception:  # noqa: BLE001
            return False

    async def bridge_send(self, peer_id: str, text: str) -> dict:
        """Send `text` to `peer_id` through Eitaa's own engine (no UI).

        Returns the raw result dict from window.__MKWL_send:
        {ok, method, msg_id, limit, code, detail}. Never raises.
        """
        if not peer_id:
            return {"ok": False, "code": "no peer_id"}
        if not await self.ensure_bridge():
            return {"ok": False, "code": "bridge unavailable"}
        try:
            res = await self.page.evaluate(
                "(a) => window.__MKWL_send(a.p, a.t)",
                {"p": str(peer_id), "t": text},
            )
            return res if isinstance(res, dict) else {"ok": False, "code": "bad bridge result"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"bridge evaluate error: {exc}"}

    async def ensure_file_bridge(self) -> bool:
        """Make sure window.__MKWL_fileInit / __MKWL_fileSend are defined."""
        try:
            has = await self.page.evaluate("() => typeof window.__MKWL_fileInit === 'function'")
        except Exception:  # noqa: BLE001
            has = False
        if has:
            return True
        if not _BRIDGE_FILE_SRC:
            return False
        try:
            await self.page.evaluate(_BRIDGE_FILE_SRC)
            return await self.page.evaluate("() => typeof window.__MKWL_fileInit === 'function'")
        except Exception:  # noqa: BLE001
            return False

    async def self_peer_id(self) -> str | None:
        """This account's own user id (its Saved Messages peer).

        Used by the test send: Saved Messages is a recipient every engine can
        address with no contact relationship, so it exercises the full pipeline
        without touching anybody else.
        """
        js = """
        () => {
          const c = [];
          try { c.push(window.appUsersManager && window.appUsersManager.getSelf
                       && window.appUsersManager.getSelf().id); } catch (e) {}
          try { c.push(window.appPeersManager && window.appPeersManager.peerId); } catch (e) {}
          try { c.push(window.appImManager && window.appImManager.myId); } catch (e) {}
          try { c.push(window.appUsersManager && window.appUsersManager.userId); } catch (e) {}
          for (const v of c) {
            if (v != null && String(v).length && !isNaN(+v) && +v > 0) return String(v);
          }
          return null;
        }
        """
        try:
            res = await self.page.evaluate(js)
        except Exception:  # noqa: BLE001
            return None
        return str(res) if res else None

    async def bridge_file_ready(self) -> bool:
        """Is a previously uploaded document still reusable in this page?

        The upload state lives in the page, so a reload or navigation wipes it.
        Checking is cheap; discovering it the hard way costs a full re-upload
        per recipient (~25s each, measured live).
        """
        try:
            return bool(await self.page.evaluate(
                "() => typeof window.__MKWL_fileReady === 'function'"
                " && window.__MKWL_fileReady()"))
        except Exception:  # noqa: BLE001
            return False

    async def bridge_file_init(self, file_path: str, caption: str = "",
                               locate_timeout: float | None = None) -> dict:
        """Upload `file_path` to Saved Messages ONCE via the bridge.

        The heavy upload happens a single time here; every recipient afterwards
        reuses the resulting document with no re-upload. Returns the raw result
        {ok, msg_id, doc_id, code}. `caption` is unused for the upload itself
        (recipients get their caption per-send) but kept for signature clarity.

        `locate_timeout` caps the "find my upload" phase in seconds. When not
        given it scales with the file size (30s base + 12s per MB, clamped to
        5 minutes), because the old fixed 90-iteration loop turned into a
        600-second stall on a slow host.
        """
        import base64 as _b64
        import mimetypes as _mt
        import os as _os

        if not file_path or not _os.path.isfile(file_path):
            return {"ok": False, "code": f"file not found: {file_path}"}
        if not await self.ensure_file_bridge():
            return {"ok": False, "code": "file bridge unavailable"}
        try:
            with open(file_path, "rb") as f:
                data = f.read()
            b64 = _b64.b64encode(data).decode("ascii")
            filename = _os.path.basename(file_path)
            mime = _mt.guess_type(file_path)[0] or "application/octet-stream"
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"read error: {exc}"}
        if locate_timeout is None:
            # Budget scaled to the file size. Measured on the live host: a 9.5 MB
            # file uploads in ~22s on a good minute, but a bad minute blew past
            # 139s (28 checks) and failed. 25s/MB gives that same file ~4 minutes
            # instead of falsely reporting it as lost.
            size_mb = len(data) / (1024 * 1024)
            locate_timeout = min(420.0, 45.0 + 25.0 * size_mb)
        try:
            res = await self.page.evaluate(
                "(a) => window.__MKWL_fileInit(a.b, a.n, a.m, a.d)",
                {"b": b64, "n": filename, "m": mime,
                 "d": int(max(10.0, float(locate_timeout)) * 1000)},
            )
            return res if isinstance(res, dict) else {"ok": False, "code": "bad init result"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"file init evaluate error: {exc}"}

    async def bridge_file_send(self, peer_id: str, caption: str = "") -> dict:
        """Deliver the already-uploaded file to one recipient (no re-upload).

        Requires bridge_file_init() to have succeeded first. Returns
        {ok, method, msg_id, limit, code}. Never raises.
        """
        if not peer_id:
            return {"ok": False, "code": "no peer_id"}
        if not await self.ensure_file_bridge():
            return {"ok": False, "code": "file bridge unavailable"}
        try:
            res = await self.page.evaluate(
                "(a) => window.__MKWL_fileSend(a.p, a.c)",
                {"p": str(peer_id), "c": caption or ""},
            )
            return res if isinstance(res, dict) else {"ok": False, "code": "bad send result"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"file send evaluate error: {exc}"}

    async def export_session(self) -> dict | None:
        """Dump the browser profile's MTProto session (IndexedDB + localStorage).

        Returns the raw export {localStorage, indexeddb, hints} or None. This is
        the bridge that lets the headless direct client reuse this exact
        authorized session with no re-login.
        """
        try:
            has = await self.page.evaluate("() => typeof window.__MKWL_exportSession === 'function'")
        except Exception:  # noqa: BLE001
            has = False
        if not has:
            if not _SESSION_EXPORT_SRC:
                return None
            try:
                await self.page.evaluate(_SESSION_EXPORT_SRC)
            except Exception:  # noqa: BLE001
                return None
        try:
            res = await self.page.evaluate("() => window.__MKWL_exportSession()")
        except Exception:  # noqa: BLE001
            return None
        return res if isinstance(res, dict) else None

    async def inject_transport_probe(self) -> bool:
        """Install the fetch/XHR transport hooks (window.__MKWL_txDump)."""
        try:
            has = await self.page.evaluate("() => typeof window.__MKWL_txDump === 'function'")
        except Exception:  # noqa: BLE001
            has = False
        if has:
            return True
        if not _TRANSPORT_PROBE_SRC:
            return False
        try:
            await self.page.evaluate(_TRANSPORT_PROBE_SRC)
            return await self.page.evaluate("() => typeof window.__MKWL_txDump === 'function'")
        except Exception:  # noqa: BLE001
            return False

    async def dump_transport(self) -> list:
        """Drain the recorded MTProto HTTP requests (url + hex heads)."""
        try:
            res = await self.page.evaluate("() => window.__MKWL_txDump ? window.__MKWL_txDump() : []")
        except Exception:  # noqa: BLE001
            return []
        return res if isinstance(res, list) else []

    async def dump_worker_requests(self) -> list:
        """Drain the requests recorded INSIDE the workers (worker_capture.js).

        Requires the session to have been opened with worker_capture.js as its
        init script (it must wrap window.Worker before any worker is created).
        """
        try:
            res = await self.page.evaluate(
                "() => window.__MKWL_workerDump ? window.__MKWL_workerDump() : null")
        except Exception:  # noqa: BLE001
            return []
        if res is None:
            return [{"kind": "no_hook", "note": "worker_capture.js was not injected as init script"}]
        return res if isinstance(res, list) else []

    async def ensure_stats_bridge(self) -> bool:
        """Make sure window.__MKWL_stats is defined."""
        try:
            has = await self.page.evaluate("() => typeof window.__MKWL_stats === 'function'")
        except Exception:  # noqa: BLE001
            has = False
        if has:
            return True
        if not _BRIDGE_STATS_SRC:
            return False
        try:
            await self.page.evaluate(_BRIDGE_STATS_SRC)
            return await self.page.evaluate("() => typeof window.__MKWL_stats === 'function'")
        except Exception:  # noqa: BLE001
            return False

    async def bridge_stats(self, with_pvs: bool = True) -> dict | None:
        """Instant stats via the API: {contacts, pvs}. None if unavailable.

        `with_pvs=False` skips the private-chat count. That count pages through
        messages.getDialogs and was measured live at 98 SECONDS while the
        contacts number itself takes 4 -- far too expensive for a hot path like
        adding an account, where only the contacts number is shown.
        """
        if not await self.ensure_stats_bridge():
            return None
        try:
            res = await self.page.evaluate(
                "(withPvs) => window.__MKWL_stats(withPvs)", bool(with_pvs))
        except Exception:  # noqa: BLE001
            return None
        if isinstance(res, dict) and res.get("ok"):
            return {"contacts": res.get("contacts", -1), "pvs": res.get("pvs", -1)}
        return None

    _CONTACTS_LIST_PROBE = "() => typeof window.__MKWL_contactsList === 'function'"

    async def ensure_contacts_list_bridge(self) -> bool:
        """Make sure window.__MKWL_contactsList is defined."""
        try:
            has = await self.page.evaluate(self._CONTACTS_LIST_PROBE)
        except Exception:  # noqa: BLE001
            has = False
        if has:
            return True
        if not _BRIDGE_CONTACTS_LIST_SRC:
            return False
        try:
            await self.page.evaluate(_BRIDGE_CONTACTS_LIST_SRC)
            return await self.page.evaluate(self._CONTACTS_LIST_PROBE)
        except Exception:  # noqa: BLE001
            return False

    async def bridge_contacts_list(self) -> dict | None:
        """The account's FULL contacts list via the API, in seconds.

        Returns {ok, count, contacts:[{peer_id, access_hash, title, ...}],
        skipped, raw} or None when the bridge is unavailable (caller then falls
        back to collect_all_contacts). Never raises.

        This is the fast, complete replacement for scroll-collecting the
        virtualized Contacts view: >10 minutes and capped, versus ~4 seconds and
        complete, both measured live on the same account.
        """
        if not await self.ensure_contacts_list_bridge():
            return None
        try:
            res = await self.page.evaluate("() => window.__MKWL_contactsList()")
        except Exception:  # noqa: BLE001
            return None
        if isinstance(res, dict) and res.get("ok"):
            contacts = res.get("contacts") or []
            return {"ok": True, "count": len(contacts), "contacts": contacts,
                    "skipped": res.get("skipped", 0), "raw": res.get("raw", 0)}
        return {"ok": False, "code": str(res.get("code")) if isinstance(res, dict) else "bad_reply",
                "contacts": []}

    _CONTACTS_BRIDGE_PROBE = (
        "() => typeof window.__MKWL_importContacts === 'function'"
        " && typeof window.__MKWL_harvestPeers === 'function'"
    )

    async def ensure_contacts_bridge(self) -> bool:
        """Make sure window.__MKWL_importContacts / __MKWL_harvestPeers exist."""
        try:
            has = await self.page.evaluate(self._CONTACTS_BRIDGE_PROBE)
        except Exception:  # noqa: BLE001
            has = False
        if has:
            return True
        if not _BRIDGE_CONTACTS_SRC:
            return False
        try:
            await self.page.evaluate(_BRIDGE_CONTACTS_SRC)
            return await self.page.evaluate(self._CONTACTS_BRIDGE_PROBE)
        except Exception:  # noqa: BLE001
            return False

    async def bridge_import_contacts(self, entries: list[dict],
                                     plus_prefix: bool = False) -> dict:
        """Import a batch of phone numbers via contacts.importContacts.

        entries: [{"phone": "+98...", "first": "...", "last": "..."}]. Returns
        {ok, batch, imported_count, users_count, retry_count, phone_format,
        added:[{user_id, access_hash, phone, first}]} or, on rate-limit,
        {ok:False, limit:True, code, wait}. Never raises.

        `plus_prefix` picks the phone format sent to the server ("+98..." vs
        "98..."); the caller probes both so an empty import is diagnosable
        instead of silently returning zero contacts.
        """
        if not entries:
            return {"ok": True, "batch": 0, "imported_count": 0, "added": []}
        if not await self.ensure_contacts_bridge():
            return {"ok": False, "code": "contacts bridge unavailable"}
        try:
            res = await self.page.evaluate(
                "(a) => window.__MKWL_importContacts(a.entries, {plusPrefix: a.plus})",
                {"entries": entries, "plus": bool(plus_prefix)},
            )
            return res if isinstance(res, dict) else {"ok": False, "code": "bad import result"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"import evaluate error: {exc}"}

    async def bridge_harvest_peers(self, peer_ids: list) -> dict:
        """Resolve known peer_ids into {user_id, access_hash} via Eitaa's own
        peer manager, so the browser-free sender can target them.

        Returns {ok, peers:[{peer_id, user_id, access_hash}], missing:[...]}.
        Never raises.
        """
        if not peer_ids:
            return {"ok": True, "peers": [], "missing": []}
        if not await self.ensure_contacts_bridge():
            return {"ok": False, "code": "contacts bridge unavailable"}
        try:
            res = await self.page.evaluate(
                "(ids) => window.__MKWL_harvestPeers(ids)",
                [str(p) for p in peer_ids],
            )
            return res if isinstance(res, dict) else {"ok": False, "code": "bad harvest result"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "code": f"harvest evaluate error: {exc}"}

    async def send_text(self, query: str, text: str, verify: bool = True) -> SendResult:
        try:
            await self.open_chat(query)
        except DriverError as exc:
            return SendResult(ok=False, to=query, detail=str(exc))

        _ts = time.monotonic()
        box = await _first_visible(self.page, S.MESSAGE_INPUT, timeout=8000)
        t_box = time.monotonic() - _ts
        if box is None:
            return SendResult(ok=False, to=query, detail="message input not found")

        await box.click()
        await box.type(text, delay=5)
        await self.page.wait_for_timeout(150)

        sent = await self._click_send_or_enter(box)
        if not sent:
            return SendResult(ok=False, to=query, detail="could not trigger send")

        await self.page.wait_for_timeout(500)
        print(f"[timing] send_text: box={t_box:.1f}s send_total={time.monotonic()-_ts:.1f}s", flush=True)

        if verify:
            ok = await self._verify_sent(text)
            return SendResult(
                ok=ok,
                to=query,
                detail="verified outgoing bubble" if ok else "sent but not verified in DOM",
            )
        return SendResult(ok=True, to=query, detail="sent (no verification)")

    async def send_file(
        self,
        file_path: str,
        caption: str = "",
        query: str | None = None,
        to_saved: bool = False,
        as_photo: bool = False,
    ) -> SendResult:
        """Upload and send a file (optionally with a caption).

        Confirmed flow on this Eitaa Web build: the paperclip opens a dropdown
        (`.btn-menu.active`) with two items -- `tgico-image` ("عکس یا ویدیو")
        and `tgico-document` ("فایل"). Clicking a menu item opens the native
        file chooser. The click MUST be a real Playwright click (a trusted user
        gesture); a JS `.click()` is blocked by the browser from opening the
        file dialog. We default to the document ("فایل") item so any file type
        is accepted; as_photo uses the image item instead.
        """
        target = "Saved Messages" if to_saved else (query or "")
        try:
            if to_saved:
                await self.open_saved_messages()
            else:
                await self.open_chat(query or "")
        except DriverError as exc:
            return SendResult(ok=False, to=target, detail=str(exc))

        # 1) Open the attach dropdown via the paperclip.
        attach = await _first_visible(self.page, S.ATTACH_BUTTON, timeout=8000)
        if attach is None:
            return SendResult(ok=False, to=target, detail="attach (paperclip) button not found")
        try:
            await attach.click()
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, to=target, detail=f"could not open attach menu: {exc}")
        await self.page.wait_for_timeout(600)

        # 2) Pick the upload type. Document ("فایل") accepts any file; image
        #    ("عکس یا ویدیو") is for photos/videos only.
        item_selectors = S.ATTACH_MENU_IMAGE if as_photo else S.ATTACH_MENU_DOCUMENT
        menu_item = await _first_visible(self.page, item_selectors, timeout=3000)
        if menu_item is None:
            return SendResult(
                ok=False, to=target,
                detail="attach menu item not found ("
                       + ("image/عکس" if as_photo else "document/فایل") + ")",
            )

        # 3) A real click on the item raises the native file chooser.
        try:
            async with self.page.expect_file_chooser(timeout=6000) as fc_info:
                await menu_item.click()
            chooser = await fc_info.value
        except PWTimeout:
            return SendResult(ok=False, to=target, detail="file chooser did not open after clicking the menu item")

        try:
            await chooser.set_files(file_path)
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, to=target, detail=f"could not set file: {exc}")

        # 4) Wait for the media-preview popup.
        popup = await _first_visible(self.page, S.MEDIA_PREVIEW_POPUP, timeout=8000)
        if popup is None:
            return SendResult(ok=False, to=target, detail="media-preview popup did not open after attaching")

        # 5) Optional caption goes into the popup's message input.
        if caption:
            capbox = await _first_visible(self.page, S.MEDIA_CAPTION_INPUT, timeout=4000)
            if capbox is not None:
                try:
                    await capbox.click()
                    await capbox.type(caption, delay=15)
                    await self.page.wait_for_timeout(300)
                except Exception:  # noqa: BLE001
                    pass

        # 6) Send from within the preview popup.
        send_btn = await _first_visible(self.page, S.MEDIA_SEND_BUTTON, timeout=4000)
        if send_btn is None:
            return SendResult(ok=False, to=target, detail="send button in preview popup not found")
        try:
            await send_btn.click()
        except Exception as exc:  # noqa: BLE001
            return SendResult(ok=False, to=target, detail=f"could not click send: {exc}")

        await self.page.wait_for_timeout(2000)

        # 7) Verify: the preview popup should be gone.
        still_open = await _first_visible(self.page, S.MEDIA_PREVIEW_POPUP, timeout=800)
        if still_open is not None:
            return SendResult(ok=False, to=target, detail="preview popup stayed open after clicking send")
        return SendResult(ok=True, to=target, detail="file sent (preview closed)")

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


async def inspect_attach(driver: "EitaaDriver", file_path: str | None = None) -> dict:
    """Open Saved Messages and reveal the file-upload UI.

    Dumps the composer buttons (the paperclip lives here), any hidden file
    <input>s, and the attach dropdown menu items. If a file_path is given, it
    also attaches that file to reveal the media-preview popup (caption box +
    send button) and then cancels WITHOUT sending. Only structural info
    (classes/aria/labels) is returned -- never message content.
    """
    from eitaa import selectors as S

    page = driver.page
    out: dict = {
        "chat_opened": False,
        "composer_buttons": [],
        "file_inputs": [],
        "attach_button_found": False,
        "attach_menu": [],
        "media_popup": None,
    }

    try:
        await driver.open_saved_messages()
        out["chat_opened"] = True
    except DriverError as exc:
        out["error"] = str(exc)
        return out

    # Buttons in the composer row (the paperclip/attach button is here).
    try:
        out["composer_buttons"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'.chat-input .btn-icon, .input-message-container .btn-icon, "
            ".rows-wrapper .btn-icon, .new-message-wrapper .btn-icon'))"
            ".slice(0,24).map(b => ({cls:b.className||null, aria:b.getAttribute('aria-label')||null}))"
        )
    except Exception:  # noqa: BLE001
        pass

    # Hidden file inputs (tweb keeps them in the DOM ready to receive files).
    try:
        out["file_inputs"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('input[type=file]'))"
            ".slice(0,10).map(i => ({cls:i.className||null, accept:i.accept||null, multiple:i.multiple}))"
        )
    except Exception:  # noqa: BLE001
        pass

    # Locate the paperclip and dump its exact DOM neighborhood so we can see
    # whether it carries a child <input type=file>, a sibling menu, etc.
    attach = await _first_visible(
        page,
        [
            ".chat-input .btn-icon.tgico-attach",
            ".btn-icon.tgico-attach",
            ".attach-file",
        ],
        timeout=4000,
    )
    out["attach_button_found"] = attach is not None
    try:
        out["attach_dom"] = await page.evaluate(
            """
            () => {
              const el = document.querySelector('.attach-file, .btn-icon.tgico-attach');
              if (!el) return null;
              const cs = (n) => n ? (n.className || n.tagName) : null;
              const parent = el.parentElement;
              return {
                self: el.className || null,
                parent: cs(parent),
                next_sibling: cs(el.nextElementSibling),
                children: Array.from(el.children).slice(0,8).map(c => c.tagName + '.' + (c.className||'')),
                parent_children: parent ? Array.from(parent.children).slice(0,12).map(c => c.tagName + '.' + (c.className||'')) : [],
                child_file_input: !!el.querySelector('input[type=file]'),
                parent_file_input: parent ? !!parent.querySelector('input[type=file]') : false,
              };
            }
            """
        )
    except Exception:  # noqa: BLE001
        out["attach_dom"] = None

    # Watch for a native file chooser firing when we click the paperclip.
    fc_state = {"fired": False}

    def _on_fc(_fc):  # noqa: ANN001
        fc_state["fired"] = True

    page.on("filechooser", _on_fc)
    if attach is not None:
        try:
            await attach.click()
            await page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001
            pass
    out["filechooser_fired_on_paperclip_click"] = fc_state["fired"]

    # Report every popup and every TRULY-visible menu that exists now.
    try:
        out["popups_after_click"] = await page.evaluate(
            """
            () => {
              const vis = (el) => {
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden'
                  && el.getClientRects().length > 0;
              };
              return Array.from(document.querySelectorAll('.popup')).slice(0,10).map(p => ({
                cls: p.className || null, visible: vis(p)
              }));
            }
            """
        )
        out["visible_menus_after_click"] = await page.evaluate(
            """
            () => {
              const vis = (el) => {
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden'
                  && el.getClientRects().length > 0;
              };
              return Array.from(document.querySelectorAll('.btn-menu'))
                .filter(vis).slice(0,6).map(m => ({
                  cls: m.className || null,
                  items: Array.from(m.querySelectorAll('.btn-menu-item')).slice(0,15).map(b => ({
                    cls: b.className || null, text: (b.textContent||'').trim().slice(0,30)
                  }))
                }));
            }
            """
        )
    except Exception:  # noqa: BLE001
        pass

    # Strategy A test: set the file directly on the persistent hidden input and
    # see whether a media-preview popup opens. This is the simplest reliable
    # path if it works.
    if file_path:
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
            finput = page.locator("input[type=file]").first
            if await finput.count() > 0:
                await finput.set_input_files(file_path)
                await page.wait_for_timeout(2200)
                out["after_set_input_files"] = await page.evaluate(
                    """
                    () => {
                      const vis = (el) => {
                        const s = getComputedStyle(el);
                        return s.display !== 'none' && s.visibility !== 'hidden'
                          && el.getClientRects().length > 0;
                      };
                      const popups = Array.from(document.querySelectorAll('.popup'))
                        .filter(vis).map(p => p.className || null);
                      const media = document.querySelector('.popup-new-media, .popup.active');
                      let detail = null;
                      if (media) {
                        detail = {
                          cls: media.className || null,
                          editables: Array.from(media.querySelectorAll('.input-message-input, [contenteditable="true"]'))
                            .slice(0,6).map(n => ({cls:n.className||null, placeholder:n.getAttribute('data-placeholder')||null})),
                          buttons: Array.from(media.querySelectorAll('button, .btn-send, .btn-primary'))
                            .slice(0,14).map(b => ({cls:b.className||null, text:(b.textContent||'').trim().slice(0,20)}))
                        };
                      }
                      return { visible_popups: popups, media_popup: detail };
                    }
                    """
                )
        except Exception as exc:  # noqa: BLE001
            out["after_set_input_files_error"] = f"{type(exc).__name__}: {exc}"

    page.remove_listener("filechooser", _on_fc)

    # Clean up without sending anything.
    for _ in range(3):
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
    return out



async def inspect_login(driver: "EitaaDriver") -> dict:
    """Dump the Eitaa Web auth-page structure so we can build auto-login.

    Run this against a FRESH (not-logged-in) profile so the phone/code form is
    on screen. Returns only structural info (input types/classes/placeholders,
    button classes/text, visible labels) -- never any typed value.
    """
    page = driver.page
    out: dict = {
        "logged_in": await driver.is_logged_in(),
        "url": None,
        "inputs": [],
        "buttons": [],
        "labels": [],
        "selects": [],
    }
    try:
        out["url"] = page.url
    except Exception:  # noqa: BLE001
        pass

    try:
        out["inputs"] = await page.evaluate(
            """
            () => {
              const vis = (el) => {
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden'
                  && el.getClientRects().length > 0;
              };
              return Array.from(document.querySelectorAll('input, [contenteditable="true"]'))
                .filter(vis).slice(0, 20).map(i => ({
                  tag: i.tagName,
                  type: i.getAttribute('type'),
                  cls: i.className || null,
                  id: i.id || null,
                  name: i.getAttribute('name'),
                  inputmode: i.getAttribute('inputmode'),
                  placeholder: i.getAttribute('placeholder')
                    || i.getAttribute('data-placeholder') || null,
                  autocomplete: i.getAttribute('autocomplete'),
                }));
            }
            """
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        out["buttons"] = await page.evaluate(
            """
            () => {
              const vis = (el) => {
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden'
                  && el.getClientRects().length > 0;
              };
              return Array.from(document.querySelectorAll('button, .btn, [role="button"]'))
                .filter(vis).slice(0, 20).map(b => ({
                  cls: b.className || null,
                  text: (b.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40),
                  aria: b.getAttribute('aria-label') || null,
                }));
            }
            """
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        out["selects"] = await page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'.input-field, .selector, [class*=country], select'))"
            ".slice(0,12).map(n => ({cls:n.className||null, "
            "text:(n.textContent||'').replace(/\\s+/g,' ').trim().slice(0,40)}))"
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        out["labels"] = await page.evaluate(
            """
            () => {
              const vis = (el) => {
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden'
                  && el.getClientRects().length > 0;
              };
              return Array.from(document.querySelectorAll(
                'label, h1, h2, h3, h4, .subtitle, .auth-title, [class*=title]'
              )).filter(vis).slice(0, 15).map(n =>
                (n.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60)
              ).filter(Boolean);
            }
            """
        )
    except Exception:  # noqa: BLE001
        pass

    return out
