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

from worker import selection, store  # noqa: E402

FAILURES: list[str] = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)
    return cond


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
