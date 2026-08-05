"""Choosing a worker: session affinity first, else least-loaded round-robin.

Ported from Makiioo's pick_worker_for_login / worker_for_account, adapted to the
JSON store here. Two rules:

  * An account that is ALREADY on a worker stays there (session affinity) -- its
    Eitaa profile lives on that machine, so its jobs must run there.
  * A NEW account goes to the enabled, healthy worker with the FEWEST accounts,
    which is round-robin as accounts are added one at a time. The local master
    is always usable; a remote worker must be healthy ("ok").

`config.MASTER_AS_WORKER` (default True) keeps a local master that runs jobs
in-process, so with no remote workers everything behaves exactly as before.
"""
from __future__ import annotations

from config import config

from . import store


def master_as_worker() -> bool:
    return bool(getattr(config, "MASTER_AS_WORKER", True))


def worker_for_account(account: str) -> dict | None:
    """Where an existing account's jobs must run (affinity), or the master."""
    return store.worker_for_account(account)


def pick_for_new_account(exclude_id: int | None = None) -> dict | None:
    """Least-loaded enabled+healthy worker for a brand-new account.

    Round-robin falls out of "fewest accounts" because accounts are added one at
    a time. Returns None if nothing is usable. `exclude_id` skips a worker (used
    when moving an account to a DIFFERENT server than its current one).
    """
    if master_as_worker():
        store.ensure_master()
    workers = store.list_enabled()
    usable = []
    for w in workers:
        if exclude_id is not None and int(w.get("id") or 0) == int(exclude_id):
            continue
        if not store.has_room(w):
            continue                       # capacity-aware: skip full workers
        if store.is_local(w):
            usable.append(w)               # the local master is always usable
        elif w.get("status") == "ok":
            usable.append(w)               # remotes must be healthy
    if not usable:
        return None
    usable.sort(key=lambda w: (store.count_accounts_on(int(w["id"])), int(w["id"])))
    return usable[0]


def assign_new_account(account: str, exclude_id: int | None = None) -> dict | None:
    """Pick a worker for a new account AND record the affinity. Returns it."""
    worker = pick_for_new_account(exclude_id=exclude_id)
    if worker is not None:
        store.assign(account, int(worker["id"]))
    return worker


def runs_locally(account: str) -> bool:
    """True when this account's jobs run in-process (master), which is the case
    for every account until a remote worker owns it. The job runner uses this to
    decide 'run here' vs 'dispatch to a worker'."""
    w = worker_for_account(account)
    return store.is_local(w) if w is not None else True
