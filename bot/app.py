"""Telegram control panel (Telethon) for the Mkwlsoso Eitaa manager.

Owner-only. English ReconBot-style panel. Wires the existing EitaaDriver
operations (send text/file, range contact creation, stats) behind inline
buttons, and posts English log cards for every run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

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


# ---- keyboards ---------------------------------------------------------

def kb_home():
    return [
        [Button.inline("👤 Accounts", b"menu:accounts"),
         Button.inline("📝 Content", b"menu:content")],
        [Button.inline("📤 Send", b"menu:send"),
         Button.inline("➕ Contacts", b"menu:contacts")],
        [Button.inline("⚙ Settings", b"menu:settings"),
         Button.inline("📊 Stats", b"menu:stats")],
        [Button.inline("❓ Help", b"menu:help")],
    ]


def kb_back():
    return [[Button.inline("⬅ Back", b"menu:home")]]


def kb_accounts():
    rows = []
    active = store.active_account
    for acc in list_accounts():
        mark = "✅ " if acc == active else ""
        rows.append([Button.inline(f"{mark}{acc}", f"acc:select:{acc}".encode())])
    rows.append([Button.inline("➕ Add Account", b"acc:add")])
    rows.append([Button.inline("⬅ Back", b"menu:home")])
    return rows


def kb_content():
    return [
        [Button.inline("📝 Set Text", b"content:text"),
         Button.inline("📎 Set File", b"content:file")],
        [Button.inline("🗑 Clear", b"content:clear")],
        [Button.inline("⬅ Back", b"menu:home")],
    ]


def kb_settings():
    return [
        [Button.inline("⏱ Text Send Delay", b"set:textdelay")],
        [Button.inline("⏱ Contact Create Delay", b"set:contactdelay")],
        [Button.inline("🔢 Log Every N", b"set:logevery")],
        [Button.inline("⬅ Back", b"menu:home")],
    ]


def kb_send():
    rows = [[Button.inline("▶ Start Send", b"send:start")]]
    active = store.active_account
    if active and manager.is_busy(active):
        rows.append([Button.inline("⏹ Stop", b"send:stop")])
    rows.append([Button.inline("⬅ Back", b"menu:home")])
    return rows


def kb_contacts():
    rows = [[Button.inline("▶ Build From Range", b"contacts:start")]]
    active = store.active_account
    if active and manager.is_busy(active):
        rows.append([Button.inline("⏹ Stop", b"contacts:stop")])
    rows.append([Button.inline("⬅ Back", b"menu:home")])
    return rows


# ---- panel renderers ---------------------------------------------------

def home_text() -> str:
    return cards.panel_home(config.BOT_VERSION, len(list_accounts()), store.active_account)


def accounts_text() -> str:
    accs = list_accounts()
    return cards.card(
        "👤 ACCOUNTS",
        [("Total ", len(accs)), ("Active", store.active_account or "none")],
        footer="Select an account to make it active." if accs else "No accounts yet. Use Add Account.",
    )


def content_text() -> str:
    return cards.card("📝 CONTENT", [("Current", store.content_summary())],
                      footer="Set the text or file the bot will send.")


def settings_text() -> str:
    return cards.card(
        "⚙ SETTINGS",
        [
            ("Text send delay   ", f"{store.text_send_delay:g}s"),
            ("Contact create delay", f"{store.contact_create_delay:g}s"),
            ("Log every         ", store.send_log_every),
        ],
    )


# ---- command + callback handlers ---------------------------------------

async def show_home(event, edit=False):
    if edit:
        await event.edit(home_text(), buttons=kb_home())
    else:
        await event.respond(home_text(), buttons=kb_home())


@bot.on(events.NewMessage(pattern=r"^/start"))
async def _start(event):
    print(f"[bot] /start received: sender_id={event.sender_id} owner={config.OWNER_ID}", flush=True)
    if not is_owner(event):
        print("[bot] /start ignored: sender is not the configured OWNER_ID", flush=True)
        try:
            await event.respond("This panel is private.")
        except Exception:  # noqa: BLE001
            pass
        return
    pending.pop(event.sender_id, None)
    try:
        await show_home(event)
        print("[bot] home panel sent", flush=True)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[bot] failed to send home panel: {exc}", flush=True)


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
        await report(cards.error_card("panel_callback", code=type(exc).__name__, detail=str(exc)))
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
    if data == "menu:send":
        return await event.edit(send_text_panel(), buttons=kb_send())
    if data == "menu:contacts":
        return await event.edit(contacts_panel(), buttons=kb_contacts())
    if data == "menu:stats":
        return await _do_stats(event)
    if data == "menu:help":
        return await event.edit(help_text(), buttons=kb_back())

    # accounts
    if data.startswith("acc:select:"):
        acc = data.split(":", 2)[2]
        store.set_active_account(acc)
        await event.answer(f"Active: {acc}")
        return await event.edit(accounts_text(), buttons=kb_accounts())
    if data == "acc:add":
        return await event.edit(
            cards.card("➕ ADD ACCOUNT",
                       [("Status", "auto-login coming next")],
                       footer="Auto-login (phone + code) is the next build phase. "
                              "For now, log in with the CLI: python cli.py login --account <name>"),
            buttons=kb_back())

    # content
    if data == "content:text":
        pending[event.sender_id] = {"step": "await_text"}
        return await event.edit(
            cards.card("📝 SET TEXT", footer="Send me the text to store."),
            buttons=kb_back())
    if data == "content:file":
        pending[event.sender_id] = {"step": "await_file"}
        return await event.edit(
            cards.card("📎 SET FILE",
                       footer="Upload the file (any type: apk, zip, ...). "
                              "Add a caption to the upload to store it too."),
            buttons=kb_back())
    if data == "content:clear":
        store.clear_content()
        await event.answer("Content cleared.")
        return await event.edit(content_text(), buttons=kb_content())

    # settings
    if data == "set:textdelay":
        pending[event.sender_id] = {"step": "await_textdelay"}
        return await event.edit(
            cards.card("⏱ TEXT SEND DELAY", [("Current", f"{store.text_send_delay:g}s")],
                       footer="Send a number of seconds (e.g. 8)."), buttons=kb_back())
    if data == "set:contactdelay":
        pending[event.sender_id] = {"step": "await_contactdelay"}
        return await event.edit(
            cards.card("⏱ CONTACT CREATE DELAY", [("Current", f"{store.contact_create_delay:g}s")],
                       footer="Send a number of seconds (e.g. 3)."), buttons=kb_back())
    if data == "set:logevery":
        pending[event.sender_id] = {"step": "await_logevery"}
        return await event.edit(
            cards.card("🔢 LOG EVERY N", [("Current", store.send_log_every)],
                       footer="Send how many sends per progress card (e.g. 50)."),
            buttons=kb_back())

    # send
    if data == "send:start":
        return await _start_send(event)
    if data == "send:stop":
        if active:
            manager.stop_account(active)
            await event.answer("Stop requested.")
        return await event.edit(send_text_panel(), buttons=kb_send())

    # contacts
    if data == "contacts:start":
        if not active:
            return await event.answer("Select an account first.", alert=True)
        pending[event.sender_id] = {"step": "await_prefix"}
        return await event.edit(
            cards.card("➕ CONTACT RANGE",
                       footer="Send a mobile prefix, e.g. 091646. "
                              "The bot fills the rest and creates contacts."),
            buttons=kb_back())
    if data == "contacts:stop":
        if active:
            manager.stop_account(active)
            await event.answer("Stop requested.")
        return await event.edit(contacts_panel(), buttons=kb_contacts())

    await event.answer()


def send_text_panel() -> str:
    active = store.active_account
    busy = active and manager.is_busy(active)
    return cards.card(
        "📤 SEND",
        [
            ("Account", active or "none"),
            ("Content", store.content_summary()),
            ("Delay  ", f"{store.text_send_delay:g}s"),
            ("Status ", "running" if busy else "idle"),
        ],
        footer="Sends the stored content to ALL contacts of the active account.",
    )


def contacts_panel() -> str:
    active = store.active_account
    busy = active and manager.is_busy(active)
    return cards.card(
        "➕ CONTACTS",
        [
            ("Account", active or "none"),
            ("Delay  ", f"{store.contact_create_delay:g}s"),
            ("Status ", "running" if busy else "idle"),
        ],
        footer="Create contacts from a number range (prefix like 091646).",
    )


def help_text() -> str:
    return cards.card(
        "❓ HELP",
        body=(
            "Accounts: pick the active Eitaa account.\n"
            "Content: set the text or file to send.\n"
            "Send: broadcast the content to all contacts.\n"
            "Contacts: create contacts from a number prefix.\n"
            "Settings: send speed, contact speed, log frequency.\n"
            "Stats: contacts + private chats of the active account.\n"
            "You get English log cards for start, every N sends, and finish; "
            "errors and limit detection post their own cards."
        ),
    )


async def _do_stats(event):
    active = store.active_account
    if not active:
        return await event.answer("Select an account first.", alert=True)
    if manager.is_busy(active):
        return await event.answer("Account is busy with a running job.", alert=True)
    await event.edit(cards.card("📊 STATS", [("Account", active)],
                                footer="Counting... this scrolls contacts + chats."),
                     buttons=kb_back())

    async def _run():
        from capture.browser import open_session
        from eitaa.driver import EitaaDriver
        try:
            async with open_session(active) as session:
                driver = EitaaDriver(session)
                await driver.open()
                if not await driver.is_logged_in():
                    await report(cards.error_card("stats", active, code="not_logged_in",
                                                  detail="account is not logged in"))
                    return
                s = await driver.get_stats()
                await report(cards.card(
                    "📊 STATS",
                    [("Account", active),
                     ("Contacts", s.get("contacts")),
                     ("Private chats", s.get("pvs"))]))
        except Exception as exc:  # noqa: BLE001
            await report(cards.error_card("stats", active, code=type(exc).__name__, detail=str(exc)))

    asyncio.create_task(_run())
    await event.answer("Stats job started; result will arrive as a card.")


async def _start_send(event):
    active = store.active_account
    if not active:
        return await event.answer("Select an account first.", alert=True)
    if store.content.get("kind") not in ("text", "file"):
        return await event.answer("Set content first (Content menu).", alert=True)
    if manager.is_busy(active):
        return await event.answer("Account already has a running job.", alert=True)
    await manager.run_send(active, dict(store.content), dict(store.settings), report)
    await event.answer("Send job started.")
    await event.edit(send_text_panel(), buttons=kb_send())


async def _start_contacts(event_sender_id, prefix: str, count: int):
    active = store.active_account
    await manager.run_contacts(active, prefix, count, dict(store.settings), report)


@bot.on(events.NewMessage)
async def _conversation(event):
    print(f"[bot] message: sender_id={event.sender_id} text={ (event.raw_text or '')[:40]!r} file={bool(event.message.file)}", flush=True)
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
            cards.card("➕ CONTACT RANGE", [("Prefix", text)],
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
        await manager.run_contacts(active, prefix, count, dict(store.settings), report)
        return await event.respond(
            cards.card("➕ CONTACT BUILD QUEUED",
                       [("Account", active), ("Prefix", prefix), ("Count", count)],
                       footer="Live progress cards will follow."),
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
    print("[bot] Send /start to the bot as the owner. Watching for messages...", flush=True)
    bot.run_until_disconnected()


if __name__ == "__main__":
    main()
