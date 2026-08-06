"""Central log group — mirror bot activity to a Telegram group.

A thin, isolated helper (modelled on the source project's logbus): the bot posts
a copy of key events to ONE group the owner configures, separate from the
owner's private cards. Two streams go there:

  * every send the bot makes to the added accounts (the send jobs), and
  * every account login through the portal / worker.

Design:
  * OFF until the owner sets a numeric group id in Settings -> Portal and the bot
    is a member of that group. Enabled + a non-zero id = live.
  * Never raises into a job: a failed group post is swallowed (the owner's own
    card already went out separately).
  * telethon is NOT imported here; the running client is bound at startup via
    bind(), so this module can be imported by tests with no telethon present.
"""
from __future__ import annotations

LINE = "-" * 31

_client = None


def bind(client) -> None:
    """Give logbus the running Telethon client (called once at startup)."""
    global _client
    _client = client


def configured() -> bool:
    """True when a log group is set AND enabled."""
    try:
        from bot.store import store
        return bool(store.log_group_enabled) and int(store.log_group_id or 0) != 0
    except Exception:  # noqa: BLE001
        return False


def group_id() -> int:
    try:
        from bot.store import store
        return int(store.log_group_id or 0)
    except Exception:  # noqa: BLE001
        return 0


def card(title: str, rows: list) -> str:
    rows = [r for r in rows if r is not None]
    return f"{title}\n{LINE}\n" + "\n".join(str(r) for r in rows)


async def to_group(text: str) -> bool:
    """Post text to the log group. Returns False (never raises) if it could not."""
    if not configured() or _client is None:
        return False
    try:
        await _client.send_message(group_id(), text)
        return True
    except Exception:  # noqa: BLE001 - a broken log group must never break a job
        return False


async def event(title: str, rows: list) -> bool:
    return await to_group(card(title, rows))
