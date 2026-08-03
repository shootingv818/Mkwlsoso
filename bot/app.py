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
from bot import blocked_store
from bot import cards
from bot import contacts_store
from bot import direct_ctx
from bot import progress_store
from bot.store import store
from bot.runner import manager, expand_range
from capture.pool import pool as session_pool


# ---- helpers -----------------------------------------------------------

# Every list in the panel is paged at this size so a keyboard never grows huge.
PAGE_SIZE = 10


def list_accounts() -> list[str]:
    """Accounts in the order they were ADDED, newest last.

    Sorting by profile-directory name meant sorting by phone number, so a new
    account landed wherever its digits fell -- typically mid-list, on a page the
    owner was not on. Positions are assigned once (existing accounts keep their
    current alphabetical order) and persist, so anything added later goes to the
    end and shows up on the last page.
    """
    d = config.PROFILES_DIR
    if not d.is_dir():
        return []
    names = sorted(p.name for p in d.iterdir() if p.is_dir())
    try:
        store.ensure_account_order(names)
        return sorted(names, key=lambda n: (store.account_seq(n), n))
    except Exception:  # noqa: BLE001 - never lose the list over an ordering issue
        return names


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


def page_of(account: str | None, items: list[str] | None = None) -> int:
    """Which page `account` sits on, 0 when it is not in the list."""
    if not account:
        return 0
    seq = items if items is not None else list_accounts()
    try:
        return seq.index(account) // PAGE_SIZE
    except ValueError:
        return 0


def page_of_active() -> int:
    return page_of(store.active_account)


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

    if progress_store.clear(account):
        removed.append("sent log")

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


async def send_document(path: str, caption: str = "") -> object:
    """Deliver a file to the owner's chat.

    The panel only ever sent text before this; the photo export needs to hand
    over the PDFs it builds. Errors propagate so the job can report them on its
    own card instead of failing silently.
    """
    return await bot.send_file(config.report_to(), path, caption=caption,
                              force_document=True)


async def report(text: str) -> None:
    try:
        await bot.send_message(config.report_to(), text)
    except Exception:  # noqa: BLE001
        pass


class LiveCard:
    """One Telegram message edited in place while a job runs.

    IMPORTANT: `set()` does NOT wait for Telegram. It used to, and that put a
    Telegram round trip (plus any edit rate limiting Telethon sleeps through)
    directly inside the send loop - so a job that should pace itself by Eitaa's
    speed was also paying for its own progress card. Now the newest text is
    stashed and a single background painter delivers it.

    Only the LATEST text matters, so intermediate updates are dropped instead of
    queued; a card that is one tick behind is fine, a send loop that stalls is
    not.
    """

    def __init__(self, chat_id, min_interval: float = 2.0) -> None:
        self.chat_id = chat_id
        self.min_interval = min_interval
        self._msg = None
        self._pending: str | None = None
        self._sent_text: str | None = None
        self._painter: asyncio.Task | None = None
        self._wake = asyncio.Event()
        # Serialises the painter and flush(): without it they raced and the card
        # could end up showing an OLD state ('Sending 50/100' after 'Done').
        self._paint_lock = asyncio.Lock()
        self._closed = False
        self.paints = 0
        self.dropped = 0

    async def set(self, text: str, force: bool = False) -> None:
        """Queue `text` for painting. Returns immediately (never blocks a job)."""
        if self._closed:
            return
        if self._pending is not None:
            self.dropped += 1
        self._pending = text
        self._wake.set()
        if self._painter is None or self._painter.done():
            self._painter = asyncio.create_task(self._paint_loop())
        if force:
            # Give the painter a chance to flush a final/important state without
            # actually waiting on the network.
            await asyncio.sleep(0)

    async def _paint_loop(self) -> None:
        try:
            while not self._closed:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=30)
                except asyncio.TimeoutError:
                    if self._pending is None:
                        return
                self._wake.clear()
                async with self._paint_lock:
                    text, self._pending = self._pending, None
                    if text is None or text == self._sent_text:
                        continue
                    try:
                        if self._msg is None:
                            self._msg = await bot.send_message(self.chat_id, text)
                        else:
                            await self._msg.edit(text)
                        self._sent_text = text
                        self.paints += 1
                    except MessageNotModifiedError:
                        self._sent_text = text
                    except Exception:  # noqa: BLE001 - never break a job
                        pass
                # Rate-limit ourselves so Telegram never has to.
                await asyncio.sleep(self.min_interval)
        except asyncio.CancelledError:
            return

    async def flush(self) -> None:
        """Paint the last queued text now (used when a job ends).

        Takes the same lock as the painter, so the final state can never be
        overwritten by an in-flight older edit.
        """
        async with self._paint_lock:
            text, self._pending = self._pending, None
            if text is None or text == self._sent_text:
                return
            try:
                if self._msg is None:
                    self._msg = await bot.send_message(self.chat_id, text)
                else:
                    await self._msg.edit(text)
                self._sent_text = text
                self.paints += 1
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._closed = True
        if self._painter is not None:
            self._painter.cancel()
            self._painter = None


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
         Button.inline("🧪 Test to Me", b"pnl:dryrun")],
        [Button.inline("➕ Build Contacts", b"pnl:contacts")],
        # Contacts are saved automatically at login; this only re-reads them
        # when the account has gained new contacts since.
        [Button.inline("🔄 Update Contacts", b"pnl:save"),
         Button.inline("♻️ Refresh Panel", b"pnl:refresh")],
        # Read-only: runs the same login gate every job starts with, so a dead
        # session is found here instead of halfway through a campaign.
        [Button.inline("🔎 Check Session", b"pnl:check")],
        # Read-only: exports this account's photos to PDF, one photo per page.
        [Button.inline(f"🖼 Export Photos: {store.photo_direction}",
                       b"pnl:photos")],
        [Button.inline("🔁 Photo Filter", b"pnl:photodir")],
    ]
    if busy:
        # The label escalates: a second press force-stops.
        label = "❌ Force Stop" if manager.account_stopping(acc) else "⏹ Stop"
        rows.append([Button.inline(label, b"pnl:stop")])
    # Left: clears the resume ledger (current content goes to everyone again).
    # Right: clears the refused-peer cache so Eitaa gets asked about them again.
    rows.append([Button.inline("🧹 Reset Sent Log", b"pnl:resetlog"),
                 Button.inline("⛔ Reset Refused", b"pnl:resetblocked")])
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
    # The engine switch is back, now with three choices: bridge (proven page),
    # hybrid (browser-free sends with the page as a per-recipient safety net) and
    # direct (browser-free only, MKWL_ENABLE_DIRECT=1, no safety net).
    _ENGINE_MARK = {"bridge": "🌉 bridge", "hybrid": "⚡ hybrid", "direct": "🚀 direct"}
    rows.append([Button.inline(
        f"🔧 Engine: {_ENGINE_MARK.get(store.engine, store.engine)} — tap to change",
        b"set:engine")])
    rows += [
        [Button.inline("⏱ Send Delay", b"set:textdelay"),
         Button.inline("⚡ Concurrency", b"set:concurrency")],
        [Button.inline(
            f"🚫 Pause on limit: {'ON' if store.stop_on_limit else 'OFF'}",
            b"set:stoponlimit")],
        [Button.inline("⏱ Contact Delay", b"set:contactdelay"),
         Button.inline("🔢 Log Every N", b"set:logevery")],
        [Button.inline("🏠 Browser Standby", b"set:pool"),
         Button.inline(
             f"🚀 No-browser sends: {'ON' if store.browserless else 'OFF'}",
             b"set:browserless")],
        [Button.inline(
            f"📦 APK send mode: {'ON' if store.apk_octet else 'OFF'}",
            b"set:apkoctet")],
        [Button.inline(
            f"🔥 Warm Path: {'ON' if store.warmpath else 'OFF'}",
            b"set:warmpath")],
        [Button.inline("⬅ Back", b"menu:home")],
    ]
    return rows


# ---- panel renderers ---------------------------------------------------

async def home_text() -> str:
    ping = await server_ping_ms()
    accounts = list_accounts()
    counts = [contacts_store.count(a) for a in accounts]
    return cards.panel_home(
        len(accounts), sum(1 for n in counts if n),
        store.account_phone(store.active_account) if store.active_account else None,
        engine=store.engine if config.ENABLE_DIRECT else None,
        ping_ms=ping, contacts=sum(counts),
        running=len(manager.active_jobs()),
        content=store.content_summary() if store.content.get("kind") else None,
        last_run=store.last_run,
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
    meta_ts = meta.get("meta_updated")
    meta_age = ((time.time() - float(meta_ts)) / 3600.0) if meta_ts else None
    already = progress_store.done_count(
        acc, progress_store.content_key(store.content)) if store.content.get("kind") else 0
    return cards.account_panel(
        acc, store.account_phone(acc),
        meta.get("contacts"), meta.get("pvs"),
        store.engine, manager.is_busy(acc), peers=peer_count(acc),
        saved=contacts_store.count(acc), saved_age=contacts_store.age_hours(acc),
        meta_age=meta_age, pending=already,
        refused=blocked_store.count(acc),
        engine_ready=(direct_ctx.has_context(acc) if store.engine != "bridge" else None),
    )


def content_text() -> str:
    return cards.card("📝 CONTENT", [("Current", store.content_summary())],
                      footer="Set the text or file the bot will send.")


def settings_text() -> str:
    conc = store.send_concurrency
    pairs = [
        ("Send delay   ", f"{store.text_send_delay:g}s between messages"),
        ("Concurrency  ", f"{conc} at a time" + (" (sequential)" if conc == 1 else "")),
        ("On limit     ", "pause the run" if store.stop_on_limit
                          else "keep going, only report"),
        ("Contact delay", f"{store.contact_create_delay:g}s between batches"),
        ("Log every    ", f"{store.send_log_every} sends"),
        ("APK mode     ", "on — .apk sent as generic binary"
                          if store.apk_octet else "off — normal apk MIME"),
        ("Warm Path    ", "on — reuse the booted page, skip redundant loads"
                          if store.warmpath else "off — reload the web app per job"),
    ]
    eng = store.engine
    eng_txt = {
        "bridge": "🌉 bridge — every send goes through the browser page",
        "hybrid": "⚡ hybrid — browser-free sends, page as the safety net",
        "direct": "🚀 direct — browser-free only, no safety net",
    }.get(eng, eng)
    pairs.insert(0, ("Engine       ", eng_txt))
    active = store.active_account
    if active and eng != "bridge":
        age = direct_ctx.newest_capture_age_hours(active)
        pairs.insert(1, ("Engine ready ",
                         ("yes, session captured " +
                          ("just now" if age is not None and age < 1
                           else f"{int(age)}h ago" if age is not None else ""))
                         if direct_ctx.has_context(active)
                         else "not yet — it is captured on the next browser job"))
    return cards.card(
        "⚙ SETTINGS", pairs,
        footer="Longer delays are safer against Eitaa's rate limits. Hybrid sends "
               "without a browser and falls back to the page per recipient, so it is "
               "the fast option that keeps the proven path.",
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
    # Telegram invalidates a callback query after a few seconds, and a handler
    # here can open a browser (measured: 158s on the live host), so the answer
    # that came after the work failed with QueryIdInvalidError. Answering up
    # front is NOT an option: Telethon marks the query answered, which would
    # silently swallow every later alert like "Select an account first". Instead
    # a watchdog acknowledges only if the handler is still working after 6s.
    done = asyncio.Event()

    async def _ack_if_slow():
        try:
            await asyncio.wait_for(done.wait(), timeout=6)
        except asyncio.TimeoutError:
            try:
                await event.answer()
            except Exception:  # noqa: BLE001
                pass

    watchdog = asyncio.create_task(_ack_if_slow())
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
    finally:
        done.set()
        watchdog.cancel()


async def _handle_callback(event):
    data = event.data.decode()
    active = store.active_account

    if data == "menu:home":
        pending.pop(event.sender_id, None)
        return await show_home(event, edit=True)
    if data == "noop":
        return await event.answer()
    if data == "menu:accounts":
        # Land on the page holding the ACTIVE account. A freshly added account
        # becomes active and now sorts last, so the owner opens the list already
        # looking at it instead of hunting through pages.
        return await event.edit(accounts_text(),
                               buttons=kb_accounts(page_of_active()))
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
    if data == "pnl:check":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        if manager.is_busy(active):
            return await event.answer("Account already has a running job.", alert=True)
        await manager.run_session_check(active, report, store.account_phone(active),
                                        live=LiveCard(config.report_to()))
        await event.answer("Checking session…")
        return await event.edit(
            cards.card("🔎 SESSION CHECK",
                       [("Phone ", store.account_phone(active)),
                        ("Engine", store.engine)],
                       footer="Opening this account's session and asking Eitaa whether "
                              "it is still logged in. Nothing is sent to anybody and no "
                              "contacts are collected. A live card follows with the "
                              "result."),
            buttons=kb_back())
    if data == "pnl:photodir":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        now = store.cycle_photo_direction()
        await event.answer("Photo filter: " + now)
        return await event.edit(account_panel_text(active),
                               buttons=kb_account_panel(active))
    if data == "pnl:photos":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        if manager.is_busy(active):
            return await event.answer("Account already has a running job.", alert=True)
        from photo_export import cards as px_cards
        direction = store.photo_direction
        await manager.run_photo_export(
            active, report, store.account_phone(active),
            live=LiveCard(config.report_to()), direction=direction,
            send_document=send_document)
        await event.answer("Exporting photos…")
        return await event.edit(
            px_cards.started(account=active, phone=store.account_phone(active),
                             direction=direction),
            buttons=kb_back())
    if data == "pnl:save":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        if manager.is_busy(active):
            return await event.answer("Account already has a running job.", alert=True)
        await manager.run_save_contacts(active, report, store.account_phone(active))
        await event.answer("Updating contacts…")
        return await event.edit(
            cards.card("🔄 UPDATE CONTACTS",
                       [("Phone", store.account_phone(active))],
                       footer="Re-reading this account's contact list from Eitaa. "
                              "Contacts are already saved automatically at login, so "
                              "this is only needed after new contacts were added."),
            buttons=kb_back())
    if data == "pnl:resetlog":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        had = progress_store.done_count(
            active, progress_store.content_key(store.content))
        progress_store.clear(active)
        await event.answer("Sent log cleared.")
        return await event.edit(
            cards.card("🧹 SENT LOG CLEARED",
                       [("Phone   ", store.account_phone(active)),
                        ("Cleared ", f"{had:,} delivered marks" if had else "nothing to clear")],
                       footer="The next send treats every saved contact as new, so the "
                              "current content will be delivered again to all of them."),
            buttons=kb_account_panel(active))
    if data == "pnl:dryrun":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        if store.content.get("kind") not in ("text", "file"):
            return await event.answer("Set content first (Content menu).", alert=True)
        if manager.is_busy(active):
            return await event.answer("Account already has a running job.", alert=True)
        await manager.run_dry_run(active, dict(store.content), dict(store.settings),
                                  report, store.account_phone(active),
                                  live=LiveCard(config.report_to()))
        await event.answer("Test send started.")
        return await event.edit(
            cards.card("🧪 TEST SEND",
                       [("Phone  ", store.account_phone(active)),
                        ("Engine ", store.engine),
                        ("Content", store.content_summary())],
                       footer="Sending ONE copy to your own Saved Messages, using the "
                              "same engine a campaign would. Nobody else receives it. "
                              "A 🧪 TEST SEND card will follow with the result and the "
                              "real per-message time."),
            buttons=kb_back())
    if data == "pnl:resetblocked":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        had = blocked_store.count(active)
        blocked_store.clear(active)
        await event.answer("Refused list cleared.")
        return await event.edit(
            cards.card("⛔ REFUSED LIST CLEARED",
                       [("Phone   ", store.account_phone(active)),
                        ("Cleared ", f"{had:,} peers" if had else "nothing to clear")],
                       footer="Eitaa will be asked about these recipients again on the "
                              "next run. If they were refused because there is no "
                              "two-way contact, they will simply be refused again."),
            buttons=kb_account_panel(active))
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
        new = store.cycle_engine()
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
    if data == "set:stoponlimit":
        now = store.toggle_stop_on_limit()
        await event.answer("Pause on limit: " + ("ON" if now else "OFF"))
        return await event.edit(settings_text(), buttons=kb_settings())
    if data == "set:browserless":
        now = store.toggle_browserless()
        await event.answer("No-browser sends: " + ("ON" if now else "OFF"))
        return await event.edit(settings_text(), buttons=kb_settings())
    if data == "set:apkoctet":
        now = store.toggle_apk_octet()
        await event.answer("APK send mode: " + ("ON" if now else "OFF"))
        return await event.edit(settings_text(), buttons=kb_settings())
    if data == "set:warmpath":
        now = store.toggle_warmpath()
        await event.answer("Warm Path: " + ("ON" if now else "OFF"))
        return await event.edit(settings_text(), buttons=kb_settings())
    if data == "set:pool":
        return await event.edit(
            cards.pool_card(session_pool.status()),
            buttons=[[Button.inline("🧹 Close standby sessions", b"set:poolclose")],
                     [Button.inline("⬅ Back", b"menu:settings")]])
    if data == "set:poolclose":
        n = await session_pool.close_all()
        await event.answer(f"Closed {n} standby session(s).")
        return await event.edit(
            cards.pool_card(session_pool.status()),
            buttons=[[Button.inline("⬅ Back", b"menu:settings")]])
    if data == "set:concurrency":
        pending[event.sender_id] = {"step": "await_concurrency"}
        return await event.edit(
            cards.card("⚡ SEND CONCURRENCY",
                       [("Current", f"{store.send_concurrency} at a time")],
                       footer="How many recipients may be in flight at once on the fast "
                              "path (1-10). 1 is the proven sequential behaviour. Try 3 "
                              "first and watch for limit cards before going higher. The "
                              "UI fallback always stays sequential."),
            buttons=kb_back())
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
        from eitaa import warmpath
        from eitaa.driver import EitaaDriver
        try:
            # Warm Path borrows a standby session so this button stops launching a
            # second Chromium outside the pool's max_open ceiling. With the engine
            # off it opens its own session exactly as before.
            async with (session_pool.lease(acc, headed=config.HEADED_JOBS)
                        if warmpath.use_pool() else open_session(acc)) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if not await driver.is_logged_in():
                    await report(cards.error_card("stats", acc, code="not_logged_in",
                                                  detail="account is not logged in"))
                    return
                s = await driver.bridge_stats(with_pvs=warmpath.stats_with_pvs())
                if s is None:
                    s = await driver.get_stats()
                # bridge_stats reports pvs=-1 when the 98-second getDialogs paging
                # was skipped. Storing that would erase the last real count, so
                # only a measured value is written.
                pvs_measured = s.get("pvs")
                if not (isinstance(pvs_measured, int) and pvs_measured >= 0):
                    pvs_measured = None
                store.set_account_meta(acc, contacts=s.get("contacts"), pvs=pvs_measured)
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
                "None of the selected accounts have saved peers. Tap "
                "'🔄 Update Contacts' on each first.", alert=True)

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
        # A live stage card, because opening Chromium alone takes minutes here
        # and the login used to be silent until the code arrived.
        started = await manager.start_bridge_login(
            name, phone, report, live=LiveCard(config.report_to()))
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

    if step == "await_concurrency":
        try:
            val = int(re.sub(r"\D", "", text or ""))
        except ValueError:
            return await event.respond("Send a whole number between 1 and 10.")
        if not 1 <= val <= 10:
            return await event.respond("Send a whole number between 1 and 10.")
        store.set_setting("send_concurrency", val)
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
