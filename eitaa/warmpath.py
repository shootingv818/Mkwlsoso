"""Warm Path engine (isolated, opt-in): stop re-booting the Eitaa web app.

WHY
---
`EitaaDriver.open()` calls `session.goto()` unconditionally, so every job
navigates to web.eitaa.com again -- even when the pooled standby session is
already sitting on the chat list. A navigation is not just a download: the SPA
re-runs its own startup sync (help.getConfig, updates.getState,
updates.getDifference, messages.getDialogs, ...) before the bot's own single
`contacts.getContacts` ever runs. Measured on this host, one owner sitting
(add account -> build contacts -> update contacts -> refresh -> send) pays FIVE
full boots where one is enough, plus a hard 4s wait each time.

WHAT IT DOES
------------
When the page is already booted, the reload is skipped and two things the reload
used to provide silently are supplied explicitly instead:

1. A CLEAN LEFT COLUMN. The UI contact-add flow deliberately finishes on the
   Contacts view (`runner.py` -> `driver._reset_to_contacts_view()`), and
   `is_logged_in()` cannot tell that view from the chat list because both match
   the broad `selectors.SEARCH_INPUT` list. Skipping the reload without fixing
   the view makes a send type the recipient name into the CONTACTS search box.
   `driver._return_to_chat_list()` (already in the driver) restores it with
   keyboard/Escape only -- zero server requests.

2. WORKER TRAFFIC FOR THE BROWSER-FREE ENGINE. `direct_ctx.refresh_from_driver`
   rebuilds the direct/hybrid session context from `__MKWL_workerDump()`, which
   DRAINS its buffer on read (see eitaa/worker_capture.js). The boot used to
   refill it. A send whose recipients come from the contacts cache makes no API
   call at all before `_pick_transport`, so with no boot the dump is empty and
   the engine silently degrades to the bridge. One existing call --
   `driver.bridge_stats(with_pvs=False)` -> `contacts.getContacts` -- refills it
   for a single request instead of a whole app boot.

SAFETY / ROLLBACK
-----------------
OFF by default. Every entry point is a question the caller asks BEFORE doing
anything: when this engine is off (or anything at all goes wrong) the answer
sends the caller down its original code path, unchanged. Nothing here replaces
existing logic; it only ever declines to run.

  * `open_page(driver)`   -> True  = the page is ready, skip the goto
                             False = do the original goto + 4s wait
  * `use_pool()`          -> True  = lease a standby session for the stats
                             refresh instead of launching a private browser
  * `stats_with_pvs()`    -> the `with_pvs` value to pass to `bridge_stats`

Toggle: Settings -> "Warm Path", or MKWL_WARMPATH=1.
"""

from __future__ import annotations

from config import config
from eitaa import selectors as S


def enabled() -> bool:
    """True when the owner turned Warm Path on.

    The persisted panel setting wins; `config.WARMPATH` (MKWL_WARMPATH) is the
    default for a fresh install. Never raises: if the panel state cannot be read
    the engine reports OFF, which means every caller keeps its old behaviour.
    """
    try:
        from bot.store import store as _store
        return bool(_store.warmpath)
    except Exception:  # noqa: BLE001 - unreadable state must not enable anything
        return bool(config.WARMPATH)


def use_pool() -> bool:
    """Whether the stats refresh should borrow a standby session.

    `bot/app.py` opens its own private browser for the Refresh button, which
    bypasses the pool's `max_open` ceiling -- two Chromium processes can be alive
    at once on a 961 MB host while the pool believes there is one.
    """
    return enabled()


def stats_with_pvs() -> bool:
    """The `with_pvs` argument for `driver.bridge_stats()`.

    The PV count pages `messages.getDialogs` (up to 80 requests, measured live at
    98 SECONDS) and the panel only shows it as one cosmetic row. `runner.py`
    already passes False on the login path; this makes the Refresh button agree.
    """
    return not enabled()


def _engine_needs_context() -> bool:
    """True when the browser-free engine is selected, so the worker dump matters."""
    try:
        from bot.store import store as _store
        return str(_store.engine) != "bridge"
    except Exception:  # noqa: BLE001
        return False


async def _warm_up(driver) -> None:
    """Put one MTProto envelope in the worker dump, the cheap way.

    Only for the browser-free engine: the bridge engine never reads the dump, so
    the bridge path pays nothing at all here.
    """
    if not _engine_needs_context():
        return
    try:
        await driver.bridge_stats(with_pvs=False)
    except Exception:  # noqa: BLE001 - a failed warm-up only costs freshness
        pass


async def _subview_open(driver) -> bool:
    """True when a left-column subview is covering the chat list.

    The back/close control is the signal, NOT `selectors.SEARCH_INPUT`: the
    Contacts view carries its own search box, so a search input proves nothing
    about which view is on screen. This is the same blind spot that makes
    `driver._return_to_chat_list()` return early while the Contacts view is still
    up, so it is checked here before handing over to it.
    """
    for sel in S.SUBVIEW_CLOSE:
        try:
            loc = driver.page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _dismiss_subviews(driver) -> bool:
    """Close whatever is stacked over the chat list, keyboard/click only.

    Returns True when the chat list is the root view again. Costs no server
    requests -- that is the whole point of not reloading.
    """
    for _ in range(4):
        if not await _subview_open(driver):
            return True
        await driver._return_to_chat_list()
        if not await _subview_open(driver):
            return True
        try:
            await driver.page.keyboard.press("Escape")
            await driver.page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            break
    return not await _subview_open(driver)


async def open_page(driver) -> bool:
    """Try to make the page job-ready WITHOUT navigating.

    Returns True only when the page is booted, logged in and showing the chat
    list. Any doubt returns False, and the caller then runs its original
    `session.goto()` -- so a failure here is never worse than today.
    """
    if not enabled():
        return False
    try:
        page = getattr(driver, "page", None)
        if page is None:
            return False
        url = str(getattr(page, "url", "") or "")
        if not url.startswith(config.EITAA_WEB_URL):
            return False  # cold or foreign page: it really does need a boot

        # A previous job may have left a subview or popup on screen. The UI
        # contact-add flow finishes on the Contacts view on purpose, and a send
        # that starts there types the recipient into the wrong search box.
        if not await _dismiss_subviews(driver):
            return False
        if not await driver.is_logged_in():
            # The UI could not be recovered by keyboard alone; a reload is the
            # honest answer, exactly as before this engine existed.
            return False

        await _warm_up(driver)
        return True
    except Exception as exc:  # noqa: BLE001 - never break a job to save a reload
        print(f"[warmpath] skipped ({type(exc).__name__}: {exc}); "
              f"using the normal page load", flush=True)
        return False
