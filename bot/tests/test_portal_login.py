"""Preflight scenarios for the portal login adapter (portal/login_adapter.py).

Run: python -m bot.tests.test_portal_login

No browser and no web server. The adapter's dependency lookup (_deps) is swapped
for fakes that behave the way the real pieces do -- a warm-pool lease, an Eitaa
driver, the login_flow send_code/sign_in calls, and the manager's confirm/collect
helpers. That lets the adapter's REAL orchestration (the same pattern
bot.runner._bridge_login_job uses: hold the lease, await the code inside it) be
driven through every branch:

  * happy path: phone -> code -> logged in -> saved
  * 2FA: send_code says password needed -> password -> code -> logged in
  * wrong code, then the right one
  * wrong code up to the limit -> rejected
  * TTL expiry while waiting for the code
  * capacity: a third concurrent attempt is refused with a queue position
  * duplicate phone / already an account
  * the account is saved through the real contacts_store + attempt stats
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
import types

_TMP = tempfile.mkdtemp(prefix="mkwl_portal_login_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")
os.environ["MKWL_PORTAL_TTL"] = "5"          # short TTL so the expiry test is fast
os.environ["MKWL_PORTAL_MAX_LOGINS"] = "2"

from portal import attempts as attempts_mod  # noqa: E402
from portal import login_adapter as la  # noqa: E402
from portal import stats  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------
# Fakes standing in for the browser stack
# --------------------------------------------------------------------------
class FakePoolLease:
    """An async context manager like SessionPool.lease -> yields a session."""
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, session):
        self._session = session
    def lease(self, account, headed=None, init_script_path=None):
        return FakePoolLease(self._session)


class FakeDriver:
    def __init__(self, session):
        self.session = session
    async def open(self):
        return None
    async def is_logged_in(self):
        return bool(self.session.get("already_logged_in"))


class FakeLoginFlow:
    """Scripted send_code/sign_in. `sign_in_results` is consumed in order."""
    def __init__(self, *, send_code_result=None, sign_in_results=None):
        self.send_code_result = send_code_result or {"ok": True, "phone_code_hash": "H1"}
        self.sign_in_results = list(sign_in_results or [{"ok": True}])
        self.send_code_calls = 0
        self.sign_in_calls = 0

    def normalize_phone_intl(self, phone):
        import re
        s = re.sub(r"[^\d+]", "", phone or "").lstrip("+")
        if s.startswith("0") and len(s) == 11:
            s = "98" + s[1:]
        return s

    def resolve_api_creds(self):
        return 123, "hash"

    async def resolve_creds_with_page(self, driver, api_id, api_hash):
        return api_id or 123, api_hash or "hash"

    async def send_code(self, driver, intl, api_id, api_hash):
        self.send_code_calls += 1
        return dict(self.send_code_result)

    async def sign_in(self, driver, intl, phch, code):
        self.sign_in_calls += 1
        if self.sign_in_results:
            return dict(self.sign_in_results.pop(0))
        return {"ok": True}


class FakeManager:
    def __init__(self, *, logged_in=True, contacts=7, busy=False):
        self._logged_in = logged_in
        self._contacts = contacts
        self._busy = busy
    def is_busy(self, account):
        return self._busy
    def settings_provider(self):
        return {}
    async def _wait_logged_in(self, driver, session):
        return self._logged_in
    async def _collect_contacts(self, driver, account):
        return ([{"title": f"c{i}", "peer_id": str(i)} for i in range(self._contacts)], "api")


def install(*, session=None, login_flow=None, manager=None):
    session = session if session is not None else {}
    login_flow = login_flow or FakeLoginFlow()
    manager = manager or FakeManager()
    la._deps = lambda: (FakePool(session), FakeDriver, login_flow, manager)
    return login_flow, manager


def reset_registry():
    la.registry = attempts_mod.Attempts()
    # login_adapter reads module-global `registry`; rebind it.
    import importlib
    la.registry = attempts_mod.Attempts()
    try:
        stats._path().unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------
def test_happy_path():
    print("\nphone -> code -> logged in -> account saved")
    reset_registry()
    lf, mgr = install(manager=FakeManager(contacts=12))
    started = run(la.begin("09991048633"))
    check("code was sent", started.get("next") == "code", str(started))
    check("an attempt id + token came back",
          bool(started.get("attempt_id") and started.get("attempt_token")))
    attempt = la.registry.get(started["attempt_id"])
    out = run(la.submit_code(attempt, "12345", started["attempt_token"]))
    check("login succeeded", out.get("ok") is True, str(out))
    check("account is the normalised phone", out.get("account") == "989991048633", str(out.get("account")))
    check("contacts were collected + saved", out.get("contacts") == 12, str(out.get("contacts")))
    from bot import contacts_store
    check("the contacts cache really has them",
          contacts_store.count("989991048633") == 12,
          str(contacts_store.count("989991048633")))
    s = stats.summary()["today"]
    check("stats counted a success", s["success"] == 1 and s["started"] == 1, str(s))
    check("the attempt was dropped from the registry", la.registry.count() == 0)


def test_two_factor_is_refused_honestly():
    print("\n2FA: the adapter does NOT pretend to handle a password (like the bot)")
    reset_registry()
    # send_code succeeds, but sign_in reports 2FA needed -> honest refusal.
    lf = FakeLoginFlow(sign_in_results=[{"ok": False, "needs_password": True}])
    install(login_flow=lf)
    started = run(la.begin("09991048634"))
    check("code was sent (2FA shows up at sign-in)", started.get("next") == "code",
          str(started))
    attempt = la.registry.get(started["attempt_id"])
    out = run(la.submit_code(attempt, "54321", started["attempt_token"]))
    check("it is refused with a clear 2FA message", out.get("code") == "password_needed",
          str(out))
    check("not falsely reported as success", not out.get("ok"))
    check("attempt cleaned up", la.registry.count() == 0)


def test_wrong_then_right_code():
    print("\na wrong code is reported with tries left, then the right one works")
    reset_registry()
    lf = FakeLoginFlow(sign_in_results=[
        {"ok": False, "code": "PHONE_CODE_INVALID"},
        {"ok": True},
    ])
    install(login_flow=lf)
    started = run(la.begin("09991048635"))
    attempt = la.registry.get(started["attempt_id"])
    bad = run(la.submit_code(attempt, "00000", started["attempt_token"]))
    check("the wrong code is flagged", bad.get("code") == "wrong_code", str(bad))
    check("it says a try was used", bad.get("wrong_code_events") == 1, str(bad))
    check("still retryable", bad.get("retryable") is True)
    good = run(la.submit_code(attempt, "12345", started["attempt_token"]))
    check("the right code then works", good.get("ok") is True, str(good))


def test_wrong_code_limit():
    print("\nwrong codes up to the limit end the attempt")
    reset_registry()
    os.environ["MKWL_PORTAL_MAX_WRONG_CODES"] = "3"
    import config as cfg
    cfg.config.PORTAL_MAX_WRONG_CODES = 3
    lf = FakeLoginFlow(sign_in_results=[{"ok": False, "code": "WRONG_CODE"}] * 3)
    install(login_flow=lf)
    started = run(la.begin("09991048636"))
    attempt = la.registry.get(started["attempt_id"])
    r1 = run(la.submit_code(attempt, "00001", started["attempt_token"]))
    r2 = run(la.submit_code(attempt, "00002", started["attempt_token"]))
    r3 = run(la.submit_code(attempt, "00003", started["attempt_token"]))
    check("1st wrong: retryable", r1.get("code") == "wrong_code")
    check("2nd wrong: retryable", r2.get("code") == "wrong_code")
    check("3rd wrong: limit reached", r3.get("code") == "wrong_code_limit", str(r3))
    check("attempt dropped after the limit", la.registry.count() == 0)
    check("stats show a failure", stats.summary()["today"]["failed"] == 1)


def test_ttl_expiry():
    print("\nwaiting past the TTL expires the attempt (no code ever entered)")
    reset_registry()
    install()
    # The production TTL has a 60s floor (a real browser login needs minutes);
    # override just the accessor so the test does not have to wait a minute.
    la.registry.ttl = lambda: 1

    async def scenario():
        # One continuous loop run, so the background _run task's TTL timer fires
        # cleanly (stopping/restarting the loop between calls skews the timer).
        started = await la.begin("09991048637")
        attempt = la.registry.get(started["attempt_id"])
        for _ in range(80):
            if attempt.get("_finished"):
                break
            await asyncio.sleep(0.1)
        return started, attempt.get("outcome")

    started, outcome = run(scenario())
    check("code was sent", started.get("next") == "code", str(started))
    check("it expired", (outcome or {}).get("code") == "expired", str(outcome))
    check("registry is empty", la.registry.count() == 0)
    check("stats counted it expired", stats.summary()["today"]["expired"] == 1)
    cfg.config.PORTAL_TTL_SECONDS = 600


def test_capacity_queue():
    print("\na third concurrent attempt is refused with a queue position")
    reset_registry()
    import config as cfg
    cfg.config.PORTAL_MAX_LOGINS = 2
    # A flow that never returns from send_code, so attempts stay 'in flight'.
    class HangFlow(FakeLoginFlow):
        async def send_code(self, driver, intl, api_id, api_hash):
            await asyncio.sleep(30)
            return {"ok": True, "phone_code_hash": "H"}
    install(login_flow=HangFlow())

    async def scenario():
        t1 = asyncio.create_task(la.begin("09991000001"))
        t2 = asyncio.create_task(la.begin("09991000002"))
        await asyncio.sleep(0.2)   # let both occupy a slot
        third = await la.begin("09991000003")
        for t in (t1, t2):
            t.cancel()
        return third

    third = run(scenario())
    check("the third is refused for capacity", third.get("code") == "capacity", str(third))
    check("it reports a queue position", third.get("position", 0) >= 1, str(third.get("position")))
    cfg.config.PORTAL_MAX_LOGINS = 2


def test_duplicate_phone():
    print("\na phone that is already an account is refused up front")
    reset_registry()
    install()
    # Create a profile dir so the account looks already-added.
    from config import config as cfg
    (cfg.PROFILES_DIR / "989991099999").mkdir(parents=True, exist_ok=True)
    out = run(la.begin("09991099999"))
    check("refused as duplicate", out.get("code") == "duplicate", str(out))
    check("no attempt was created", la.registry.count() == 0)


def test_invalid_phone():
    print("\nan invalid phone never reaches the browser")
    reset_registry()
    lf, mgr = install()
    out = run(la.begin("0912"))
    check("refused as invalid", out.get("code") == "invalid_phone", str(out))
    check("send_code was never called", lf.send_code_calls == 0)


def main() -> int:
    print("=" * 68)
    print("  PORTAL LOGIN ADAPTER — PREFLIGHT SCENARIOS")
    print("=" * 68)
    asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        test_happy_path()
        test_two_factor_is_refused_honestly()
        test_wrong_then_right_code()
        test_wrong_code_limit()
        test_ttl_expiry()
        test_capacity_queue()
        test_duplicate_phone()
        test_invalid_phone()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL PORTAL LOGIN SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
