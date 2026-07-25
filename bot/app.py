"""Telegram control panel (Telethon) for the Mkwlsoso Eitaa manager.

Owner-only. English ReconBot-style panel. Wires the Eitaa operations (send
text/file, range contact creation, stats) behind inline buttons and posts
English log cards. Two engines are selectable in Settings:
  • bridge  — drives Eitaa Web (tweb) in a headless Chromium
  • direct  — browser-free MTProto (direct/) for contact building

Live jobs (contact build / send) edit ONE card in place (LiveCard).
"""

from __future__ import annotations

import asyncio
import re
import socket
import time

from telethon import TelegramClient, events, Button
from telethon.errors import MessageNotModifiedError

from config import config
from bot import cards
from bot import contacts_store
from bot.store import store
from bot.runner import manager, expand_range


# ---- helpers -----------------------------------------------------------

# Every list in the panel is paged at this size so a keyboard never grows huge.
PAGE_SIZE = 10


def list_accounts() -> list[str]:
    d = config.PROFILES_DIR
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def account_name_for_phone(phone: str) -> str:
    """The profile (account) name for a phone number: its digits.

    Accounts used to need a made-up name; now the number is the identity, so the
    profile directory is simply "989304683887" and every list can show the number
    with nothing extra to remember. Iranian numbers are normalized to 98XXXXXXXXXX
    so 0930..., 930... and +98930... all map to the SAME account.
    """
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        return "98" + digits
    # Not an Iranian mobile: keep the digits as given rather than mangling them.
    return re.sub(r"\D", "", phone or "")


def _page_slice(items: list, page: int) -> tuple[list, int, int]:
    """(items on this page, clamped page index, page count) for PAGE_SIZE paging."""
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    return items[start:start + PAGE_SIZE], page, pages


def _pager_row(prefix: str, page: int, pages: int) -> list:
    """A ◀ / page / ▶ row for a paged keyboard (only when there's >1 page)."""
    if pages <= 1:
        return []
    prev_page = (page - 1) % pages
    next_page = (page + 1) % pages
    return [
        Button.inline("◀", f"{prefix}{prev_page}".encode()),
        Button.inline(f"{page + 1}/{pages}", b"noop"),
        Button.inline("▶", f"{prefix}{next_page}".encode()),
    ]


def delete_account_files(account: str) -> list[str]:
    """Remove an account's browser profile and its per-account artifacts.

    Returns human labels of what was actually removed, for the log card.
    """
    import shutil

    removed: list[str] = []
    profile = config.profile_dir(account)
    try:
        if profile.is_dir():
            shutil.rmtree(profile)
            removed.append("browser profile")
    except OSError as exc:
        print(f"[account] could not remove profile {profile}: {exc}", flush=True)

    if contacts_store.forget(account):
        removed.append("saved contacts")

    # Saved peers live in the isolated direct/ store; ask it to clean up.
    try:
        from direct import peers as peer_store
        if peer_store.forget(account):
            removed.append("saved peers")
    except Exception:  # noqa: BLE001 - direct/ may have been deleted on purpose
        pass

    # Captured session + cookies for this account (gitignored artifacts).
    sessions = config.ARTIFACTS_DIR / "sessions"
    if sessions.is_dir():
        patterns = (f"capall_{account}_*.json", f"worker_tx_{account}_*.json",
                    f"cookies_{account}.json", f"{account}_*.json")
        hit = False
        for pattern in patterns:
            for path in sessions.glob(pattern):
                try:
                    path.unlink()
                    hit = True
                except OSError:
                    pass
        if hit:
            removed.append("captured session")
    return removed


def is_owner(event) -> bool:
    return config.OWNER_ID and event.sender_id == config.OWNER_ID


def _ping_blocking() -> int | None:
    """Round-trip (ms) to open a TCP connection to the Eitaa host. None on fail."""
    try:
        t = time.monotonic()
        with socket.create_connection((config.PING_HOST, 443), timeout=4):
            pass
        return int((time.monotonic() - t) * 1000)
    except Exception:  # noqa: BLE001
        return None


async def server_ping_ms() -> int | None:
    try:
        return await asyncio.to_thread(_ping_blocking)
    except Exception:  # noqa: BLE001
        return None


# conversation state: owner_id -> {"step": str, ...}
pending: dict = {}

# The session file lives under DATA_DIR; make sure it exists BEFORE Telethon
# opens its SQLite session (the client is created at import time).
config.DATA_DIR.mkdir(parents=True, exist_ok=True)

bot = TelegramClient(
    str(config.DATA_DIR / "panel_bot"), config.API_ID, config.API_HASH
)


async def report(text: str) -> None:
    try:
        await bot.send_message(config.report_to(), text)
    except Exception:  # noqa: BLE001
        pass


class LiveCard:
    """One Telegram message edited in place while a job runs (throttled)."""

    def __init__(self, chat_id, min_interval: float = 2.0) -> None:
        self.chat_id = chat_id
        self.min_interval = min_interval
        self._msg = None
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def set(self, text: str, force: bool = False) -> None:
        async with self._lock:
            now = time.monotonic()
            if not force and self._msg is not None and (now - self._last) < self.min_interval:
                return
            try:
                if self._msg is None:
                    self._msg = await bot.send_message(self.chat_id, text)
                else:
                    await self._msg.edit(text)
                self._last = now
            except MessageNotModifiedError:
                pass
            except Exception:  # noqa: BLE001
                pass


# ---- keyboards ---------------------------------------------------------

def kb_home():
    """Home is now four clear sections; Multi Send is one of them, not buried
    inside the accounts list."""
    return [
        [Button.inline("👤 Accounts", b"menu:accounts"),
         Button.inline("🚀 Multi Send", b"multi:open:0")],
        [Button.inline("📝 Content", b"menu:content"),
         Button.inline("⚙ Settings", b"menu:settings")],
        [Button.inline("➕ Add Account", b"acc:add"),
         Button.inline("🔄 Refresh", b"menu:home")],
    ]


def kb_back():
    return [[Button.inline("⬅ Back", b"menu:home")]]


def kb_accounts(page: int = 0):
    """Accounts list, 10 per page. Each row: the number + its saved contact count,
    with a mark for the active one and for anything currently running."""
    accounts = list_accounts()
    shown, page, pages = _page_slice(accounts, page)
    active = store.active_account
    rows = []
    for acc in shown:
        if manager.is_busy(acc):
            mark = "⏳"
        elif acc == active:
            mark = "🟢"
        else:
            mark = "•"
        n = contacts_store.count(acc)
        label = f"{mark} {store.account_phone(acc)}" + (f" · {n:,}" if n else "")
        rows.append([Button.inline(label, f"acc:open:{acc}".encode())])
    pager = _pager_row("acc:page:", page, pages)
    if pager:
        rows.append(pager)
    rows.append([Button.inline("➕ Add Account", b"acc:add"),
                 Button.inline("⬅ Home", b"menu:home")])
    return rows


def kb_account_panel(acc: str):
    busy = manager.is_busy(acc)
    rows = [
        [Button.inline("📤 Send", b"pnl:send"),
         Button.inline("➕ Build Contacts", b"pnl:contacts")],
        [Button.inline("📥 Save Contacts", b"pnl:save"),
         Button.inline("🔄 Refresh", b"pnl:refresh")],
    ]
    if busy:
        # The label escalates: a second press force-stops.
        label = "❌ Force Stop" if manager.account_stopping(acc) else "⏹ Stop"
        rows.append([Button.inline(label, b"pnl:stop")])
    rows.append([Button.inline("🗑 Delete Account", f"acc:del:{acc}".encode())])
    rows.append([Button.inline("👤 Accounts", b"menu:accounts"),
                 Button.inline("⬅ Home", b"menu:home")])
    return rows


def kb_confirm_delete(acc: str):
    return [
        [Button.inline("✅ Yes, delete", f"acc:delx:{acc}".encode()),
         Button.inline("❌ Cancel", f"acc:open:{acc}".encode())],
    ]


def kb_multi(page: int = 0):
    """Tick several accounts, then send from all of them at once (10 per page).

    Each row shows the account's saved contact count, so it is obvious how much
    reach a tick actually adds.
    """
    accounts = list_accounts()
    selected = store.prune_selected(accounts)
    shown, page, pages = _page_slice(accounts, page)
    rows = []
    for acc in shown:
        # Ticked accounts show their position, because the order is the send order.
        if acc in selected:
            mark = f"{selected.index(acc) + 1}️⃣"
        else:
            mark = "☐"
        n = contacts_store.count(acc)
        label = f"{mark} {store.account_phone(acc)} · " + (f"{n:,}" if n else "—")
        rows.append([Button.inline(label, f"multi:tog:{page}:{acc}".encode())])
    pager = _pager_row("multi:open:", page, pages)
    if pager:
        rows.append(pager)
    if accounts:
        if manager.multi_jobs():
            label = "❌ Force Stop Run" if manager.multi_stopping() else "⏹ Stop Run"
            rows.append([Button.inline(label, b"multi:stop")])
        else:
            reach = sum(contacts_store.count(a) for a in selected)
            rows.append([Button.inline(
                f"🚀 Send · {len(selected)} acct · {reach:,} contacts", b"multi:go")])
        rows.append([Button.inline("☑ All", f"multi:all:{page}".encode()),
                     Button.inline("🧹 Clear", f"multi:clear:{page}".encode())])
    rows.append([Button.inline("⬅ Home", b"menu:home")])
    return rows


def kb_content():
    return [
        [Button.inline("📝 Set Text", b"content:text"),
         Button.inline("📎 Set File", b"content:file")],
        [Button.inline("🗑 Clear", b"content:clear")],
        [Button.inline("⬅ Back", b"menu:home")],
    ]


def kb_settings():
    rows = []
    # The engine switch is hidden: the panel is bridge-only. direct/ is still in
    # the source and MKWL_ENABLE_DIRECT=1 brings this button back.
    if config.ENABLE_DIRECT:
        eng = store.engine
        other = "direct ⚡" if eng == "bridge" else "bridge 🌉"
        rows.append([Button.inline(f"🔧 Engine: {eng}  →  switch to {other}",
                                   b"set:engine")])
    rows += [
        [Button.inline("⏱ Send Delay", b"set:textdelay"),
         Button.inline("⏱ Contact Delay", b"set:contactdelay")],
        [Button.inline("🔢 Log Every N", b"set:logevery")],
        [Button.inline("⬅ Back", b"menu:home")],
    ]
    return rows


# ---- panel renderers ---------------------------------------------------

async def home_text() -> str:
    ping = await server_ping_ms()
    accounts = list_accounts()
    saved_total = sum(contacts_store.count(a) for a in accounts)
    return cards.panel_home(
        config.BOT_VERSION, len(accounts),
        store.account_phone(store.active_account) if store.active_account else None,
        engine=store.engine if config.ENABLE_DIRECT else None,
        ping_ms=ping, contacts=saved_total,
        running=len(manager.active_jobs()),
        content=store.content_summary() if store.content.get("kind") else None,
    )


def accounts_text() -> str:
    accs = list_accounts()
    saved = sum(contacts_store.count(a) for a in accs)
    running = [a for a in accs if manager.is_busy(a)]
    return cards.card(
        "👤 ACCOUNTS",
        [
            ("Total   ", len(accs)),
            ("Contacts", f"{saved:,} saved" if saved else "none saved yet"),
            ("Active  ", store.account_phone(store.active_account)
             if store.active_account else "—"),
            ("Running ", f"⏳ {len(running)}" if running else None),
        ],
        footer=("Tap a number to open it. 🟢 active · ⏳ busy · the number after each "
                "account is its saved contacts."
                if accs else "No accounts yet. Use ➕ Add Account."),
    )


def multi_text() -> str:
    """The Multi Send section: pick accounts, see the order and combined reach."""
    accounts = list_accounts()
    selected = store.prune_selected(accounts)
    reach = sum(contacts_store.count(a) for a in selected)
    unsaved = [a for a in selected if not contacts_store.count(a)]
    running = manager.multi_jobs()

    pairs = [
        ("Accounts", f"{len(selected)} of {len(accounts)} ticked"),
        ("Reach   ", f"{reach:,} contacts" if reach else "—"),
        ("Content ", store.content_summary()),
        ("Delay   ", f"{store.text_send_delay:g}s between messages"),
    ]
    # The tick order IS the send order, so show it.
    body = None
    if selected:
        body = "Order:\n" + "\n".join(
            f"{i}. {store.account_phone(a)} · "
            + (f"{contacts_store.count(a):,}" if contacts_store.count(a) else "reads first")
            for i, a in enumerate(selected, start=1))

    if running:
        footer = ("🛑 Stopping… press Force Stop Run to cut it off immediately."
                  if manager.multi_stopping()
                  else "⏳ A run is in progress. Stop Run ends it after the message "
                       "in flight; press it twice to force.")
    elif not accounts:
        footer = "No accounts yet. Add one first."
    elif not selected:
        footer = ("Tick accounts in the order you want them used — the first one "
                  "ticked sends first.")
    else:
        footer = (f"{len(selected)} account(s) run ONE AFTER ANOTHER into a single "
                  "live card. When one finishes, stops, or its session fails, the "
                  "next begins.")
        if unsaved:
            footer += (f"\n📥 {len(unsaved)} of them have no saved contacts yet; their "
                       "list is read automatically before sending starts.")
    return cards.card("🚀 MULTI SEND", pairs, footer=footer, body=body)


def peer_count(acc: str) -> int | None:
    """How many contacts the browser-free sender can reach for this account."""
    try:
        from direct import peers as peer_store
        return peer_store.count(acc)
    except Exception:  # noqa: BLE001 - direct/ is optional by design
        return None


def account_panel_text(acc: str) -> str:
    meta = store.account_meta(acc)
    return cards.account_panel(
        acc, store.account_phone(acc),
        meta.get("contacts"), meta.get("pvs"),
        store.engine, manager.is_busy(acc), peers=peer_count(acc),
        saved=contacts_store.count(acc), saved_age=contacts_store.age_hours(acc),
    )


def content_text() -> str:
    return cards.card("📝 CONTENT", [("Current", store.content_summary())],
                      footer="Set the text or file the bot will send.")


def settings_text() -> str:
    pairs = [
        ("Send delay   ", f"{store.text_send_delay:g}s between messages"),
        ("Contact delay", f"{store.contact_create_delay:g}s between batches"),
        ("Log every    ", f"{store.send_log_every} sends"),
    ]
    if config.ENABLE_DIRECT:
        pairs.insert(0, ("Engine       ",
                         "🌉 bridge (browser)" if store.engine == "bridge"
                         else "⚡ direct (no browser)"))
    return cards.card(
        "⚙ SETTINGS", pairs,
        footer="Longer delays are safer against Eitaa's rate limits.",
    )


# ---- command + callback handlers ---------------------------------------

async def show_home(event, edit=False):
    text = await home_text()
    if edit:
        await event.edit(text, buttons=kb_home())
    else:
        await event.respond(text, buttons=kb_home())


@bot.on(events.NewMessage(pattern=r"^/start"))
async def _start(event):
    if not is_owner(event):
        try:
            await event.respond("This panel is private.")
        except Exception:  # noqa: BLE001
            pass
        return
    pending.pop(event.sender_id, None)
    try:
        await show_home(event)
    except Exception as exc:  # noqa: BLE001
        await report(cards.error_card("start", code=type(exc).__name__, detail=str(exc)))


@bot.on(events.CallbackQuery)
async def _callbacks(event):
    if not is_owner(event):
        await event.answer("Not authorized.", alert=True)
        return
    try:
        await _handle_callback(event)
    except MessageNotModifiedError:
        await event.answer()
    except Exception as exc:  # noqa: BLE001
        await report(cards.error_card("panel_callback", code=type(exc).__name__,
                                      detail=str(exc), phase="callback"))
        try:
            await event.answer("Error handled; a log card was sent.", alert=True)
        except Exception:  # noqa: BLE001
            pass


async def _handle_callback(event):
    data = event.data.decode()
    active = store.active_account

    if data == "menu:home":
        pending.pop(event.sender_id, None)
        return await show_home(event, edit=True)
    if data == "noop":
        return await event.answer()
    if data == "menu:accounts":
        return await event.edit(accounts_text(), buttons=kb_accounts(0))
    if data.startswith("acc:page:"):
        page = int(data.rsplit(":", 1)[1] or 0)
        return await event.edit(accounts_text(), buttons=kb_accounts(page))
    if data == "menu:content":
        return await event.edit(content_text(), buttons=kb_content())
    if data == "menu:settings":
        return await event.edit(settings_text(), buttons=kb_settings())

    # ---- accounts ----
    if data.startswith("acc:open:"):
        acc = data.split(":", 2)[2]
        store.set_active_account(acc)
        await event.answer(f"Active: {store.account_phone(acc)}")
        return await event.edit(account_panel_text(acc), buttons=kb_account_panel(acc))
    if data == "acc:add":
        # No name step: the phone number IS the account, so that's all we ask.
        pending[event.sender_id] = {"step": "login_phone"}
        return await event.edit(
            cards.card("➕ ADD ACCOUNT",
                       footer="Send the phone number (e.g. 09304683887). Then I'll ask "
                              "for the login code, right here. No noVNC needed."),
            buttons=kb_back())

    # ---- delete an account ----
    if data.startswith("acc:del:"):
        acc = data.split(":", 2)[2]
        if manager.is_busy(acc):
            return await event.answer("Account is busy with a running job.", alert=True)
        return await event.edit(
            cards.card("🗑 DELETE ACCOUNT",
                       [("Phone", store.account_phone(acc))],
                       footer="This removes its browser profile, saved peers and captured "
                              "session. You would have to log in again. Are you sure?"),
            buttons=kb_confirm_delete(acc))
    if data.startswith("acc:delx:"):
        acc = data.split(":", 2)[2]
        if manager.is_busy(acc):
            return await event.answer("Account is busy with a running job.", alert=True)
        phone = store.account_phone(acc)
        removed = delete_account_files(acc)
        store.remove_account(acc)
        await event.answer("Deleted.")
        await report(cards.account_deleted(phone, removed))
        return await event.edit(accounts_text(), buttons=kb_accounts(0))

    # ---- multi-account send ----
    if data.startswith("multi:open:"):
        page = int(data.rsplit(":", 1)[1] or 0)
        return await event.edit(multi_text(), buttons=kb_multi(page))
    if data.startswith("multi:tog:"):
        _, _, page_s, acc = data.split(":", 3)
        store.toggle_selected(acc)
        return await event.edit(multi_text(), buttons=kb_multi(int(page_s or 0)))
    if data.startswith("multi:clear:"):
        page = int(data.rsplit(":", 1)[1] or 0)
        store.clear_selected()
        return await event.edit(multi_text(), buttons=kb_multi(page))
    if data.startswith("multi:all:"):
        page = int(data.rsplit(":", 1)[1] or 0)
        store.set_selected(list_accounts())
        return await event.edit(multi_text(), buttons=kb_multi(page))
    if data == "multi:go":
        return await _start_multi_send(event)
    if data == "multi:stop":
        force = manager.multi_stopping()
        n = manager.stop_multi(force=force)
        if not n:
            await event.answer("No multi-account run is active.", alert=True)
        elif force:
            await event.answer("Force-stopped the whole run.")
        else:
            await event.answer("Stopping now — press again to force.")
        return await event.edit(multi_text(), buttons=kb_multi(0))

    # ---- per-account panel actions (operate on active) ----
    if data == "pnl:refresh":
        if not active:
            return await event.answer("No active account.", alert=True)
        return await _refresh_account(event, active)
    if data == "pnl:save":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        if manager.is_busy(active):
            return await event.answer("Account already has a running job.", alert=True)
        await manager.run_save_contacts(active, report, store.account_phone(active))
        await event.answer("Saving contacts…")
        return await event.edit(
            cards.card("📥 SAVE CONTACTS",
                       [("Phone", store.account_phone(active))],
                       footer="Reading this account's full contacts list once and saving "
                              "it. This is the slow part — a 📥 CONTACTS SAVED card will "
                              "follow, and after that every send starts instantly."),
            buttons=kb_back())
    if data == "pnl:send":
        return await _start_send(event)
    if data == "pnl:contacts":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        pending[event.sender_id] = {"step": "await_prefix"}
        return await event.edit(
            cards.card("➕ BUILD CONTACTS", [("Account", store.account_phone(active)),
                                            ("Engine", store.engine)],
                       footer="Send a mobile prefix, e.g. 091646. The bot fills the rest "
                              "and builds contacts (batched, fast)."),
            buttons=kb_back())
    if data == "pnl:stop":
        if active:
            # First press: stop cleanly (every wait wakes at once, so this is
            # immediate unless a message is mid-flight). Press again to kill it.
            force = manager.account_stopping(active)
            n = manager.stop_account(active, force=force)
            if not n:
                await event.answer("Nothing is running on this account.", alert=True)
            elif force:
                await event.answer("Force-stopped.")
            else:
                await event.answer("Stopping now — press again to force.")
        return await event.edit(account_panel_text(active), buttons=kb_account_panel(active))

    # ---- content ----
    if data == "content:text":
        pending[event.sender_id] = {"step": "await_text"}
        return await event.edit(
            cards.card("📝 SET TEXT", footer="Send me the text to store."),
            buttons=kb_back())
    if data == "content:file":
        pending[event.sender_id] = {"step": "await_file"}
        return await event.edit(
            cards.card("📎 SET FILE",
                       footer="Upload the file (any type Eitaa allows: zip, pdf, ...). "
                              "Add a caption to the upload to store it too."),
            buttons=kb_back())
    if data == "content:clear":
        store.clear_content()
        await event.answer("Content cleared.")
        return await event.edit(content_text(), buttons=kb_content())

    # ---- settings ----
    if data == "set:engine":
        new = store.toggle_engine()
        await event.answer(f"Engine: {new}")
        return await event.edit(settings_text(), buttons=kb_settings())
    if data == "set:textdelay":
        pending[event.sender_id] = {"step": "await_textdelay"}
        return await event.edit(
            cards.card("⏱ TEXT SEND DELAY", [("Current", f"{store.text_send_delay:g}s")],
                       footer="Send a number of seconds (e.g. 8)."), buttons=kb_back())
    if data == "set:contactdelay":
        pending[event.sender_id] = {"step": "await_contactdelay"}
        return await event.edit(
            cards.card("⏱ CONTACT CREATE DELAY", [("Current", f"{store.contact_create_delay:g}s")],
                       footer="Send a number of seconds (e.g. 0.2 for fast)."), buttons=kb_back())
    if data == "set:logevery":
        pending[event.sender_id] = {"step": "await_logevery"}
        return await event.edit(
            cards.card("🔢 LOG EVERY N", [("Current", store.send_log_every)],
                       footer="Send how many sends per progress card (e.g. 50)."),
            buttons=kb_back())

    await event.answer()


async def _refresh_account(event, acc: str):
    if manager.is_busy(acc):
        return await event.answer("Account is busy with a running job.", alert=True)
    await event.answer("Refreshing stats…")

    async def _run():
        from capture.browser import open_session
        from eitaa.driver import EitaaDriver
        try:
            async with open_session(acc) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if not await driver.is_logged_in():
                    await report(cards.error_card("stats", acc, code="not_logged_in",
                                                  detail="account is not logged in"))
                    return
                s = await driver.bridge_stats()
                if s is None:
                    s = await driver.get_stats()
                store.set_account_meta(acc, contacts=s.get("contacts"), pvs=s.get("pvs"))
                await report(cards.account_panel(
                    acc, store.account_phone(acc), s.get("contacts"), s.get("pvs"),
                    store.engine, False, peers=peer_count(acc)))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("stats", acc, code=type(exc).__name__,
                                          detail=str(exc), phase="refresh"))

    asyncio.create_task(_run())


async def _start_send(event):
    active = store.active_account
    if not active:
        return await event.answer("Select an account first.", alert=True)
    if store.content.get("kind") not in ("text", "file"):
        return await event.answer("Set content first (Content menu).", alert=True)
    if manager.is_busy(active):
        return await event.answer("Account already has a running job.", alert=True)
    live = LiveCard(config.report_to())
    await manager.run_send(active, dict(store.content), dict(store.settings), report,
                           live=live, account_phone=store.account_phone(active))
    await event.answer("Send job started.")
    await event.edit(account_panel_text(active), buttons=kb_account_panel(active))


async def _start_multi_send(event):
    """Send from every ticked account at once, into ONE shared live card."""
    accounts = list_accounts()
    selected = store.prune_selected(accounts)
    if not selected:
        return await event.answer("Tick at least one account first.", alert=True)
    if store.content.get("kind") not in ("text", "file"):
        return await event.answer("Set content first (Content menu).", alert=True)
    busy = [a for a in selected if manager.is_busy(a)]
    free = [a for a in selected if not manager.is_busy(a)]
    if not free:
        return await event.answer("All selected accounts already have a running job.",
                                  alert=True)
    # With the browser-free engine (only reachable when explicitly enabled) an
    # account without saved peers can send NOTHING. Say so BEFORE starting.
    no_peers = []
    if store.engine == "direct":
        no_peers = [a for a in free if not (peer_count(a) or 0)]
        if len(no_peers) == len(free):
            return await event.answer(
                "None of the selected accounts have saved peers. Tap 'Save Contacts' "
                "on each first.", alert=True)

    live = LiveCard(config.report_to())
    # Selection ORDER matters: the first account ticked sends first.
    pairs = [(a, store.account_phone(a)) for a in free]
    reach = sum(contacts_store.count(a) for a in free)
    await manager.run_send_multi(pairs, dict(store.content),
                                 dict(store.settings), report, live=live)
    await event.answer(f"Queued {len(pairs)} account(s).")
    notes = ["Accounts run ONE AT A TIME in the order below. When one finishes "
             "(or stops, or its session fails) the next one starts."]
    if busy:
        notes.append("Skipped (already busy): "
                     + ", ".join(store.account_phone(a) for a in busy))
    unsaved = [a for a in free if not contacts_store.count(a)]
    if unsaved:
        notes.append(f"📥 {len(unsaved)} account(s) have no saved contacts yet, so "
                     "their list is read first. A queue card with the grand total "
                     "follows.")
    if no_peers:
        notes.append(f"🚧 {len(no_peers)} account(s) have no saved peers and will send "
                     "nothing: " + ", ".join(store.account_phone(a) for a in no_peers))
    order = "\n".join(f"{i}. {p}" for i, (_, p) in enumerate(pairs, start=1))
    await event.edit(
        cards.card("🚀 MULTI SEND QUEUED",
                   [("Accounts", len(pairs)),
                    ("Reach   ", f"{reach:,} contacts" if reach else "reading…"),
                    ("Content ", store.content_summary())],
                   body="Order:\n" + order,
                   footer="\n".join(notes)),
        buttons=[[Button.inline("⏹ Stop Run", b"multi:stop")],
                 [Button.inline("⬅ Home", b"menu:home")]])


@bot.on(events.NewMessage)
async def _conversation(event):
    if not is_owner(event):
        return
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        return
    st = pending.get(event.sender_id)
    if not st:
        return
    step = st.get("step")

    if step == "await_text":
        if not text:
            return await event.respond("Send non-empty text.")
        store.set_text_content(text)
        pending.pop(event.sender_id, None)
        return await event.respond(content_text(), buttons=kb_content())

    if step == "await_file":
        if not event.message.file:
            return await event.respond("Upload a file (as a document).")
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        dest_dir = config.DATA_DIR / "content"
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = event.message.file.name or f"file_{event.message.id}"
        dest = (dest_dir / name).resolve()
        await event.message.download_media(file=str(dest))
        caption = text or ""
        store.set_file_content(str(dest), name, caption)
        pending.pop(event.sender_id, None)
        return await event.respond(content_text(), buttons=kb_content())

    if step == "login_phone":
        phone = re.sub(r"[^\d+]", "", text or "")
        if len(re.sub(r"\D", "", phone)) < 10:
            return await event.respond("Send a valid phone number (e.g. 09304683887).")
        # The phone number IS the account: no separate name to invent or remember.
        name = account_name_for_phone(phone)
        if name in list_accounts():
            pending.pop(event.sender_id, None)
            return await event.respond(
                cards.card("➕ ADD ACCOUNT", [("Phone", store.account_phone(name))],
                           footer="This number is already added. Open it from Accounts."),
                buttons=kb_back())
        if manager.is_busy(name):
            return await event.respond("That account already has a running job. Try again later.")
        started = await manager.start_bridge_login(name, phone, report)
        if not started:
            pending.pop(event.sender_id, None)
            return await event.respond("That account is busy right now. Try again later.")
        st["login_account"] = name
        st["step"] = "login_code"
        return await event.respond(
            cards.card("➕ ADD ACCOUNT", [("Phone ", name), ("Status", "requesting code…")],
                       footer="Wait for the '📩 CODE SENT' card, then send the code here (digits only)."),
            buttons=kb_back())

    if step == "login_code":
        name = st.get("login_account")
        code = re.sub(r"\D", "", text or "")
        if not code:
            return await event.respond("Send the login code (digits only).")
        res = manager.submit_login_code(name or "", code)
        if res == "ok":
            pending.pop(event.sender_id, None)
            return await event.respond(
                cards.card("➕ ADD ACCOUNT", [("Account", name), ("Status", "signing in…")],
                           footer="Got the code. Watch for the ✅ ACCOUNT ADDED card."),
                buttons=kb_back())
        if res == "not_ready":
            return await event.respond("Not ready yet — wait for the '📩 CODE SENT' card, then resend the code.")
        if res == "already":
            return await event.respond("Code already submitted; hold on for the result card.")
        pending.pop(event.sender_id, None)
        return await event.respond("No active login for that account. Tap Add Account to start again.")

    if step in ("await_textdelay", "await_contactdelay"):
        try:
            val = float(text)
            if val < 0 or val > 3600:
                raise ValueError
        except ValueError:
            return await event.respond("Send a number of seconds between 0 and 3600.")
        key = "text_send_delay" if step == "await_textdelay" else "contact_create_delay"
        store.set_setting(key, val)
        pending.pop(event.sender_id, None)
        return await event.respond(settings_text(), buttons=kb_settings())

    if step == "await_logevery":
        try:
            val = int(text)
            if val < 1 or val > 100000:
                raise ValueError
        except ValueError:
            return await event.respond("Send an integer >= 1 (e.g. 50).")
        store.set_setting("send_log_every", val)
        pending.pop(event.sender_id, None)
        return await event.respond(settings_text(), buttons=kb_settings())

    if step == "await_prefix":
        entries, err = expand_range(text, 1)
        if err:
            return await event.respond(f"Invalid prefix: {err}")
        st["prefix"] = text
        st["step"] = "await_count"
        return await event.respond(
            cards.card("➕ BUILD CONTACTS", [("Prefix", text)],
                       footer="How many contacts to create? Send a number."),
            buttons=kb_back())

    if step == "await_count":
        try:
            count = int(text)
            if count < 1 or count > 100000:
                raise ValueError
        except ValueError:
            return await event.respond("Send an integer between 1 and 100000.")
        prefix = st.get("prefix", "")
        active = store.active_account
        pending.pop(event.sender_id, None)
        if not active:
            return await event.respond("No active account.")
        if manager.is_busy(active):
            return await event.respond("Account already has a running job.")
        live = LiveCard(config.report_to())
        await manager.run_contacts(active, prefix, count, dict(store.settings), report,
                                   live=live, account_phone=store.account_phone(active))
        return await event.respond(
            cards.card("➕ CONTACT BUILD QUEUED",
                       [("Account", store.account_phone(active)), ("Prefix", prefix),
                        ("Count", count), ("Engine", store.engine)],
                       footer="A live progress card will follow and update in place."),
            buttons=kb_back())


def main() -> None:
    config.ensure_dirs()
    missing = [k for k, v in {
        "API_ID": config.API_ID, "API_HASH": config.API_HASH,
        "BOT_TOKEN": config.BOT_TOKEN, "OWNER_ID": config.OWNER_ID,
    }.items() if not v]
    if missing:
        raise SystemExit(
            "Missing required bot config: " + ", ".join(missing)
            + ". Set them in .env (see .env.example)."
        )
    print("[bot] starting Telegram panel...", flush=True)
    bot.start(bot_token=config.BOT_TOKEN)
    try:
        me = bot.loop.run_until_complete(bot.get_me())
        print(f"[bot] logged in as @{me.username} (id={me.id})", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[bot] warning: could not fetch bot identity: {exc}", flush=True)
    print(f"[bot] online. OWNER_ID={config.OWNER_ID} REPORT_TO={config.report_to()}", flush=True)
    bot.run_until_disconnected()


if __name__ == "__main__":
    main()
