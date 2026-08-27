#!/usr/bin/env python3
"""Contact status census — how many are online, recent, or long gone, and how fast.

WHY
---
Segmenting a broadcast (online first, then recently seen, then the rest) needs a
last-seen status per contact. The data is ALREADY on the wire and is being thrown
away twice, exactly as access_hash was before it:

  * eitaa/contacts_list.js calls contacts.getContacts, which returns full User
    objects, then keeps only {peer_id, access_hash, title, username, phone}.
    u.status is dropped.
  * bot/contacts_store.save() then whitelists {title, peer_id, access_hash}, so
    even if the JS kept status it would not survive being saved.

So no extra API call is needed for this. One getContacts -- the same one every
"Update Contacts" already makes -- carries everything.

WHAT THIS DOES
--------------
Read-only. It sends NOTHING, changes no settings, and writes no product state.
It makes ONE getContacts call, buckets every contact by status, and reports
COUNTS plus a precise timing breakdown so we know what the census costs before
wiring it into the panel.

No per-contact output: the account has too many for that to be readable.

    cd ~/Mkwlsoso
    DISPLAY=:99 .venv/bin/python deploy/contacts_status.py --account 989991048633

Add --save to write the raw last-seen timestamps for planning the send order.
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

#: One call, aggregated IN THE PAGE. Returning 40k user objects over the CDP
#: bridge would be slower than the API call itself, so the counting happens here
#: and only counts plus a compact array of integers comes back.
#:
#: Telegram/tweb status constructors, which Eitaa inherits:
#:   userStatusOnline      online right now (expires = until when)
#:   userStatusOffline     was_online = an EXACT unix timestamp
#:   userStatusRecently    seen recently, exact time hidden by privacy
#:   userStatusLastWeek    within a week, time hidden
#:   userStatusLastMonth   within a month, time hidden
#:   userStatusEmpty       nothing known (or fully hidden)
CENSUS_JS = r"""
async () => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };

  const t0 = Date.now();
  let users;
  try {
    const c = await AM.invokeApi("contacts.getContacts", { hash: 0 });
    users = (c && c.users) || [];
  } catch (e) {
    return { ok: false, code: "getContacts:" +
      String((e && (e.type || e.error_message || e.message)) || e) };
  }
  const api_ms = Date.now() - t0;

  const t1 = Date.now();
  const counts = {};
  const bump = (k) => { counts[k] = (counts[k] || 0) + 1; };
  const was_online = [];        // exact timestamps, only for userStatusOffline
  const online_now = [];        // expiry timestamps for userStatusOnline
  let addressable = 0, no_hash = 0, deleted = 0, bots = 0, self = 0;
  let no_status_field = 0;

  for (let i = 0; i < users.length; i++) {
    const u = users[i];
    if (!u || u.id == null) { bump("_malformed"); continue; }
    const f = u.pFlags || {};
    // Counted but excluded: these can never receive a broadcast, so leaving them
    // in the segment totals would inflate every tier.
    if (f.self) { self++; continue; }
    if (f.deleted) { deleted++; continue; }
    if (f.bot) { bots++; continue; }

    if (u.access_hash) addressable++; else no_hash++;

    const st = u.status;
    if (!st || !st._) { no_status_field++; bump("userStatusEmpty"); continue; }
    bump(st._);
    if (st._ === "userStatusOffline" && typeof st.was_online === "number") {
      was_online.push(st.was_online);
    } else if (st._ === "userStatusOnline" && typeof st.expires === "number") {
      online_now.push(st.expires);
    }
  }

  return { ok: true, api_ms: api_ms, count_ms: Date.now() - t1,
           raw_users: users.length, counts: counts,
           was_online: was_online, online_now: online_now,
           addressable: addressable, no_hash: no_hash,
           excluded: { deleted: deleted, bots: bots, self: self },
           no_status_field: no_status_field,
           server_now: Math.floor(Date.now() / 1000) };
}
"""

#: Send-order tiers. Deliberately derived from the EXACT was_online timestamp
#: where one exists, because "recently" as a category is up to three days wide
#: while the timestamp is precise. Tiers are checked in order.
TIERS: list[tuple[str, str]] = [
    ("online", "online right now"),
    ("seen_1h", "last seen within 1 hour"),
    ("seen_today", "last seen today (< 24h)"),
    ("seen_3d", "last seen within 3 days"),
    ("seen_week", "last seen within a week"),
    ("seen_month", "last seen within a month"),
    ("older", "last seen more than a month ago"),
    ("recently_hidden", "'recently', exact time hidden"),
    ("week_hidden", "'last week', exact time hidden"),
    ("month_hidden", "'last month', exact time hidden"),
    ("unknown", "no status at all (privacy or never seen)"),
]


def bucket(res: dict) -> dict:
    """Turn the page's raw counts into send tiers.

    The hidden tiers are kept SEPARATE from the timestamped ones on purpose. A
    contact reporting 'recently' with no timestamp is not the same fact as one
    that was provably seen 20 minutes ago, and merging them would quietly
    overstate how many people are actually reachable-and-active.
    """
    now = int(res.get("server_now") or time.time())
    counts = dict(res.get("counts") or {})
    out = {k: 0 for k, _ in TIERS}

    out["online"] = int(counts.get("userStatusOnline", 0))
    out["recently_hidden"] = int(counts.get("userStatusRecently", 0))
    out["week_hidden"] = int(counts.get("userStatusLastWeek", 0))
    out["month_hidden"] = int(counts.get("userStatusLastMonth", 0))
    out["unknown"] = int(counts.get("userStatusEmpty", 0))

    HOUR, DAY = 3600, 86400
    for ts in (res.get("was_online") or []):
        age = now - int(ts)
        if age < 0:
            age = 0                      # clock skew; treat as just-now
        if age <= HOUR:
            out["seen_1h"] += 1
        elif age <= DAY:
            out["seen_today"] += 1
        elif age <= 3 * DAY:
            out["seen_3d"] += 1
        elif age <= 7 * DAY:
            out["seen_week"] += 1
        elif age <= 30 * DAY:
            out["seen_month"] += 1
        else:
            out["older"] += 1
    return out


def fmt_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def report(res: dict, tiers: dict, timings: dict, total_contacts: int) -> None:
    say = print
    say("")
    say("=" * 66)
    say("CONTACT STATUS CENSUS")
    say("=" * 66)

    ex = res.get("excluded") or {}
    say(f"  contacts returned : {res.get('raw_users', 0):,}")
    say(f"  usable            : {total_contacts:,}"
        f"   (excluded: {ex.get('deleted', 0)} deleted, "
        f"{ex.get('bots', 0)} bots, {ex.get('self', 0)} self)")
    say(f"  addressable       : {res.get('addressable', 0):,} have access_hash"
        + (f"   ⚠️ {res['no_hash']:,} do NOT" if res.get("no_hash") else ""))

    say("")
    say("  SEND ORDER — tiers, in the order they should be sent")
    say("  " + "-" * 62)
    cum = 0
    width = max(len(k) for k, _ in TIERS)
    for key, label in TIERS:
        n = tiers.get(key, 0)
        if not n:
            continue
        cum += n
        pct = (n * 100.0 / total_contacts) if total_contacts else 0.0
        cpct = (cum * 100.0 / total_contacts) if total_contacts else 0.0
        say(f"  {key:<{width}}  {n:>7,}  {pct:5.1f}%   cumulative {cum:>7,}"
            f" ({cpct:5.1f}%)   {label}")
    missing = total_contacts - cum
    if missing:
        say(f"  {'(unaccounted)':<{width}}  {missing:>7,}")

    # The three numbers actually asked for.
    active = tiers["online"] + tiers["seen_1h"] + tiers["seen_today"]
    recent = (tiers["seen_3d"] + tiers["seen_week"]
              + tiers["recently_hidden"] + tiers["week_hidden"])
    gone = (tiers["seen_month"] + tiers["older"] + tiers["month_hidden"]
            + tiers["unknown"])
    say("")
    say("  THE HEADLINE")
    say("  " + "-" * 62)
    say(f"  online / today      : {active:>7,}")
    say(f"  recent (3d-1w)      : {recent:>7,}")
    say(f"  long gone / unknown : {gone:>7,}")

    hidden = (tiers["recently_hidden"] + tiers["week_hidden"]
              + tiers["month_hidden"] + tiers["unknown"])
    if total_contacts:
        say("")
        say(f"  PRIVACY: {hidden:,} of {total_contacts:,} "
            f"({hidden * 100.0 / total_contacts:.1f}%) give no exact last-seen "
            f"time.")
        if hidden * 2 > total_contacts:
            say("  More than half hide it, so tiering will be coarse for most of")
            say("  the list. Worth knowing BEFORE building a send order on it.")

    say("")
    say("  SPEED")
    say("  " + "-" * 62)
    for k, v in timings.items():
        say(f"  {k:<22} {v:6.2f}s")
    say(f"  {'getContacts (in page)':<22} {res.get('api_ms', 0) / 1000:6.2f}s")
    say(f"  {'counting (in page)':<22} {res.get('count_ms', 0) / 1000:6.2f}s")
    if total_contacts and res.get("api_ms"):
        rate = total_contacts / max(0.001, res["api_ms"] / 1000)
        say(f"  -> {rate:,.0f} contacts/second on the API call")
    say("")
    say("  The census itself is ONE getContacts call. Everything else above is")
    say("  browser startup, which the panel already pays for on any job.")


async def run(args) -> int:
    from capture.pool import pool as session_pool
    from config import config
    from eitaa.driver import EitaaDriver

    timings: dict[str, float] = {}
    t_start = time.time()

    print(f"contact status census — account {args.account}")
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
            logged = await drv.is_logged_in()
            timings["login check"] = time.time() - t0
            if not logged:
                print("  ABORT: this account is not logged in.")
                return 2

            t0 = time.time()
            res = await drv.page.evaluate(CENSUS_JS)
            timings["census round trip"] = time.time() - t0
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        print("  Check DISPLAY=:99, xvfb, and that the account session is live.")
        return 1

    if not (res or {}).get("ok"):
        print(f"  census failed: {(res or {}).get('code')}")
        return 1

    timings["total"] = time.time() - t_start
    ex = res.get("excluded") or {}
    total = int(res.get("raw_users", 0)) - sum(
        int(ex.get(k, 0)) for k in ("deleted", "bots", "self"))
    tiers = bucket(res)
    report(res, tiers, timings, max(0, total))

    if args.save:
        out = Path(config.ARTIFACTS_DIR) / (
            f"contacts_status_{args.account}_{time.strftime('%Y%m%d-%H%M%S')}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"account": args.account, "at": time.time(),
                   "counts": res.get("counts"), "tiers": tiers,
                   "addressable": res.get("addressable"),
                   "no_hash": res.get("no_hash"), "excluded": ex,
                   "server_now": res.get("server_now"),
                   "was_online": res.get("was_online"),
                   "timings": timings}
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        print("")
        print(f"  raw last-seen timestamps saved: {out}")
        print("  (that file is what a segmented send order would be built from)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Count contacts by last-seen status. Read-only.")
    ap.add_argument("--account", required=True, help="e.g. 989991048633")
    ap.add_argument("--save", action="store_true",
                    help="write the raw last-seen timestamps for planning")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
