"""The session check itself: open the account's page, ask if it is still logged in.

Nothing is sent to anybody and nothing is collected. The check answers the one
question a campaign silently assumed until now: *is this account's session still
good?* Every job in `bot/runner.py` already begins with `driver.is_logged_in()`
and aborts with a `not_logged_in` error card when it fails - but you only found
that out by starting a real run. This runs exactly that gate on its own.

Three signals are reported, weakest to strongest:

  1. `is_logged_in()`   - the logged-in UI is present (driver.py's heuristic).
  2. `self_peer_id()`   - the app can name the account's own peer, so the session
                          is genuinely authorized, not just a restored shell.
  3. `bridge_stats()`   - a real API round trip answered. `with_pvs=False`
                          because the private-chat count pages through
                          messages.getDialogs and was measured at 98s, while the
                          contacts number takes ~4s.

A session can pass (1) and fail (3) - that is a meaningful state (the app is up
but the server is not answering this account), so it gets its own card title
instead of being flattened into "ok" or "error".
"""

from __future__ import annotations

import os
import time

from config import config
from bot import cards
from bot import contacts_store
from bot import direct_ctx
from capture.pool import pool as session_pool
from eitaa.driver import EitaaDriver

# How long the app may take to show its logged-in UI before the session is
# called dead. Defaults to the project's own login settle timeout, because a
# false "not logged in" on this slow host (Chromium alone was measured at
# 158-203s) would be worse than a check that occasionally takes two minutes.
_CHECK_TIMEOUT_ENV = os.environ.get("MKWL_SESSION_CHECK_TIMEOUT")


async def check_session(account: str, phone: str, report, live=None,
                        engine: str = "bridge") -> dict:
    """Check one account's session and post a result card. Returns a summary.

    Raises on an unexpected failure so the caller in `bot/runner.py` posts the
    single error card, exactly like every other job there.
    """
    # Imported here, not at module import time: `bot.runner` is what imports this
    # package, so a top-level import would be circular. By the time this runs,
    # `bot.runner` is fully loaded.
    from bot.runner import (StageTracker, manager, _worker_capture_script,
                            _LOGIN_SETTLE_TIMEOUT)

    timeout = float(_CHECK_TIMEOUT_ENV) if _CHECK_TIMEOUT_ENV else _LOGIN_SETTLE_TIMEOUT
    start = time.time()
    stages = StageTracker(live, phone, [
        ("browser", "open browser"),
        ("login", "ask Eitaa if the session is alive"),
        ("probe", "confirm it over the API"),
    ]) if live is not None else None

    summary: dict = {"account": account, "logged_in": False, "api": False,
                     "peer_id": None, "contacts": None}
    try:
        if stages is not None:
            await stages.begin("browser", "Read-only check: nothing is sent to "
                                          "anybody and no contacts are collected.")
        # Same lease every job uses, so the check reuses a warm session when one
        # is on standby and arms the browser-free capture when that engine is on.
        async with session_pool.lease(
                account, headed=config.HEADED_JOBS,
                init_script_path=_worker_capture_script(engine)) as session:
            driver = EitaaDriver(session)
            await driver.open()

            if stages is not None:
                await stages.begin("login")
            alive = await driver.is_logged_in()
            if not alive:
                # One 8s selector poll is how a job decides, and on this host a
                # slow boot looks identical to a dead session. Give the app the
                # same grace period (and the one page reload) a login gets before
                # reporting the session as gone.
                alive = await manager._wait_logged_in(driver, session, None, timeout)
            summary["logged_in"] = bool(alive)

            if not alive:
                if stages is not None:
                    await stages.fail("This account is not logged in.")
                await report(cards.error_card(
                    "session_check", account, code="not_logged_in", engine=engine,
                    phase="login",
                    detail=f"the app never showed the logged-in UI within "
                           f"{timeout:.0f}s - log in again with ➕ Add Account"))
                return summary

            if stages is not None:
                await stages.begin("probe")
            summary["peer_id"] = await driver.self_peer_id()
            stats = await driver.bridge_stats(with_pvs=False)
            summary["api"] = stats is not None
            contacts = (stats or {}).get("contacts")
            if isinstance(contacts, int) and contacts >= 0:
                summary["contacts"] = contacts
                # The panel already caches this number; refreshing it here means
                # the account card is up to date for free.
                try:
                    from bot.store import store
                    store.set_account_meta(account, contacts=contacts)
                except Exception:  # noqa: BLE001 - a stale label must never fail a check
                    pass

            summary["seconds"] = round(time.time() - start, 2)
            if stages is not None:
                await stages.done("Session is alive." if summary["api"] else
                                  "Logged in, but the API did not answer.")
            await report(result_card(account, phone, engine, summary, timeout))
        return summary
    except Exception:
        if stages is not None:
            await stages.fail("The check itself failed - see the error card.")
        raise
    finally:
        if stages is not None:
            stages.stop()
        flush = getattr(live, "flush", None)
        if flush is not None:
            try:
                await flush()
            except Exception:  # noqa: BLE001
                pass


def result_card(account: str, phone: str, engine: str, summary: dict,
                timeout: float) -> str:
    """The success/partial card, built with the project's own card shell."""
    api_ok = bool(summary.get("api"))
    peer = summary.get("peer_id")
    contacts = summary.get("contacts")

    saved = contacts_store.count(account)
    saved_age = contacts_store.age_hours(account)
    ctx_age = direct_ctx.newest_capture_age_hours(account)
    ctx_ready = direct_ctx.has_context(account)

    if api_ok and peer:
        title = "✅ SESSION OK"
        footer = ("This account is logged in and Eitaa answered it, so a send or "
                  "contact build can use it right now.")
    elif api_ok:
        title = "✅ SESSION OK"
        footer = ("Logged in and the API answered. The account's own peer could "
                  "not be read from the page, which only affects 🧪 Test to Me "
                  "on the bridge engine.")
    else:
        title = "⚠️ LOGGED IN, API SILENT"
        footer = ("The logged-in UI is there but the API call got no answer. That "
                  "is usually a temporary network or rate-limit issue - check "
                  "again in a few minutes before assuming the session is gone.")

    return cards.card(
        title,
        [
            ("Phone       ", phone),
            ("Engine      ", engine),
            ("Login       ", "🟢 logged in"),
            ("API answer  ", "🟢 answered" if api_ok else "🟡 no answer"),
            ("Own peer    ", peer or "not resolved"),
            ("Contacts    ", f"{contacts:,} on Eitaa"
                             if isinstance(contacts, int) else None),
            ("Saved       ", (f"{saved:,} cached"
                              + (f" · {saved_age:.0f}h old"
                                 if saved_age is not None else ""))
                             if saved else "none cached yet"),
            ("Browser-free", ("ready"
                              + (f" · captured {ctx_age:.0f}h ago"
                                 if ctx_age is not None else ""))
                             if ctx_ready else "no capture yet"),
            ("Checked in  ", cards.fmt_duration(summary.get("seconds") or 0)),
            ("Time        ", cards.now_hms()),
        ],
        footer=footer,
    )
