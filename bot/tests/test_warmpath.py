"""Tests for the Warm Path engine (eitaa/warmpath.py).

Run: python -m bot.tests.test_warmpath

Two promises are checked here, and the first one matters most:

  1. OFF is the old bot. Every hook must send the caller down its original code
     path, so a broken engine is switched off and nothing else changes.
  2. ON is safe. The reload is only skipped when the page is genuinely usable,
     the left column is restored first (or the reload happens anyway), and the
     browser-free engine still gets its worker traffic.

No browser, no network: a fake driver records what the engine asked it to do.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_warmpath_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")


def _stub_playwright() -> None:
    try:
        import playwright.async_api  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    pkg = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")
    for name in ("BrowserContext", "CDPSession", "Page", "Locator", "Error"):
        setattr(api, name, type(name, (object,), {}))
    api.TimeoutError = type("TimeoutError", (Exception,), {})
    api.async_playwright = lambda: None
    pkg.async_api = api
    sys.modules.setdefault("playwright", pkg)
    sys.modules.setdefault("playwright.async_api", api)


_stub_playwright()

from config import config  # noqa: E402
from bot.store import store  # noqa: E402
from eitaa import selectors, warmpath  # noqa: E402
from eitaa.driver import EitaaDriver  # noqa: E402

EITAA = config.EITAA_WEB_URL
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


# ---- fakes -----------------------------------------------------------------

# Which selectors are on screen in each left-column state. The Contacts view
# has its OWN search box and so satisfies selectors.SEARCH_INPUT too -- that is
# precisely why the reload cannot simply be dropped.
_SEARCH_VISIBLE = {"chat_list", "contacts_view"}
_CLOSE_BUTTONS = tuple(selectors.SUBVIEW_CLOSE)


class FakeLocator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self._page = page
        self._sel = selector

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if await self.is_visible() else 0

    async def is_visible(self):
        # A subview shows its back/close control; the chat list root does not.
        if self._sel in _CLOSE_BUTTONS:
            return self._page.view in ("contacts_view", "popup")
        # The Contacts view has a search box of its own, so a search input is
        # visible there too. That ambiguity is what the engine must survive.
        if "input" in self._sel:
            return self._page.view in _SEARCH_VISIBLE
        return False

    async def click(self):
        self._page.clicks += 1
        if self._page.recoverable:
            self._page.view = "chat_list"


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    async def press(self, key: str):
        self._page.key_presses += 1
        if key == "Escape" and self._page.recoverable:
            self._page.view = "chat_list"


class FakePage:
    """Minimal Playwright surface: enough for the driver's real
    `_return_to_chat_list()` and `is_logged_in()` to run unmodified."""

    def __init__(self, url: str = "about:blank", view: str = "blank",
                 recoverable: bool = True) -> None:
        self.url = url
        self.view = view            # "blank" | "chat_list" | "contacts_view" | "popup"
        self.recoverable = recoverable
        self.navigations = 0
        self.waited_ms = 0
        self.clicks = 0
        self.key_presses = 0
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector: str):
        return FakeLocator(self, selector)

    async def goto(self, url, wait_until=None):
        self.navigations += 1
        self.url = url
        self.view = "chat_list"     # a real boot lands on the chat list

    async def wait_for_timeout(self, ms):
        self.waited_ms += ms

    async def evaluate(self, *args, **kwargs):
        # No in-page bridges in a fake page: `ensure_stats_bridge()` then reports
        # unavailable and the warm-up degrades quietly, which is the behaviour we
        # want to prove is harmless.
        return False


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def goto(self, url=None):
        await self.page.goto(url or EITAA, wait_until="domcontentloaded")


class FakeDriver:
    """Only the members warmpath.open_page() is allowed to touch."""

    def __init__(self, page: FakePage, *, recoverable: bool = True) -> None:
        self.page = page
        self.session = FakeSession(page)
        self.recoverable = recoverable
        page.recoverable = recoverable
        self.returned_to_chat_list = 0
        self.login_checks = 0
        self.warmups = 0

    async def _return_to_chat_list(self):
        self.returned_to_chat_list += 1
        if self.recoverable:
            self.page.view = "chat_list"

    async def is_logged_in(self):
        self.login_checks += 1
        return self.page.view == "chat_list"

    async def bridge_stats(self, with_pvs: bool = True):
        self.warmups += 1
        return {"contacts": 1094, "pvs": -1 if with_pvs is False else 350}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def set_engine(name: str) -> None:
    store.set_setting("engine", name)


def real_driver(session) -> EitaaDriver:
    """A real EitaaDriver over a fake session (`page` is a read-only property
    that reads through to session.page, so only the session is injected)."""
    drv = EitaaDriver.__new__(EitaaDriver)
    drv.session = session
    return drv


# ---- 1. OFF is the old bot -------------------------------------------------

def test_off_is_unchanged() -> None:
    print("\n1) engine OFF -> every path behaves exactly as before")
    store.set_setting("warmpath", False)

    check("enabled() is False", warmpath.enabled() is False)
    check("use_pool() is False (Refresh keeps its own session)",
          warmpath.use_pool() is False)
    check("stats_with_pvs() is True (the PV count still happens)",
          warmpath.stats_with_pvs() is True)

    # Even on a perfectly warm page, OFF must decline.
    page = FakePage(EITAA + "/", "chat_list")
    drv = FakeDriver(page)
    check("open_page() declines on a warm page", run(warmpath.open_page(drv)) is False)
    check("it touched nothing at all",
          drv.returned_to_chat_list == 0 and drv.login_checks == 0 and drv.warmups == 0)

    # The real driver must then do its normal load.
    run(EitaaDriver.open(real_driver(drv.session)))
    check("the normal goto + 4s wait still runs",
          page.navigations == 1 and page.waited_ms == 4000,
          f"nav={page.navigations} wait={page.waited_ms}ms")


# ---- 2. ON, and the page is reusable --------------------------------------

def test_on_warm_page() -> None:
    print("\n2) engine ON, page already booted on the chat list")
    store.set_setting("warmpath", True)
    set_engine("bridge")

    page = FakePage(EITAA + "/", "chat_list")
    drv = FakeDriver(page)
    ok = run(warmpath.open_page(drv))
    check("open_page() accepts the warm page", ok is True)
    check("no navigation happened", page.navigations == 0)
    check("no 4s wait was paid", page.waited_ms == 0)
    check("login was confirmed before reusing the page", drv.login_checks == 1)
    check("an already-clean chat list needs no repair work",
          drv.returned_to_chat_list == 0, f"{drv.returned_to_chat_list} call(s)")
    check("the bridge engine pays no warm-up call", drv.warmups == 0,
          f"{drv.warmups} call(s)")

    # And through the real driver.
    run(EitaaDriver.open(real_driver(drv.session)))
    check("EitaaDriver.open() skipped the reload", page.navigations == 0)


# ---- 3. ON, but the page is not reusable ---------------------------------

def test_on_cold_page() -> None:
    print("\n3) engine ON, but the page was never loaded")
    store.set_setting("warmpath", True)
    page = FakePage("about:blank", "blank")
    drv = FakeDriver(page)
    check("open_page() declines a cold page", run(warmpath.open_page(drv)) is False)

    run(EitaaDriver.open(real_driver(drv.session)))
    check("the normal boot runs", page.navigations == 1 and page.waited_ms == 4000)


def test_on_foreign_page() -> None:
    print("\n4) engine ON, page navigated somewhere else")
    store.set_setting("warmpath", True)
    page = FakePage("https://example.com/", "chat_list")
    drv = FakeDriver(page)
    check("a foreign URL is not trusted", run(warmpath.open_page(drv)) is False)


def test_on_dirty_view_recovered() -> None:
    print("\n5) engine ON, previous job left the Contacts view open")
    store.set_setting("warmpath", True)
    set_engine("bridge")
    page = FakePage(EITAA + "/", "contacts_view")
    drv = FakeDriver(page, recoverable=True)
    ok = run(warmpath.open_page(drv))
    check("the view is restored without a reload", ok is True and page.navigations == 0)
    check("_return_to_chat_list() did the work", drv.returned_to_chat_list == 1)
    check("the chat list is what a send will see", page.view == "chat_list")


def test_on_dirty_view_unrecoverable() -> None:
    print("\n6) engine ON, the UI cannot be recovered by keyboard")
    store.set_setting("warmpath", True)
    page = FakePage(EITAA + "/", "popup")
    drv = FakeDriver(page, recoverable=False)
    check("open_page() gives up rather than guessing",
          run(warmpath.open_page(drv)) is False)

    run(EitaaDriver.open(real_driver(drv.session)))
    check("a real reload rescues it, as before", page.navigations == 1
          and page.view == "chat_list")


# ---- 4. the browser-free engine keeps its traffic ------------------------

def test_warmup_for_browserfree_engine() -> None:
    print("\n7) engine ON + browser-free send engine -> worker dump refilled")
    store.set_setting("warmpath", True)
    for engine, expect in (("bridge", 0), ("hybrid", 1), ("direct", 1)):
        set_engine(engine)
        page = FakePage(EITAA + "/", "chat_list")
        drv = FakeDriver(page)
        run(warmpath.open_page(drv))
        check(f"engine={engine}: warm-up calls = {expect}", drv.warmups == expect,
              f"got {drv.warmups}")
        check(f"engine={engine}: still no navigation", page.navigations == 0)
    set_engine("bridge")


# ---- 5. failures degrade to the old path --------------------------------

def test_failsafe() -> None:
    print("\n8) anything unexpected -> the old path, never a crash")
    store.set_setting("warmpath", True)

    class Exploding(FakeDriver):
        async def _return_to_chat_list(self):
            raise RuntimeError("page detached")

    # A dirty view forces the repair path, which is where this driver blows up.
    page = FakePage(EITAA + "/", "contacts_view")
    drv = Exploding(page)
    check("an exception inside the engine returns False",
          run(warmpath.open_page(drv)) is False)

    class NoPage:
        page = None

    check("a driver without a page returns False",
          run(warmpath.open_page(NoPage())) is False)

    # Whatever the engine decides, open() must leave a page a job can work on:
    # either reused (no navigation) or freshly loaded.
    cold = FakePage("about:blank", "blank")
    run(EitaaDriver.open(real_driver(FakeSession(cold))))
    check("a cold page is loaded", cold.navigations == 1 and cold.view == "chat_list")

    warm = FakePage(EITAA + "/", "chat_list")
    run(EitaaDriver.open(real_driver(FakeSession(warm))))
    check("a usable page is left usable either way",
          warm.url.startswith(EITAA) and warm.view == "chat_list",
          f"url={warm.url} view={warm.view} nav={warm.navigations}")


# ---- 6. the toggle itself ------------------------------------------------

def test_toggle_persists() -> None:
    print("\n9) the Settings toggle")
    store.set_setting("warmpath", False)
    check("default is OFF", store.warmpath is False)
    check("toggle turns it ON", store.toggle_warmpath() is True)
    check("the property agrees", store.warmpath is True)
    check("toggle turns it OFF again", store.toggle_warmpath() is False)
    check("engine follows the setting", warmpath.enabled() is False)
    store.set_setting("warmpath", True)
    check("engine follows the setting when ON", warmpath.enabled() is True)
    store.set_setting("warmpath", False)


# ---- 7. the whole sitting -----------------------------------------------

def test_full_sitting() -> None:
    print("\n10) one owner sitting: how many app boots, ON vs OFF")
    # Leftover view after each job, taken from the real code paths.
    sitting = [
        ("add account", "chat_list"),
        ("build contacts (UI flow)", "contacts_view"),
        ("update contacts", "chat_list"),
        ("refresh stats", "chat_list"),
        ("send broadcast", "chat_list"),
    ]
    results = {}
    for flag in (False, True):
        store.set_setting("warmpath", flag)
        set_engine("hybrid")
        page = FakePage("about:blank", "blank")
        drv = FakeDriver(page)
        real = real_driver(drv.session)
        for _job, leftover in sitting:
            run(EitaaDriver.open(real))
            page.view = leftover
        results[flag] = (page.navigations, page.waited_ms)
        print(f"    warmpath={'ON ' if flag else 'OFF'} -> boots={page.navigations} "
              f"fixed_waits={page.waited_ms}ms")
    store.set_setting("warmpath", False)
    set_engine("bridge")

    check("OFF keeps the old cost (a boot per job)", results[False][0] == 5,
          f"{results[False][0]} boots")
    check("ON boots once for the whole sitting", results[True][0] == 1,
          f"{results[True][0]} boots")
    # Only the one real boot pays the 4s settle; recovering a subview costs a
    # few hundred ms of keyboard work and no server request at all.
    check("ON spends far less time waiting", results[True][1] < results[False][1] / 2,
          f"{results[True][1]}ms vs {results[False][1]}ms")


def main() -> int:
    print("=" * 68)
    print("WARM PATH ENGINE")
    print("=" * 68)
    try:
        test_off_is_unchanged()
        test_on_warm_page()
        test_on_cold_page()
        test_on_foreign_page()
        test_on_dirty_view_recovered()
        test_on_dirty_view_unrecoverable()
        test_warmup_for_browserfree_engine()
        test_failsafe()
        test_toggle_persists()
        test_full_sitting()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL WARM PATH TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
