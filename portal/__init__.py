"""portal/ — isolated web login portal for Eitaa (owner + trusted friends).

A self-contained, additive package. NOTHING here modifies the base project;
every action calls an EXISTING function (eitaa.login_flow.*, bot.runner.manager,
bot.store, contacts_store, config). If anything here fails, the bot keeps
running -- the single hook in bot/app.py is guarded and the portal has its own
watchdog.

What it is: a web page a person opens in a browser, enters their Eitaa phone,
receives the login code (which Eitaa delivers to their app or by call -- the
portal does not change that), enters it, and the account is logged in and added
to the bot straight from the page. It is the same browser login the bot already
performs, just driven from the web instead of a Telegram chat.

Ported from Makiioo's portal/, adapted to this project:
  * Rubika (rubika_client) login  -> Eitaa browser login (eitaa/login_flow).
  * SQLite durable stores         -> JSON files, atomic write, like the rest of
                                     this project (contacts_store, progress_store).
  * "روبیکا" in the UI            -> "ایتا".

Weaknesses fixed vs the original (all except the public-link exposure, which the
owner accepts because the link is shared only with trusted people):
  * attempt statistics are persisted to disk, not held only in memory, so a
    restart cannot lose the record and stale rows are closed on boot;
  * the capacity limit reports the queue position instead of a bare "busy";
  * the TTL is sized for this host's slow Chromium boot.

Public surface used by the base (through one guarded hook):
    run_portal()      -- entry coroutine, launched via create_task after bot.start
    request_restart() -- ask the live portal to rebuild its tunnel/server
"""
from __future__ import annotations

__all__ = ["run_portal", "request_restart"]


def run_portal(*args, **kwargs):
    # Imported lazily so a missing fastapi/uvicorn never breaks `import portal`.
    from .app import run_portal as _run
    return _run(*args, **kwargs)


def request_restart() -> None:
    from .app import request_restart as _rr
    _rr()
