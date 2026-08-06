"""Tests for the worker foundation (worker/store.py, worker/selection.py).

Run: python -m bot.tests.test_worker

Pure-Python: the JSON registry, tag generation, health, account→worker affinity,
and the selection logic (least-loaded round-robin with affinity + failover). The
SSH/Docker/tunnel/API transport is NOT tested here -- it needs a real second
server and is exercised live.
"""
from __future__ import annotations

import os
import shutil
import tempfile

_TMP = tempfile.mkdtemp(prefix="mkwl_worker_test_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["PROFILES_DIR"] = os.path.join(_TMP, "profiles")
os.environ["ARTIFACTS_DIR"] = os.path.join(_TMP, "artifacts")

from worker import health, selection, store  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


def run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def fresh():
    try:
        store._path().unlink()
    except OSError:
        pass


def test_master_worker():
    print("\na local master worker exists and runs jobs in-process")
    fresh()
    m = store.ensure_master()
    check("master created", bool(m) and m["is_master"])
    check("tag is the master family #W0_", m["tag"].startswith("#W0_"), m["tag"])
    check("calling again does not duplicate it",
          store.ensure_master()["id"] == m["id"] and len(store.list_workers()) == 1)
    check("is_local recognises it", store.is_local(m))


def test_new_account_defaults_to_master():
    print("\nwith no remote workers a new account runs locally (like today)")
    fresh()
    store.ensure_master()
    w = selection.assign_new_account("989120000001")
    check("assigned to the master", store.is_local(w))
    check("runs_locally is True", selection.runs_locally("989120000001"))
    check("the master now owns 1 account", store.count_accounts_on(w["id"]) == 1)


def test_round_robin_across_healthy_remotes():
    print("\nnew accounts spread least-loaded across healthy workers (round-robin)")
    fresh()
    m = store.ensure_master()
    r1 = store.add_remote("1.1.1.1", 22, 8799)
    r2 = store.add_remote("2.2.2.2", 22, 8799)
    store.set_health(r1, "ok", 40)
    store.set_health(r2, "ok", 55)
    # master has 0, r1 0, r2 0 -> ties break by id (master id=1 wins first)
    picks = []
    for i in range(6):
        w = selection.assign_new_account(f"9891230000{i:02d}")
        picks.append(w["id"])
    counts = {wid: picks.count(wid) for wid in set(picks)}
    check("all three workers were used", len(counts) == 3, str(counts))
    check("load is balanced (each got 2)", set(counts.values()) == {2}, str(counts))


def test_unhealthy_remote_is_skipped():
    print("\nan unhealthy remote is not chosen (master + healthy remote only)")
    fresh()
    store.ensure_master()
    good = store.add_remote("1.1.1.1", 22, 8799)
    bad = store.add_remote("2.2.2.2", 22, 8799)
    store.set_health(good, "ok", 30)
    store.set_health(bad, "down", -1)
    seen = set()
    for i in range(8):
        w = selection.assign_new_account(f"9891240000{i:02d}")
        seen.add(w["id"])
    check("the down worker was never chosen", bad not in seen, str(seen))
    check("only master + healthy remote used", seen == {store.master()["id"], good},
          str(seen))


def test_session_affinity():
    print("\nan existing account STAYS on its worker (session lives in its profile)")
    fresh()
    store.ensure_master()
    r1 = store.add_remote("1.1.1.1", 22, 8799)
    store.set_health(r1, "ok", 30)
    store.assign("989125550000", r1)
    w = selection.worker_for_account("989125550000")
    check("it resolves to its assigned worker", w["id"] == r1, str(w))
    check("runs_locally is False (it is on a remote)",
          not selection.runs_locally("989125550000"))
    # Even if that worker later looks busier, affinity does not move it.
    for i in range(5):
        selection.assign_new_account(f"9891260000{i:02d}")
    check("affinity is unchanged after other accounts are added",
          selection.worker_for_account("989125550000")["id"] == r1)


def test_unknown_account_falls_back_to_master():
    print("\nan account with no assignment falls back to the master")
    fresh()
    store.ensure_master()
    w = selection.worker_for_account("989129999999")
    check("falls back to master", store.is_local(w))
    check("runs_locally True", selection.runs_locally("989129999999"))


def test_removing_a_worker_unpins_its_accounts():
    print("\nremoving a worker unpins its accounts (they fall back to master)")
    fresh()
    store.ensure_master()
    r1 = store.add_remote("1.1.1.1", 22, 8799)
    store.set_health(r1, "ok", 30)
    store.assign("989127770000", r1)
    check("pinned before removal",
          selection.worker_for_account("989127770000")["id"] == r1)
    store.remove(r1)
    check("worker gone", store.get(r1) is None)
    check("account now falls back to master",
          store.is_local(selection.worker_for_account("989127770000")))


def test_exclude_for_transfer():
    print("\nexclude_id lets a transfer pick a DIFFERENT worker than the current")
    fresh()
    store.ensure_master()
    r1 = store.add_remote("1.1.1.1", 22, 8799)
    store.set_health(r1, "ok", 30)
    # master id=1, r1 id=2. Excluding the master forces the remote.
    w = selection.pick_for_new_account(exclude_id=store.master()["id"])
    check("the master was excluded", not store.is_local(w) and w["id"] == r1, str(w))


def test_no_usable_worker():
    print("\nif nothing is usable, selection returns None instead of guessing")
    fresh()
    import config as cfg
    original = cfg.config.MASTER_AS_WORKER
    cfg.config.MASTER_AS_WORKER = False   # no local master
    try:
        # a single remote, and it is down
        bad = store.add_remote("9.9.9.9", 22, 8799)
        store.set_health(bad, "down", -1)
        check("returns None", selection.pick_for_new_account() is None)
    finally:
        cfg.config.MASTER_AS_WORKER = original


def test_tags_are_unique():
    print("\ngenerated tags do not collide")
    fresh()
    store.ensure_master()
    ids = [store.add_remote(f"1.1.1.{i}", 22, 8799) for i in range(12)]
    tags = [store.get(i)["tag"] for i in ids]
    check("all tags unique", len(set(tags)) == len(tags), str(tags))
    check("remotes use the non-master family (#W1.. #W9)",
          all(not t.startswith("#W0_") for t in tags), str(tags))


def test_capacity_full_worker_skipped():
    print("\na worker at its account capacity is not chosen for new accounts")
    fresh()
    store.ensure_master()
    small = store.add_remote("1.1.1.1", 22, 8799, max_accounts=2)
    store.set_health(small, "ok", 30)
    # Master is uncapped; force the remote by disabling the master temporarily.
    store.set_enabled(store.master()["id"], False)
    try:
        a = selection.assign_new_account("989130000001")
        b = selection.assign_new_account("989130000002")
        check("first two go to the capped remote", a["id"] == small and b["id"] == small)
        check("remote is now full", not store.has_room(store.get(small)))
        c = selection.pick_for_new_account()
        check("a third finds no room -> None", c is None, str(c))
    finally:
        store.set_enabled(store.master()["id"], True)
    # With the master back, the overflow lands on it.
    d = selection.assign_new_account("989130000003")
    check("overflow goes to the uncapped master", store.is_local(d))


def test_free_slots_reporting():
    print("\nfree_slots reports remaining room; uncapped is None")
    fresh()
    store.ensure_master()
    w = store.add_remote("1.1.1.1", 22, 8799, max_accounts=3)
    store.set_health(w, "ok", 30)
    check("3 free at first", store.free_slots(store.get(w)) == 3)
    store.assign("989131110001", w)
    check("2 free after one", store.free_slots(store.get(w)) == 2)
    check("master is uncapped (None)", store.free_slots(store.master()) is None)


def test_transfer_moves_affinity():
    print("\ntransfer moves an account's routing to another worker")
    fresh()
    store.ensure_master()
    r1 = store.add_remote("1.1.1.1", 22, 8799)
    r2 = store.add_remote("2.2.2.2", 22, 8799)
    store.set_health(r1, "ok", 30)
    store.set_health(r2, "ok", 30)
    store.assign("989132220000", r1)
    check("starts on r1", selection.worker_for_account("989132220000")["id"] == r1)
    ok = store.transfer("989132220000", r2)
    check("transfer succeeded", ok is True)
    check("now on r2", selection.worker_for_account("989132220000")["id"] == r2)
    check("r1 no longer counts it", store.count_accounts_on(r1) == 0)
    check("transfer to a missing worker fails", store.transfer("989132220000", 999) is False)


def test_health_backoff_grows():
    print("\na failing worker is re-checked less and less often (backoff)")
    fresh()
    store.ensure_master()
    w = store.add_remote("1.1.1.1", 22, 8799)
    import time as _t
    now = _t.time()
    store.set_health(w, "ok", 30)
    base_due = store.next_check_due(store.get(w), base=30)
    check("healthy: due in ~30s", abs(base_due - (store.get(w)["health_ts"] + 30)) < 1)
    store.set_health(w, "down", -1)
    store.set_health(w, "down", -1)
    row = store.get(w)
    check("two failures counted", row["fails"] == 2, str(row["fails"]))
    due = store.next_check_due(row, base=30)
    # 30 * 2^2 = 120s after the last check
    check("backoff widened to ~120s", abs(due - (row["health_ts"] + 120)) < 1, str(due - row["health_ts"]))
    store.set_health(w, "ok", 25)
    check("a success resets the streak", store.get(w)["fails"] == 0)


def test_health_check_classifies():
    print("\nhealth.check_worker classifies down / blocked / ok via fake probes")
    fresh()
    store.ensure_master()
    w = store.add_remote("1.1.1.1", 22, 8799)

    async def tcp_down(host, port, timeout=3.0):
        return -1
    async def tcp_ok(host, port, timeout=3.0):
        return 42
    async def api_no(worker, timeout=8.0):
        return False
    async def api_yes(worker, timeout=8.0):
        return True

    r = run(health.check_worker(store.get(w), tcp_ping=tcp_down, api_ping=api_no))
    check("unreachable -> down", r["status"] == "down", str(r))
    r = run(health.check_worker(store.get(w), tcp_ping=tcp_ok, api_ping=api_no))
    check("reachable but API silent -> blocked", r["status"] == "blocked", str(r))
    r = run(health.check_worker(store.get(w), tcp_ping=tcp_ok, api_ping=api_yes))
    check("reachable + API answers -> ok", r["status"] == "ok", str(r))
    check("ok was persisted to the store", store.get(w)["status"] == "ok")
    check("the local master is always ok without probing",
          run(health.check_worker(store.master()))["status"] == "ok")


def test_health_check_all_parallel_and_due():
    print("\ncheck_all runs in parallel and skips workers not yet due")
    fresh()
    store.ensure_master()
    w1 = store.add_remote("1.1.1.1", 22, 8799)
    w2 = store.add_remote("2.2.2.2", 22, 8799)

    async def tcp_ok(host, port, timeout=3.0):
        return 10
    async def api_yes(worker, timeout=8.0):
        return True

    res = run(health.check_all(only_due=True, tcp_ping=tcp_ok, api_ping=api_yes))
    check("both freshly-added workers were checked", len(res) == 2, str(len(res)))
    check("both ok", all(r["status"] == "ok" for r in res))
    # Immediately after, they are healthy and not due again -> skipped.
    res2 = run(health.check_all(only_due=True, tcp_ping=tcp_ok, api_ping=api_yes))
    check("nothing is due right after a healthy check", res2 == [], str(res2))
    # only_due=False forces a re-check.
    res3 = run(health.check_all(only_due=False, tcp_ping=tcp_ok, api_ping=api_yes))
    check("only_due=False re-checks anyway", len(res3) == 2, str(len(res3)))


def test_health_check_all_survives_one_bad_worker():
    print("\none worker whose probe raises does not sink the whole cycle")
    fresh()
    store.ensure_master()
    good = store.add_remote("1.1.1.1", 22, 8799)
    bad = store.add_remote("2.2.2.2", 22, 8799)

    async def tcp_ok(host, port, timeout=3.0):
        return 10
    async def api_maybe(worker, timeout=8.0):
        if worker["ip"] == "2.2.2.2":
            raise RuntimeError("boom")
        return True

    res = run(health.check_all(only_due=True, tcp_ping=tcp_ok, api_ping=api_maybe))
    by_ip = {r["id"]: r["status"] for r in res}
    check("the good worker is ok", by_ip.get(good) == "ok", str(by_ip))
    check("the bad worker is down, not a crash", by_ip.get(bad) == "down", str(by_ip))


def test_healthy_helper():
    print("\nhealthy() gates new work correctly")
    fresh()
    store.ensure_master()
    w = store.add_remote("1.1.1.1", 22, 8799)
    check("master always healthy", health.healthy(store.master()))
    check("never-checked remote is tentatively usable", health.healthy(store.get(w)))
    store.set_health(w, "down", -1)
    check("a down remote is not healthy", not health.healthy(store.get(w)))
    store.set_health(w, "ok", 20)
    check("an ok remote is healthy", health.healthy(store.get(w)))


def main() -> int:
    print("=" * 68)
    print("  WORKER FOUNDATION TESTS")
    print("=" * 68)
    try:
        test_master_worker()
        test_new_account_defaults_to_master()
        test_round_robin_across_healthy_remotes()
        test_unhealthy_remote_is_skipped()
        test_session_affinity()
        test_unknown_account_falls_back_to_master()
        test_removing_a_worker_unpins_its_accounts()
        test_exclude_for_transfer()
        test_no_usable_worker()
        test_tags_are_unique()
        test_capacity_full_worker_skipped()
        test_free_slots_reporting()
        test_transfer_moves_affinity()
        test_health_backoff_grows()
        test_health_check_classifies()
        test_health_check_all_parallel_and_due()
        test_health_check_all_survives_one_bad_worker()
        test_healthy_helper()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"   - {f}")
        return 1
    print("ALL WORKER FOUNDATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
