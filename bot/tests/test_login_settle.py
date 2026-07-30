"""Tests for confirming a login after sign-in (no browser, no network).

Run: python -m bot.tests.test_login_settle

Live defect this covers: a real, successful login was reported as "LOGIN
INCOMPLETE". The old code checked is_logged_in() once after 1.5s and once after a
reload + 6s, and on the target host (1 core, 30-89% CPU steal) the app needs
longer than that to switch to the chat list after sign-in.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_login_test_")
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

from bot import runner as R  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL += 1
        print(f"  FAIL  {name} {extra}")


class FakePage:
    def __init__(self):
        self.reloads = 0

    async def reload(self, **kw):
        self.reloads += 1

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)


class FakeDriver:
    """Reports logged-in only after `after` checks (or never).

    `storage_after` mimics the FAST proof: the auth keys appear in the page's own
    storage long before the chat list renders (one measured login had the app take
    272s to load, so waiting for the UI reported a good login as failed).
    """

    def __init__(self, after: int | None, storage_after: int | None = None):
        self.after = after
        self.storage_after = storage_after
        self.checks = 0
        self.storage_checks = 0

    async def has_auth_storage(self):
        self.storage_checks += 1
        if self.storage_after is None:
            return False
        return self.storage_checks >= self.storage_after

    async def is_logged_in(self):
        self.checks += 1
        if self.after is None:
            return False
        return self.checks >= self.after


def test_immediate_login():
    print("a fast login is confirmed on the first check")
    d = FakeDriver(after=1)
    s = types.SimpleNamespace(page=FakePage())
    ok = asyncio.run(R.JobManager()._wait_logged_in(d, s, None, timeout=5))
    check("confirmed", ok is True)
    check("only one check needed", d.checks == 1, d.checks)
    check("no reload was needed", s.page.reloads == 0, s.page.reloads)


def test_slow_login_is_still_confirmed():
    print("a slow app boot is confirmed instead of failing")
    # Ready only on the 4th check: the OLD code (2 checks) would have failed.
    d = FakeDriver(after=4)
    s = types.SimpleNamespace(page=FakePage())
    ok = asyncio.run(R.JobManager()._wait_logged_in(d, s, None, timeout=30))
    check("confirmed after several checks", ok is True, d.checks)
    check("it really took more than 2 checks", d.checks >= 3, d.checks)


def test_dead_login_gives_up_and_reloads_once():
    print("a login that never settles gives up, reloading exactly once")
    d = FakeDriver(after=None)
    s = types.SimpleNamespace(page=FakePage())
    ok = asyncio.run(R.JobManager()._wait_logged_in(d, s, None, timeout=8))
    check("gave up", ok is False)
    check("polled repeatedly", d.checks >= 2, d.checks)
    check("reloaded exactly once", s.page.reloads == 1, s.page.reloads)


def test_storage_confirms_before_the_ui_does():
    print("storage confirms the login long before the chat list renders")
    # The UI would only be ready on check 40; storage is ready on check 2.
    d = FakeDriver(after=40, storage_after=2)
    s = types.SimpleNamespace(page=FakePage())
    ok = asyncio.run(R.JobManager()._wait_logged_in(d, s, None, timeout=30))
    check("confirmed", ok is True)
    check("it did NOT wait for the rendered chat list", d.checks < 5, d.checks)
    check("no reload was needed", s.page.reloads == 0, s.page.reloads)


def test_timeout_is_generous_by_default():
    print("the default settle timeout is long enough for this host")
    # The app itself took 272s to load in a measured login, so 120s was too tight.
    check("default is at least 240s", R._LOGIN_SETTLE_TIMEOUT >= 240,
          R._LOGIN_SETTLE_TIMEOUT)
    check("and not unbounded", R._LOGIN_SETTLE_TIMEOUT <= 900,
          R._LOGIN_SETTLE_TIMEOUT)


def main() -> int:
    for fn in (test_immediate_login, test_slow_login_is_still_confirmed,
               test_dead_login_gives_up_and_reloads_once,
               test_storage_confirms_before_the_ui_does,
               test_timeout_is_generous_by_default):
        fn()
    print()
    print(f"{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("ALL LOGIN TESTS PASSED" if code == 0 else "LOGIN TESTS FAILED")
    sys.exit(code)
