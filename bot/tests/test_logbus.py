"""Tests for the central log group (bot/logbus.py).

Run: python -m bot.tests.test_logbus

No telethon: a fake client records what would be sent. Covers the gating (off
until an id is set AND enabled), the fail-safe (a broken client never raises),
and the card formatting.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="mkwl_logbus_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")

from bot import logbus  # noqa: E402
from bot.store import store  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeClient:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
    async def send_message(self, chat, text):
        if self.fail:
            raise RuntimeError("network down")
        self.sent.append((chat, text))


def reset():
    store.set_log_group_id(0)
    if not store.log_group_enabled:
        store.toggle_log_group()
    logbus.bind(None)


def test_off_until_configured():
    print("\nlogbus stays silent until a group id is set")
    reset()
    fc = FakeClient()
    logbus.bind(fc)
    check("not configured with id 0", not logbus.configured())
    ok = run(logbus.to_group("hello"))
    check("to_group returns False when unconfigured", ok is False)
    check("nothing was sent", fc.sent == [])


def test_live_when_set_and_enabled():
    print("\nwith an id set and enabled it posts to the group")
    reset()
    fc = FakeClient()
    logbus.bind(fc)
    store.set_log_group_id(-1001234567890)
    check("now configured", logbus.configured())
    ok = run(logbus.event("🔐 LOGIN", ["phone: 98912...", "ok"]))
    check("event returned True", ok is True)
    check("one message sent", len(fc.sent) == 1, str(fc.sent))
    chat, text = fc.sent[0]
    check("sent to the configured group", chat == -1001234567890, str(chat))
    check("card has the title + divider", "🔐 LOGIN" in text and logbus.LINE in text)


def test_disabled_toggle_silences_it():
    print("\ntoggling the group off silences it even with an id set")
    reset()
    fc = FakeClient()
    logbus.bind(fc)
    store.set_log_group_id(-100999)
    store.toggle_log_group()   # now OFF
    check("not configured when disabled", not logbus.configured())
    run(logbus.to_group("x"))
    check("nothing sent while disabled", fc.sent == [])
    store.toggle_log_group()   # back ON
    run(logbus.to_group("y"))
    check("sends again once re-enabled", len(fc.sent) == 1)


def test_never_raises_on_broken_client():
    print("\na broken client is swallowed (a job must never break on logging)")
    reset()
    logbus.bind(FakeClient(fail=True))
    store.set_log_group_id(-100777)
    ok = run(logbus.to_group("boom"))
    check("returns False instead of raising", ok is False)


def test_no_client_bound():
    print("\nno client bound yet = safe no-op")
    reset()
    logbus.bind(None)
    store.set_log_group_id(-100555)
    check("configured (id+enabled) but no client", logbus.configured())
    check("to_group is a safe False", run(logbus.to_group("x")) is False)


def main() -> int:
    print("=" * 68)
    print("  LOGBUS TESTS")
    print("=" * 68)
    try:
        test_off_until_configured()
        test_live_when_set_and_enabled()
        test_disabled_toggle_silences_it()
        test_never_raises_on_broken_client()
        test_no_client_bound()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL LOGBUS TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
