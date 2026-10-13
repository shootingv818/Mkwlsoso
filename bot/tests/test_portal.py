"""Tests for the portal foundation (portal/stats.py, status.py, attempts.py).

Run: python -m bot.tests.test_portal

No web server and no browser: only the pure-Python foundation is exercised --
the durable JSON stats (the fix for the original's in-memory-only weakness), the
live status snapshot, and the attempt registry (tokens, TTL, capacity/queue
position, lock retirement). The FastAPI/cloudflared/browser layers are covered
separately, live, because they need real dependencies.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="mkwl_portal_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")

from portal import attempts as attempts_mod  # noqa: E402
from portal import stats, status  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def fresh_stats():
    try:
        stats._path().unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# stats.py -- durable JSON, the weakness fix
# --------------------------------------------------------------------------

def test_stats_lifecycle():
    print("\nan attempt walks pending -> started -> success and is counted")
    fresh_stats()
    stats.create_attempt("a1", "989990000001", "h1", time.time(), time.time() + 600)
    check("created as pending", stats.recent(1)[0]["status"] == "pending")
    check("mark_started once returns True", stats.mark_started("a1") is True)
    check("mark_started twice returns False", stats.mark_started("a1") is False)
    stats.finish("a1", "success", account="acct_1")
    s = stats.summary()
    check("today started = 1", s["today"]["started"] == 1, str(s["today"]))
    check("today success = 1", s["today"]["success"] == 1)
    check("rate is 100%", s["today"]["rate"] == 100.0, str(s["today"]["rate"]))
    check("a finished attempt is no longer pending", stats.pending_count() == 0)


def test_stats_wrong_code_and_fail():
    print("\nwrong codes are counted; a fail is terminal")
    fresh_stats()
    stats.create_attempt("a2", "989990000002", "h", time.time(), time.time() + 600)
    stats.mark_started("a2")
    check("first wrong code -> 1", stats.wrong_code("a2", "WRONG_CODE") == 1)
    check("second wrong code -> 2", stats.wrong_code("a2", "WRONG_CODE") == 2)
    stats.finish("a2", "failed", error="wrong code limit")
    check("no more wrong codes after terminal",
          stats.wrong_code("a2", "x") == 2, "frozen at 2")
    s = stats.summary()["today"]
    check("failed counted", s["failed"] == 1, str(s))
    check("started but not success -> rate 0", s["rate"] == 0.0, str(s["rate"]))


def test_stats_survives_reload():
    print("\nthe record is on DISK: a 'restart' does not lose it")
    fresh_stats()
    stats.create_attempt("a3", "989990000003", "h", time.time(), time.time() + 600)
    stats.mark_started("a3")
    stats.finish("a3", "success", account="acct_3")
    # Simulate a process restart: nothing in memory, only the file.
    import importlib
    importlib.reload(stats)
    s = stats.summary()["total"]
    check("success survived the reload", s["success"] == 1, str(s))
    check("the file is real JSON on disk", stats._path().is_file())


def test_expire_stale_on_restart():
    print("\na 'pending' row left by a dead process is closed on boot")
    fresh_stats()
    stats.create_attempt("a4", "989990000004", "h", time.time(), time.time() + 600)
    stats.mark_started("a4")
    check("pending before boot", stats.pending_count() == 1)
    closed = stats.expire_stale()
    check("expire_stale closed it", closed == 1, str(closed))
    check("no longer pending", stats.pending_count() == 0)
    check("counted as expired", stats.summary()["total"]["expired"] == 1)


def test_abandoned_before_start_does_not_hurt_rate():
    print("\na phone typed but abandoned before a code is NOT in the denominator")
    fresh_stats()
    stats.create_attempt("a5", "989990000005", "h", time.time(), time.time() + 600)
    # never mark_started -> then it expires
    stats.finish("a5", "expired", error="ttl")
    stats.create_attempt("a6", "989990000006", "h", time.time(), time.time() + 600)
    stats.mark_started("a6")
    stats.finish("a6", "success", account="x")
    s = stats.summary()["today"]
    check("only the started one counts", s["started"] == 1, str(s))
    check("rate is a clean 100%, not 50%", s["rate"] == 100.0, str(s["rate"]))


def test_file_is_bounded():
    print("\nthe stats file cannot grow without bound")
    fresh_stats()
    for i in range(stats._MAX_ROWS + 50):
        stats.create_attempt(f"b{i}", "98999", "h", time.time(), time.time() + 1)
    rows = stats._load()["attempts"]
    check("row count is capped", len(rows) <= stats._MAX_ROWS, str(len(rows)))
    check("newest are kept", rows[-1]["attempt_id"] == f"b{stats._MAX_ROWS + 49}")


# --------------------------------------------------------------------------
# status.py
# --------------------------------------------------------------------------

def test_status_snapshot():
    print("\nthe live status snapshot is a copy, not the internal dict")
    status.set_state("running", mode="quick", url="https://x.trycloudflare.com",
                     server=True, tunnel=True)
    snap = status.snapshot()
    check("status running", snap["status"] == "running")
    check("url set", "trycloudflare" in snap["url"])
    snap["status"] = "TAMPERED"
    check("mutating the copy does not change the source",
          status.snapshot()["status"] == "running")
    status.clear_runtime("off")
    check("clear_runtime resets url + flags",
          status.snapshot()["url"] == "" and status.snapshot()["server"] is False)


# --------------------------------------------------------------------------
# attempts.py -- tokens, TTL, capacity, locks
# --------------------------------------------------------------------------

def test_attempt_token_ownership():
    print("\nan attempt is owned by its token, not just its id")
    reg = attempts_mod.Attempts()
    aid, token = reg.new_id_token()
    attempt = reg.create(aid, token, "989990000010")
    check("the right token verifies", reg.verify(attempt, token))
    check("a wrong token does not", not reg.verify(attempt, "guessed"))
    check("an empty token does not", not reg.verify(attempt, ""))
    check("ids and tokens are different lengths (id short, token long)",
          len(token) > len(aid))


def test_capacity_and_queue_position():
    print("\ncapacity reports a QUEUE POSITION, not a bare 'busy'")
    reg = attempts_mod.Attempts()
    import config as config_mod
    original = config_mod.config.PORTAL_MAX_LOGINS
    config_mod.config.PORTAL_MAX_LOGINS = 2
    try:
        check("empty -> a slot is free (position 0)", reg.capacity_position() == 0)
        a1, t1 = reg.new_id_token(); reg.create(a1, t1, "989990000011")
        check("one used, still a free slot", not reg.at_capacity())
        a2, t2 = reg.new_id_token(); reg.create(a2, t2, "989990000012")
        check("two used -> at capacity", reg.at_capacity())
        check("a third would be 1st in line", reg.capacity_position() == 1,
              str(reg.capacity_position()))
    finally:
        config_mod.config.PORTAL_MAX_LOGINS = original


def test_ttl_is_host_appropriate():
    print("\nthe TTL is long enough for this host's slow Chromium boot")
    reg = attempts_mod.Attempts()
    check("TTL >= 300s (longer than Makiioo's 300)", reg.ttl() >= 300, str(reg.ttl()))
    aid, token = reg.new_id_token()
    attempt = reg.create(aid, token, "989990000013")
    check("a fresh attempt is not expired", not reg.expired(attempt))
    check("remaining is close to the TTL",
          abs(reg.remaining(attempt) - reg.ttl()) <= 2, str(reg.remaining(attempt)))


def test_lock_retires_only_when_free():
    print("\na per-attempt lock is dropped only after everyone left")
    reg = attempts_mod.Attempts()
    aid, token = reg.new_id_token()
    reg.create(aid, token, "989990000014")
    lock = reg.lock_for(aid)

    async def scenario():
        await lock.acquire()               # someone is holding it
        reg.pop(aid)                        # attempt removed from registry
        task = asyncio.create_task(reg.retire_lock(aid, lock))
        await asyncio.sleep(0.15)
        held_still_registered = aid in reg._locks
        lock.release()                      # owner leaves
        await task
        return held_still_registered, aid in reg._locks

    while_held, after = run(scenario())
    check("lock is NOT retired while still held", while_held is True)
    check("lock IS retired once released and unused", after is False)


def test_expired_attempt_detected():
    print("\nan attempt past its deadline reads as expired")
    reg = attempts_mod.Attempts()
    aid, token = reg.new_id_token()
    attempt = reg.create(aid, token, "989990000015")
    attempt["expires_at"] = time.time() - 1
    check("expired() is True", reg.expired(attempt))
    check("remaining() is 0", reg.remaining(attempt) == 0)


def main() -> int:
    print("=" * 68)
    print("  PORTAL FOUNDATION TESTS")
    print("=" * 68)
    try:
        test_stats_lifecycle()
        test_stats_wrong_code_and_fail()
        test_stats_survives_reload()
        test_expire_stale_on_restart()
        test_abandoned_before_start_does_not_hurt_rate()
        test_file_is_bounded()
        test_status_snapshot()
        test_attempt_token_ownership()
        test_capacity_and_queue_position()
        test_ttl_is_host_appropriate()
        test_lock_retires_only_when_free()
        test_expired_attempt_detected()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL PORTAL FOUNDATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
