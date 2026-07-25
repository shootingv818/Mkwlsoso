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
from bot.store import store
from bot.runner import manager, expand_range


# ---- helpers -----------------------------------------------------------

def list_accounts() -> list[str]:
    d = config.PROFILES_DIR
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


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
    return [
        [Button.inline("👤 Accounts", b"menu:accounts"),
         Button.inline("➕ Add Account", b"acc:add")],
        [Button.inline("📝 Content", b"menu:content"),
         Button.inline("⚙ Settings", b"menu:settings")],
    ]


def kb_back():
    return [[Button.inline("⬅ Back", b"menu:home")]]


def kb_accounts():
    rows = []
    active = store.active_account
    for acc in list_accounts():
        mark = "🟢 " if acc == active else "• "
        label = store.account_phone(acc)
        rows.append([Button.inline(f"{mark}{label}", f"acc:open:{acc}".encode())])
    rows.append([Button.inline("➕ Add Account", b"acc:add"),
                 Button.inline("⬅ Back", b"menu:home")])
    return rows


def kb_account_panel(acc: str):
    busy = manager.is_busy(acc)
    rows = [
        [Button.inline("📤 Send", b"pnl:send"),
         Button.inline("➕ Build Contacts", b"pnl:contacts")],
        [Button.inline("🔄 Refresh", b"pnl:refresh")],
    ]
    if busy:
        rows.append([Button.inline("⏹ Stop", b"pnl:stop")])
    rows.append([Button.inline("👤 Accounts", b"menu:accounts"),
                 Button.inline("⬅ Home", b"menu:home")])
    return rows


def kb_content():
    return [
        [Button.inline("📝 Set Text", b"content:text"),
         Button.inline("📎 Set File", b"content:file")],
        [Button.inline("🗑 Clear", b"content:clear")],
        [Button.inline("⬅ Back", b"menu:home")],
    ]


def kb_settings():
    eng = store.engine
    other = "direct ⚡" if eng == "bridge" else "bridge 🌉"
    return [
        [Button.inline(f"🔧 Engine: {eng}  →  switch to {other}", b"set:engine")],
        [Button.inline("⏱ Text Send Delay", b"set:textdelay")],
        [Button.inline("⏱ Contact Create Delay", b"set:contactdelay")],
        [Button.inline("🔢 Log Every N", b"set:logevery")],
        [Button.inline("⬅ Back", b"menu:home")],
    ]


# ---- panel renderers ---------------------------------------------------

async def home_text() -> str:
    ping = await server_ping_ms()
    return cards.panel_home(config.BOT_VERSION, len(list_accounts()),
                            store.active_account, engine=store.engine, ping_ms=ping)


def accounts_text() -> str:
    accs = list_accounts()
    return cards.card(
        "👤 ACCOUNTS",
        [("Total ", len(accs)), ("Active", store.account_phone(store.active_account)
                                  if store.active_account else "none")],
        footer=("Tap an account's number to open its panel (send + build contacts)."
                if accs else "No accounts yet. Use ➕ Add Account."),
    )


def account_panel_text(acc: str) -> str:
    meta = store.account_meta(acc)
    return cards.account_panel(
        acc, store.account_phone(acc),
        meta.get("contacts"), meta.get("pvs"),
        store.engine, manager.is_busy(acc),
    )


def content_text() -> str:
    return cards.card("📝 CONTENT", [("Current", store.content_summary())],
                      footer="Set the text or file the bot will send.")


def settings_text() -> str:
    eng = store.engine
    return cards.card(
        "⚙ SETTINGS",
        [
            ("Engine            ", "🌉 bridge (browser)" if eng == "bridge"
             else "⚡ direct (no browser)"),
            ("Text send delay   ", f"{store.text_send_delay:g}s"),
            ("Contact create delay", f"{store.contact_create_delay:g}s"),
            ("Log every         ", store.send_log_every),
        ],
        footer="Engine governs contact building (direct = no browser). "
               "Sending uses the bridge fast-path.",
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
    if data == "menu:accounts":
        return await event.edit(accounts_text(), buttons=kb_accounts())
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
        pending[event.sender_id] = {"step": "login_name"}
        return await event.edit(
            cards.card("➕ ADD ACCOUNT",
                       footer="Send a short name for the account (letters, numbers, "
                              "underscore — e.g. acc2). Then I'll ask for the phone and "
                              "the login code, right here. No noVNC needed."),
            buttons=kb_back())

    # ---- per-account panel actions (operate on active) ----
    if data == "pnl:refresh":
        if not active:
            return await event.answer("No active account.", alert=True)
        return await _refresh_account(event, active)
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
            manager.stop_account(active)
            await event.answer("Stop requested.")
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
                    store.engine, False))
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

    if step == "login_name":
        name = text
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", name or ""):
            return await event.respond(
                "Send a valid name: letters, numbers, underscore (max 32). e.g. acc2")
        if manager.is_busy(name):
            return await event.respond("That account already has a running job. Try again later.")
        st["login_account"] = name
        st["step"] = "login_phone"
        return await event.respond(
            cards.card("➕ ADD ACCOUNT", [("Account", name)],
                       footer="Now send the phone number (e.g. 0930... or 98930...)."),
            buttons=kb_back())

    if step == "login_phone":
        phone = re.sub(r"[^\d+]", "", text or "")
        if len(re.sub(r"\D", "", phone)) < 10:
            return await event.respond("Send a valid phone number (e.g. 09304683887).")
        name = st.get("login_account")
        if not name:
            pending.pop(event.sender_id, None)
            return await event.respond("Lost the account name; tap Add Account again.")
        started = await manager.start_bridge_login(name, phone, report)
        if not started:
            pending.pop(event.sender_id, None)
            return await event.respond("That account is busy right now. Try again later.")
        st["step"] = "login_code"
        return await event.respond(
            cards.card("➕ ADD ACCOUNT", [("Account", name), ("Status", "requesting code…")],
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
