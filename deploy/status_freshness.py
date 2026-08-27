#!/usr/bin/env python3
"""Is contacts.getContacts returning CURRENT last-seen statuses, or stale ones?

WHY
---
Three runs against the same account, spanning 78 minutes, all reported the same
newest last-seen time:

    census 19:59   newest was_online 19:54
    build  21:04   newest was_online 19:54   online `expires` behind by 6m
    build  21:12   newest was_online 19:54   online `expires` behind by 1h 9m

Two things are wrong with that. On an account where 208 contacts were seen
inside 24 hours, the most-recent last-seen cannot stay frozen for 78 minutes.
And "expires is behind" grew from 6m to 1h9m during 8 minutes of wall clock,
which a fixed host-clock offset cannot do -- a constant offset stays constant.
An earlier version of this diagnosis blamed host clock skew and suggested ntp.
That was wrong: 1h9m before 21:12 is 20:03, which is when this browser session
first fetched contacts. The numbers are not skewed, they are OLD.

That matters more than any tiering detail. "Online right now" that is actually
"was online at 19:54" makes tier 1 worthless, and it fails INVISIBLY -- the
report looks healthy either way, which is why it took three runs to notice.

WHAT THIS DOES
--------------
Read-only. Four rounds against one account, each reporting the newest last-seen
it can see, so the cause is narrowed by elimination rather than guessed:

  A  getContacts on the warm page, as the real code does it   -> the baseline
  B  users.getUsers for a sample of contacts, by explicit id  -> is the SERVER
                                                                 holding newer?
  C  wait, then getContacts again, without reloading          -> do statuses
                                                                 arrive by push
                                                                 (updateUserStatus)?
  D  reload the page, then getContacts again                  -> is it the
                                                                 session/page
                                                                 that is stale?

Whichever round returns a NEWER timestamp than A is the fix. If all four agree,
the staleness is on Eitaa's side and tier 1 has to be honestly relabelled
instead of pretending to be live.

It also reads tweb's own server-time offset if exposed, which settles the clock
question directly rather than by inference.

    cd ~/Mkwlsoso
    DISPLAY=:99 .venv/bin/python deploy/status_freshness.py --account 989991048633

Nothing is sent. No settings change. Nothing is written.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: Aggregate in the page and return a handful of numbers. Shipping 1,000 user
#: objects over CDP four times would cost more than the API calls being measured.
PROBE_JS = r"""
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
  return summarise(users, Date.now() - t0);
}
"""

#: Ask the server for specific users by id. If getContacts is being served from
#: a cache, this path may not be, and a newer status here localises the problem.
GETUSERS_JS = r"""
async (ids) => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
  const input = ids.map(x => ({ _: "inputUser", user_id: x.id,
                                access_hash: x.access_hash }));
  const t0 = Date.now();
  let users;
  try {
    users = await AM.invokeApi("users.getUsers", { id: input });
  } catch (e) {
    return { ok: false, code: "users.getUsers:" +
      String((e && (e.type || e.error_message || e.message)) || e) };
  }
  return summarise(users || [], Date.now() - t0);
}
"""

#: Shared aggregator, installed once so the two probes above cannot drift apart
#: and report differently-computed numbers.
SUMMARISE_JS = r"""
() => {
  window.summarise = function (users, ms) {
    let newest = 0, online = 0, offline = 0, sentinel = 0, other = 0;
    let newestExpires = 0;
    for (let i = 0; i < users.length; i++) {
      const u = users[i]; if (!u) continue;
      const st = u.status;
      if (!st || !st._) { other++; continue; }
      if (st._ === "userStatusOnline") {
        online++;
        if (typeof st.expires === "number" && st.expires > newestExpires)
          newestExpires = st.expires;
      } else if (st._ === "userStatusOffline") {
        if (typeof st.was_online === "number") {
          if (st.was_online < 86400) sentinel++;
          else { offline++; if (st.was_online > newest) newest = st.was_online; }
        } else other++;
      } else other++;
    }
    // tweb keeps the measured difference between its clock and the server's.
    // Reading it settles the clock question directly instead of inferring it.
    let offset = null;
    try {
      const m = window.serverTimeManager || window.timeManager;
      if (m && typeof m.serverTimeOffset === "number") offset = m.serverTimeOffset;
    } catch (e) { /* not exposed in this build */ }
    return { ok: true, ms: ms, seen: users.length, newest: newest,
             newest_expires: newestExpires, online: online, offline: offline,
             sentinel: sentinel, other: other,
             page_now: Math.floor(Date.now() / 1000),
             server_time_offset: offset };
  };
  return true;
}
"""


def hhmm(ts) -> str:
    if not ts:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(int(ts)))


def line(tag: str, r: dict, baseline: int | None) -> None:
    if not r.get("ok"):
        print(f"  {tag:<26} FAILED: {r.get('code')}")
        return
    newest = int(r.get("newest") or 0)
    now = int(r.get("page_now") or time.time())
    age = f"{(now - newest) // 60}m" if newest else "-"
    delta = ""
    if baseline is not None and newest and baseline:
        d = newest - baseline
        delta = f"  {'+' if d > 0 else ''}{d // 60}m vs A" if d else "  same as A"
    exp = int(r.get("newest_expires") or 0)
    expnote = f"  expires {hhmm(exp)}" if exp else ""
    print(f"  {tag:<26} newest {hhmm(newest):<10} ({age:>5} old)"
          f"  {r.get('seen', 0):>5} users  {r.get('ms', 0):>5}ms{expnote}{delta}")


async def run(args) -> int:
    from capture.pool import pool as session_pool
    from config import config
    from eitaa.driver import EitaaDriver

    print(f"status freshness probe — account {args.account}")
    print("  read-only: nothing is sent, nothing is written")

    try:
        async with session_pool.lease(args.account,
                                      headed=config.HEADED_JOBS) as session:
            drv = EitaaDriver(session)
            await drv.open()
            if not await drv.is_logged_in():
                print("  ABORT: this account is not logged in.")
                return 2
            await drv.page.evaluate(SUMMARISE_JS)

            rounds: dict[str, dict] = {}

            print("")
            print("  " + "-" * 70)
            a = await drv.page.evaluate(PROBE_JS)
            rounds["A"] = a
            line("A warm getContacts", a, None)
            if not a.get("ok"):
                return 1
            base = int(a.get("newest") or 0)

            off = a.get("server_time_offset")
            print("")
            if off is None:
                print("  tweb does not expose serverTimeOffset in this build, so the")
                print("  clock question is answered by round D instead.")
            else:
                print(f"  tweb's own server time offset: {off}s")
                print("  (this is the real host-vs-Eitaa clock difference. If it is")
                print("   small, the earlier 'check ntp' advice was wrong and the")
                print("   numbers were stale rather than skewed.)")

            # B -- ask for specific users by id.
            if not await drv.ensure_contacts_list_bridge():
                print("  (could not install the contacts bridge for round B)")
            else:
                lst = await drv.bridge_contacts_list()
                sample = [{"id": int(c["peer_id"]), "access_hash": c["access_hash"]}
                          for c in (lst or {}).get("contacts", [])[:args.sample]
                          if c.get("peer_id") and c.get("access_hash")]
                print("")
                print("  " + "-" * 70)
                if sample:
                    rounds["B"] = await drv.page.evaluate(GETUSERS_JS, sample)
                    line(f"B users.getUsers ({len(sample)})", rounds["B"], base)
                else:
                    print("  B users.getUsers            skipped: no sample available")

            # C -- same page, later. Tests whether statuses arrive by push.
            print(f"  waiting {args.wait}s on the SAME page (push updates?)")
            await asyncio.sleep(args.wait)
            rounds["C"] = await drv.page.evaluate(PROBE_JS)
            line(f"C getContacts +{args.wait}s", rounds["C"], base)

            # D -- reload, which discards anything the page was holding.
            print("  reloading the page ...")
            try:
                await drv.page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(args.settle)
                await drv.page.evaluate(SUMMARISE_JS)
                rounds["D"] = await drv.page.evaluate(PROBE_JS)
                line("D getContacts after reload", rounds["D"], base)
            except Exception as exc:  # noqa: BLE001
                print(f"  D reload FAILED: {type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return 1

    print("")
    print("  " + "=" * 70)
    print("  HOW TO READ THIS")
    print("  " + "=" * 70)
    best_tag, best_newest = "A", base
    for tag, r in rounds.items():
        n = int((r or {}).get("newest") or 0)
        if n > best_newest:
            best_tag, best_newest = tag, n
    if best_newest > base:
        gain = (best_newest - base) // 60
        print(f"  Round {best_tag} returned a last-seen {gain}m NEWER than the warm")
        print("  getContacts. So fresh data IS reachable and the send order must")
        print(f"  use the round-{best_tag} path before sending, not the warm one.")
    else:
        print("  Every round agreed with A. The staleness is not something our")
        print("  session is doing, so it is on Eitaa's side: getContacts reports")
        print("  statuses as of when the account last synced, not live ones.")
        print("  Then tier 1 must NOT be called 'online right now' -- it is")
        print("  'online as of the last sync', and the send order should say so")
        print("  rather than implying a freshness it does not have.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check whether contact last-seen statuses are live or stale.")
    ap.add_argument("--account", required=True)
    ap.add_argument("--wait", type=int, default=45,
                    help="seconds to wait in round C (default 45)")
    ap.add_argument("--settle", type=int, default=8,
                    help="seconds to let the page settle after reload")
    ap.add_argument("--sample", type=int, default=30,
                    help="how many contacts to re-ask for in round B")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
