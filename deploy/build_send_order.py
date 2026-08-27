#!/usr/bin/env python3
"""Build the broadcast send order for one account, from Eitaa's live data.

WHAT IT DOES
------------
One contacts.getContacts call (the same one "Update Contacts" already makes),
then tiers every contact by the status Eitaa itself reported and writes the
ordered list to disk for a sender to consume.

    1. online          online right now
    2. today           seen inside 24h, exact timestamp
    3. recently        Eitaa says "recently", no time given
    4. week_or_month   Eitaa says "last week" or "last month", no time given
    5. long_ago        no usable signal

The ordering rules and the reasoning behind them live in eitaa/send_order.py.
This file is only the live wiring: browser, one API call, report, save.

Read-only with respect to the account: it sends NOTHING, changes no settings,
joins nothing. The only side effect is a JSON file under ARTIFACTS_DIR.

    cd ~/Mkwlsoso
    DISPLAY=:99 .venv/bin/python deploy/build_send_order.py --account 989991048633

Add --limit N to preview just the head of the order, and --no-save to skip
writing. Add --show-titles to print contact names (off by default so the output
stays readable and pasteable).

WHY IT PRINTS SO MUCH
---------------------
The tiering rests on a measured claim -- that Eitaa gives exact timestamps only
inside 24h. If that ever changes, the tiers silently get worse rather than
breaking, which is the dangerous kind of failure. So this prints the reason
breakdown and any warning every time, instead of only the tier totals that would
still look plausible.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from eitaa.send_order import TIER_LABEL, TIERS, build_order, groups  # noqa: E402


def artifacts_dir() -> Path:
    """Resolve ARTIFACTS_DIR to an absolute path anchored at the repo.

    config.ARTIFACTS_DIR defaults to the RELATIVE "./artifacts", so a script
    launched from $HOME writes to ~/artifacts and the file is then unfindable
    from the repo. That has already cost real debugging time, so this pins it.
    """
    from config import config
    d = Path(config.ARTIFACTS_DIR)
    if not d.is_absolute():
        d = Path(_ROOT) / d
    return d.resolve()


def report(plan: dict, timings: dict, raw: int, skipped: int,
           source: str = "live", live_error: dict | None = None) -> None:
    say = print
    total = plan["total"]
    say("")
    say("=" * 66)
    say("SEND ORDER")
    say("=" * 66)

    # The live-bridge failure is repeated HERE, inside the report, because the
    # first fallback run printed it before the report and it scrolled away -- the
    # single most important line was lost, leaving a tier-1 warning with no
    # stated cause. A diagnosis has to travel with the result it explains.
    if source != "live":
        say(f"  SOURCE: {source} — the LIVE presence bridge did not run.")
        if live_error:
            say(f"    reason: {live_error.get('code')}")
            for e in (live_error.get("errors") or [])[:6]:
                say(f"    error : {e}")
            for k in ("connected", "store_ids", "seeded_users", "id_source",
                      "batches", "failed_batches", "unbuildable_ids"):
                if live_error.get(k) is not None:
                    say(f"    {k:<16}: {live_error[k]}")
        else:
            say("    reason: not recorded — this is itself a bug, report it")
        say("")
    else:
        say("  SOURCE: live presence bridge (users.getUsers)")
    say(f"  contacts returned  : {raw:,}")
    say(f"  addressable        : {total:,}"
        + (f"   ({skipped:,} skipped: deleted/bot/self/no access_hash)"
           if skipped else ""))

    say("")
    say("  TIERS — in the order they will be sent")
    say("  " + "-" * 62)
    width = max(len(k) for k, _ in TIERS)
    cum = 0
    pres = plan.get("presence") or {}
    for key, label in TIERS:
        # Tier 1's label is earned, not fixed. If freshness was not proven, the
        # report must say so on the line itself, where it cannot be missed.
        if key == "online" and pres.get("tier1_label"):
            label = pres["tier1_label"]
        n = plan["tier_counts"].get(key, 0)
        cum += n
        pct = (n * 100.0 / total) if total else 0.0
        cpct = (cum * 100.0 / total) if total else 0.0
        say(f"  {key:<{width}}  {n:>6,}  {pct:5.1f}%   cumulative {cum:>6,}"
            f" ({cpct:5.1f}%)   {label}")

    say("")
    say("  WHY EACH CONTACT LANDED WHERE IT DID")
    say("  " + "-" * 62)
    say("  (several reasons share a tier; the distinction is kept, it just")
    say("   does not change the order)")
    for reason, n in sorted(plan["reason_counts"].items(),
                            key=lambda kv: (-kv[1], kv[0])):
        say(f"    {reason:<22} {n:>6,}")

    say("")
    say("  SEND GROUPS")
    say("  " + "-" * 62)
    for how_many in (2, 3):
        parts = "  ".join(f"{g['group']}={g['count']:,}"
                          for g in groups(plan, how_many))
        say(f"    {how_many} groups: {parts}")

    if plan.get("observations"):
        say("")
        say("  OBSERVATIONS — expected, but worth knowing")
        say("  " + "-" * 62)
        for o in plan["observations"]:
            say(f"    - {o}")

    if plan["warnings"]:
        say("")
        say("  WARNINGS — something the design depends on may have changed")
        say("  " + "-" * 62)
        for w in plan["warnings"]:
            say(f"    ! {w}")
    else:
        say("")
        say("  no warnings: every status was one Eitaa is known to send, and no")
        say("  exact timestamp appeared far past the 24-hour window, so the")
        say("  premise these tiers are built on still holds.")

    say("")
    say("  SPEED")
    say("  " + "-" * 62)
    for k, v in timings.items():
        say(f"  {k:<22} {v:6.2f}s")


def preview(plan: dict, limit: int, show_titles: bool) -> None:
    if limit <= 0:
        return
    print("")
    print(f"  HEAD OF THE ORDER (first {limit})")
    print("  " + "-" * 62)
    for i, e in enumerate(plan["ordered"][:limit], 1):
        who = e.get("title", "") if show_titles else f"peer {e.get('peer_id')}"
        seen = ""
        if isinstance(e.get("was_online"), (int, float)) and e["was_online"] > 86400:
            seen = time.strftime("  last seen %H:%M",
                                 time.localtime(e["was_online"]))
        print(f"  {i:>4}. [{e['tier']:<13}] {str(who)[:34]:<34} "
              f"{e['reason']}{seen}")


async def run(args) -> int:
    from capture.pool import pool as session_pool
    from config import config
    from eitaa.driver import EitaaDriver

    timings: dict[str, float] = {}
    live_error: dict | None = None
    t_start = time.time()

    print(f"build send order — account {args.account}")
    print("  read-only: nothing is sent, no settings change")

    try:
        t0 = time.time()
        async with session_pool.lease(args.account,
                                      headed=config.HEADED_JOBS) as session:
            timings["browser lease"] = time.time() - t0
            drv = EitaaDriver(session)

            t0 = time.time()
            await drv.open()
            timings["load Eitaa web"] = time.time() - t0

            t0 = time.time()
            if not await drv.is_logged_in():
                print("  ABORT: this account is not logged in.")
                return 2
            timings["login check"] = time.time() - t0

            # LIVE presence first. contacts.getContacts was measured returning a
            # snapshot frozen at session start for 105 minutes, so it is the
            # fallback, not the default.
            t0 = time.time()
            live = await drv.bridge_live_presence(batch=args.batch)
            timings["live presence"] = time.time() - t0

            res, source = live, "live"
            if not live.get("ok"):
                # Kept for the streaming view, but the report repeats it so it
                # cannot be lost to scrolling.
                live_error = {k: live.get(k) for k in (
                    "code", "errors", "connected", "store_ids", "seeded_users",
                    "id_source", "batches", "failed_batches",
                    "unbuildable_ids")}
                print("")
                print("  live presence bridge FAILED — falling back to the frozen")
                print(f"  contacts.getContacts path. reason: {live.get('code')}")
                t0 = time.time()
                res, source = await drv.bridge_contacts_list(), "snapshot"
                timings["getContacts fallback"] = time.time() - t0
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        print("  Check DISPLAY=:99, xvfb, and that the account session is live.")
        return 1

    if res is None:
        print("  FAILED: no contacts source worked at all.")
        return 1
    if not res.get("ok"):
        print(f"  FAILED: {res.get('code')}")
        for e in (res.get("errors") or [])[:5]:
            print(f"    error: {e}")
        return 1

    if source == "live":
        contacts = res.get("roster") or []
        now = int(res.get("server_now") or time.time())
        presence_age = res.get("presence_age_sec")
        presence_coverage = res.get("coverage_pct")
        presence_source = "users.getUsers (live)"
        raw, skipped = int(res.get("store_ids") or 0), 0
        print("")
        print("  LIVE PRESENCE — the evidence, before any tiering")
        print("  " + "-" * 62)
        print(f"    socket verified     : "
              f"{'yes' if res.get('connected') else 'NO'}"
              f"   (updates.getState in {res.get('getstate_ms', '?')}ms)")
        off = res.get("clock_offset")
        print(f"    host vs server clock: "
              + (f"{off:+d}s (measured, not assumed)" if off is not None
                 else "unknown"))
        un = res.get("unhide") or {}
        was = (un.get("before") or {}).get("state")
        print(f"    page visibility     : was {was!r} -> "
              f"{(un.get('after') or {}).get('state', '?')!r}"
              f"   (hidden pages get no updates from tweb)")
        print(f"    marked self online  : {res.get('marked_online')}")
        print(f"    roster              : {res.get('count', 0):,} contacts"
              f"   (ids from: {res.get('id_source')})")
        print(f"    statuses REFRESHED  : {res.get('fresh_count', 0):,} of "
              f"{res.get('count', 0):,}  = {res.get('coverage_pct', 0)}%"
              f"   in {res.get('batches', 0)} batches ({res.get('fetch_ms', 0)}ms)")
        if res.get("reply_shape"):
            print(f"    reply shape         : {res['reply_shape']}")
        if res.get("failed_batches"):
            print(f"    FAILED batches      : {res['failed_batches']} — those "
                  f"contacts keep their SNAPSHOT status, not a fresh one")
        if res.get("empty_replies"):
            print(f"    empty replies       : {res['empty_replies']} "
                  f"(shape: {res.get('empty_reply_shape')})")
        if res.get("unbuildable_ids"):
            print(f"    unbuildable ids     : {res['unbuildable_ids']}")
        print(f"    newest last-seen    : "
              f"{time.strftime('%H:%M:%S', time.localtime(res['newest_was_online'])) if res.get('newest_was_online') else '-'}"
              f"   age {presence_age if presence_age is not None else '?'}s")
        for e in (res.get("errors") or [])[:5]:
            print(f"    error: {e}")
    else:
        contacts = res.get("contacts") or []
        now = int(res.get("server_now") or time.time())
        presence_age, presence_coverage = None, None
        presence_source = "contacts.getContacts (snapshot)"
        raw = int(res.get("raw") or 0)
        skipped = int(res.get("skipped") or 0)
        if contacts and "status" not in contacts[0]:
            print("  ABORT: the page returned contacts WITHOUT a status field.")
            print("  That means an older contacts_list.js is cached in the browser")
            print("  session. Restart the browser session so the updated bridge")
            print("  loads, otherwise every contact would be filed as long_ago.")
            return 1

    t0 = time.time()
    plan = build_order(contacts, now=now, presence_age=presence_age,
                       presence_source=presence_source,
                       presence_coverage_pct=presence_coverage)
    timings["tiering"] = time.time() - t0
    timings["total"] = time.time() - t_start

    report(plan, timings, raw=raw, skipped=skipped, source=source,
           live_error=live_error)
    preview(plan, args.limit, args.show_titles)

    if not args.no_save:
        out = artifacts_dir() / (
            f"send_order_{args.account}_{time.strftime('%Y%m%d-%H%M%S')}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "account": args.account, "built_at": time.time(), "server_now": now,
            "source": source, "presence": plan.get("presence", {}),
            "live_evidence": {k: res.get(k) for k in (
                "connected", "clock_offset", "getstate_ms", "batches",
                "failed_batches", "unbuildable_ids", "marked_online",
                "newest_was_online", "presence_age_sec", "id_source",
                "store_ids", "errors")}
            if source == "live" else {},
            # Saved even on fallback. The first fallback run left NO record of
            # why, which made the tier-1 warning unexplainable after the fact.
            "live_failure": live_error or {},
            "tier_counts": plan["tier_counts"],
            "reason_counts": plan["reason_counts"],
            "warnings": plan["warnings"],
            "observations": plan.get("observations", []),
            "clock": plan.get("clock", {}),
            "groups": {"2": groups(plan, 2), "3": groups(plan, 3)},
            # was_online and expires are kept deliberately. The first run of
            # this script raised a question about one boundary contact that
            # could not be answered from the saved file because only the tier
            # was stored, forcing a rerun. Storing the raw values makes the
            # same question answerable offline next time.
            "order": [
                {"peer_id": e.get("peer_id"), "access_hash": e.get("access_hash"),
                 "title": e.get("title"), "tier": e["tier"], "reason": e["reason"],
                 "was_online": e.get("was_online"), "expires": e.get("expires")}
                for e in plan["ordered"]
            ],
        }
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print("")
        print(f"  saved: {out}")
        print("  (absolute path on purpose — ARTIFACTS_DIR is relative by")
        print("   default, which is how an earlier artifact went missing)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the broadcast send order from Eitaa's live statuses.")
    ap.add_argument("--account", required=True, help="e.g. 989991048633")
    ap.add_argument("--limit", type=int, default=15,
                    help="how many head-of-order rows to print (0 = none)")
    ap.add_argument("--show-titles", action="store_true",
                    help="print contact names instead of peer ids")
    ap.add_argument("--no-save", action="store_true",
                    help="report only, write no file")
    ap.add_argument("--batch", type=int, default=100,
                    help="users.getUsers batch size (default 100)")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
