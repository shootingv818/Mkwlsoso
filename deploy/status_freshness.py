#!/usr/bin/env python3
"""Is contact presence LIVE, or a server-side snapshot? Narrow it by elimination.

WHAT IS ALREADY ESTABLISHED
---------------------------
Run 1 of this probe settled two things and exposed a hole in a third.

  * The host clock is EXACT. tweb's own serverTimeOffset read 0s, so the
    "check ntp" advice in an earlier commit was wrong -- now by measurement,
    not by inference. Every past-expiry `expires` is staleness, full stop.
  * The staleness is not held by our page. A full reload returned the identical
    newest last-seen, 19:54:12, 85 minutes old.
  * All 9 online contacts shared ONE expires value, 20:03:20. Nine independently
    computed presence values do not collide on the same second. That is a
    snapshot taken at one instant, around 19:58.

THE HOLE
--------
Round B failed with PEER_ID_INVALID and the verdict still announced "Every round
agreed with A". A round that ERRORED agrees with nothing -- that was a failure
being reported as a result, which is the one thing a diagnostic must never do.
Fixed here: failures are counted and named separately, and consensus is NEVER
claimed while any round is unresolved.

Two real mistakes caused that round to fail, both mine:

  * inputUser was hand-assembled from a stringified access_hash. Eitaa's own
    appUsersManager already knows how to build a valid input for a user, so the
    correct move was to ask it instead of reconstructing what it does.
  * contacts.getStatuses was never tried at all. It is the dedicated MTProto
    method for exactly this question -- current statuses for your contacts --
    and going straight to users.getUsers skipped it.

THE ROUNDS
----------
  A  warm getContacts, as the real code does it                the baseline
  B  users.getUsers, input built by Eitaa's own manager         does the server
                                                                hold newer?
  E  contacts.getStatuses                                       the purpose-built
                                                                method
  C  wait, then getContacts again, no reload                    does presence
                                                                arrive by push?
  F  count updateUserStatus events during that wait             does presence
                                                                flow AT ALL?
  D  reload, then getContacts                                   is our session
                                                                holding it?

Round F matters because "no update arrived" and "we could not observe updates"
are different findings, and reporting 0 for both would be its own small version
of the same lie. It says which one happened.

    cd ~/Mkwlsoso
    DISPLAY=:99 .venv/bin/python deploy/status_freshness.py --account 989991048633

Read-only. Nothing is sent, no settings change, nothing is written.
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

#: Shared helpers, installed once so every round is aggregated by the SAME code.
#: Rounds computed differently and then compared as equals would be worthless.
HELPERS_JS = r"""
() => {
  window.__mkwlSummarise = function (users, ms) {
    let newest = 0, newestExpires = 0;
    let online = 0, offline = 0, sentinel = 0, other = 0;
    for (let i = 0; i < users.length; i++) {
      const u = users[i]; if (!u) continue;
      const st = u.status || u;               // ContactStatus wraps it too
      const s = (st && st.status) ? st.status : st;
      if (!s || !s._) { other++; continue; }
      if (s._ === "userStatusOnline") {
        online++;
        if (typeof s.expires === "number" && s.expires > newestExpires)
          newestExpires = s.expires;
      } else if (s._ === "userStatusOffline") {
        if (typeof s.was_online === "number") {
          if (s.was_online < 86400) sentinel++;
          else { offline++; if (s.was_online > newest) newest = s.was_online; }
        } else other++;
      } else other++;
    }
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
  window.__mkwlErr = function (e) {
    try { return String((e && (e.type || e.error_message || e.message)) || e); }
    catch (x) { return "ERR"; }
  };
  return true;
}
"""

GET_CONTACTS_JS = r"""
async () => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
  const t0 = Date.now();
  try {
    const c = await AM.invokeApi("contacts.getContacts", { hash: 0 });
    return window.__mkwlSummarise((c && c.users) || [], Date.now() - t0);
  } catch (e) {
    return { ok: false, code: "getContacts:" + window.__mkwlErr(e) };
  }
}
"""

#: The input is built by Eitaa's OWN appUsersManager. Round 1 hand-assembled
#: inputUser from a stringified access_hash and got PEER_ID_INVALID for the whole
#: batch. The manager already holds the correct representation, so asking it is
#: both shorter and the only version that can be right.
GET_USERS_JS = r"""
async (ids) => {
  const AM = window.apiManager;
  const AUM = window.appUsersManager;
  if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
  if (!AUM) return { ok: false, code: "no appUsersManager" };

  const fn = AUM.getUserInput || AUM.getUserInputById || null;
  if (!fn) return { ok: false, code: "appUsersManager has no getUserInput" };

  const input = [];
  const bad = [];
  for (let i = 0; i < ids.length; i++) {
    try {
      const inp = fn.call(AUM, ids[i]);
      if (inp && inp._) input.push(inp); else bad.push(ids[i]);
    } catch (e) { bad.push(ids[i]); }
  }
  if (!input.length)
    return { ok: false, code: "no valid inputUser built for any of "
                             + ids.length + " ids" };
  const t0 = Date.now();
  try {
    const users = await AM.invokeApi("users.getUsers", { id: input });
    const r = window.__mkwlSummarise(users || [], Date.now() - t0);
    r.asked = input.length; r.unresolved = bad.length;
    return r;
  } catch (e) {
    return { ok: false, code: "users.getUsers:" + window.__mkwlErr(e)
                              + " (asked " + input.length + ")" };
  }
}
"""

#: The method that exists for precisely this question, and which round 1 skipped.
GET_STATUSES_JS = r"""
async () => {
  const AM = window.apiManager;
  if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi" };
  const t0 = Date.now();
  try {
    const res = await AM.invokeApi("contacts.getStatuses", {});
    const arr = Array.isArray(res) ? res : ((res && res.statuses) || []);
    return window.__mkwlSummarise(arr, Date.now() - t0);
  } catch (e) {
    return { ok: false, code: "contacts.getStatuses:" + window.__mkwlErr(e) };
  }
}
"""

#: Attach to whatever this build exposes for presence updates. Several candidates
#: are tried and the one that took is REPORTED, because "0 updates seen" and
#: "never managed to listen" are different answers and must not look alike.
WATCH_START_JS = r"""
() => {
  window.__mkwlSeen = 0;
  window.__mkwlHook = null;
  window.__mkwlAny = 0;

  // 1. tweb's event bus.
  try {
    const rs = window.rootScope;
    if (rs && typeof rs.addEventListener === "function") {
      rs.addEventListener("user_update", () => { window.__mkwlSeen++; });
      window.__mkwlHook = "rootScope.user_update";
    }
  } catch (e) { /* try the next one */ }

  // 2. Wrap the raw update pump. Also counts updates of ANY kind, which
  //    distinguishes "presence is not flowing" from "nothing is flowing".
  try {
    const AUM = window.apiUpdatesManager;
    if (AUM && typeof AUM.processUpdateMessage === "function"
            && !AUM.__mkwlWrapped) {
      const orig = AUM.processUpdateMessage.bind(AUM);
      AUM.processUpdateMessage = function (u) {
        try {
          window.__mkwlAny++;
          const s = JSON.stringify(u) || "";
          if (s.indexOf("updateUserStatus") !== -1) window.__mkwlSeen++;
        } catch (e) { /* counting must never break the client */ }
        return orig(u);
      };
      AUM.__mkwlWrapped = true;
      window.__mkwlHook = (window.__mkwlHook ? window.__mkwlHook + " + " : "")
                          + "apiUpdatesManager.processUpdateMessage";
    }
  } catch (e) { /* nothing to attach to */ }

  return { hook: window.__mkwlHook };
}
"""

WATCH_READ_JS = r"""
() => ({ hook: window.__mkwlHook || null,
         status_updates: window.__mkwlSeen || 0,
         any_updates: window.__mkwlAny || 0 })
"""


def hhmm(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(int(ts))) if ts else "-"


class Rounds:
    """Holds each round's outcome, keeping FAILED distinct from AGREED."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []
        self.base: int | None = None

    def add(self, tag: str, r: dict) -> None:
        r = r if isinstance(r, dict) else {"ok": False, "code": "bad reply"}
        self.rows.append((tag, r))
        if self.base is None and r.get("ok") and r.get("newest"):
            self.base = int(r["newest"])
        self._print(tag, r)

    def skip(self, tag: str, why: str) -> None:
        self.rows.append((tag, {"ok": False, "skipped": True, "code": why}))
        print(f"  {tag:<30} SKIPPED: {why}")

    def _print(self, tag: str, r: dict) -> None:
        if not r.get("ok"):
            print(f"  {tag:<30} FAILED: {r.get('code')}")
            return
        newest = int(r.get("newest") or 0)
        now = int(r.get("page_now") or time.time())
        age = f"{(now - newest) // 60}m" if newest else "-"
        delta = ""
        if self.base and newest:
            d = newest - self.base
            delta = f"   {'+' if d > 0 else ''}{d // 60}m vs A" if d else "   same as A"
        exp = int(r.get("newest_expires") or 0)
        print(f"  {tag:<30} newest {hhmm(newest):<9} ({age:>4} old)  "
              f"{r.get('seen', 0):>5} rows  {r.get('ms', 0):>5}ms  "
              f"expires {hhmm(exp)}{delta}")

    def verdict(self) -> None:
        ok = [(t, r) for t, r in self.rows if r.get("ok")]
        failed = [(t, r) for t, r in self.rows if not r.get("ok")]
        newer = [(t, r) for t, r in ok
                 if self.base and int(r.get("newest") or 0) > self.base]

        print("")
        print("  " + "=" * 70)
        print("  VERDICT")
        print("  " + "=" * 70)
        print(f"  rounds run: {len(self.rows)}    succeeded: {len(ok)}    "
              f"failed/skipped: {len(failed)}")
        for t, r in failed:
            print(f"    unresolved: {t} -> {r.get('code')}")

        if newer:
            t, r = max(newer, key=lambda x: int(x[1]["newest"]))
            gain = (int(r["newest"]) - (self.base or 0)) // 60
            print("")
            print(f"  FRESH DATA IS REACHABLE. Round {t} returned a last-seen {gain}m")
            print("  newer than the warm getContacts. The send order must be built")
            print(f"  from the round-{t} path immediately before sending.")
            return

        if failed:
            print("")
            print(f"  NO CONCLUSION YET. {len(ok)} round(s) matched the baseline, but")
            print(f"  {len(failed)} did not resolve, so 'presence is a server snapshot'")
            print("  is NOT established -- an unresolved round is not agreement.")
            print("  Fix the round(s) above and rerun before acting on this.")
            return

        print("")
        print("  Every round succeeded and every round matched. Presence really is")
        print("  a server-side snapshot: Eitaa reports statuses as of the account's")
        print("  last sync, not live. Tier 1 must then be relabelled honestly --")
        print("  'online as of the last sync' -- because calling it 'online right")
        print("  now' claims a freshness that has now been shown not to exist.")


async def run(args) -> int:
    from capture.pool import pool as session_pool
    from config import config
    from eitaa.driver import EitaaDriver

    print(f"status freshness probe — account {args.account}")
    print("  read-only: nothing is sent, nothing is written")
    R = Rounds()

    try:
        async with session_pool.lease(args.account,
                                      headed=config.HEADED_JOBS) as session:
            drv = EitaaDriver(session)
            await drv.open()
            if not await drv.is_logged_in():
                print("  ABORT: this account is not logged in.")
                return 2
            await drv.page.evaluate(HELPERS_JS)

            print("")
            print("  " + "-" * 70)
            a = await drv.page.evaluate(GET_CONTACTS_JS)
            R.add("A warm getContacts", a)
            if not a.get("ok"):
                R.verdict()
                return 1

            off = a.get("server_time_offset")
            print("")
            print(f"  tweb serverTimeOffset: "
                  + (f"{off}s — the host clock is exact, so every past-expiry "
                     f"value is staleness" if off == 0 else
                     f"{off}s" if off is not None else
                     "not exposed by this build"))
            print("")
            print("  " + "-" * 70)

            # E first: it is the method built for this question.
            R.add("E contacts.getStatuses",
                  await drv.page.evaluate(GET_STATUSES_JS))

            # B with the input built by Eitaa itself.
            if not await drv.ensure_contacts_list_bridge():
                R.skip("B users.getUsers", "contacts bridge unavailable")
            else:
                lst = await drv.bridge_contacts_list()
                ids = [int(c["peer_id"])
                       for c in (lst or {}).get("contacts", [])[:args.sample]
                       if str(c.get("peer_id") or "").isdigit()]
                if ids:
                    R.add(f"B users.getUsers ({len(ids)})",
                          await drv.page.evaluate(GET_USERS_JS, ids))
                else:
                    R.skip("B users.getUsers", "no usable contact ids")

            # F + C: watch the update stream across the wait.
            hook = await drv.page.evaluate(WATCH_START_JS)
            print("")
            print(f"  watching for presence updates via: "
                  f"{hook.get('hook') or 'NOTHING — could not attach'}")
            print(f"  waiting {args.wait}s on the SAME page ...")
            await asyncio.sleep(args.wait)
            watch = await drv.page.evaluate(WATCH_READ_JS)
            R.add(f"C getContacts +{args.wait}s",
                  await drv.page.evaluate(GET_CONTACTS_JS))

            print("")
            if not watch.get("hook"):
                print("  F update stream: COULD NOT OBSERVE. No hook attached, so")
                print("     this says nothing about whether presence flows.")
            else:
                print(f"  F update stream: {watch.get('status_updates', 0)} "
                      f"updateUserStatus, {watch.get('any_updates', 0)} updates of "
                      f"any kind in {args.wait}s")
                if not watch.get("any_updates"):
                    print("     No updates of ANY kind arrived, so the client is not")
                    print("     receiving a live stream here at all -- presence could")
                    print("     not reach us even if Eitaa were sending it.")
                elif not watch.get("status_updates"):
                    print("     Updates ARE flowing but none carried presence, so Eitaa")
                    print("     is not pushing contact statuses to this client.")

            print("")
            print("  reloading the page ...")
            try:
                await drv.page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(args.settle)
                await drv.page.evaluate(HELPERS_JS)
                R.add("D getContacts after reload",
                      await drv.page.evaluate(GET_CONTACTS_JS))
            except Exception as exc:  # noqa: BLE001
                R.skip("D after reload", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        R.verdict()
        return 1

    R.verdict()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Is contact presence live, or a server-side snapshot?")
    ap.add_argument("--account", required=True)
    ap.add_argument("--wait", type=int, default=45)
    ap.add_argument("--settle", type=int, default=8)
    ap.add_argument("--sample", type=int, default=30)
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
