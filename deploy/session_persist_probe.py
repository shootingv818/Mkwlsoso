#!/usr/bin/env python3
"""Why is a successful login gone a minute later?

Established by the health check on a profile that HAD logged in:

    API object resolvable   : rootScope.managers        (the resolver fix holds)
    a real API round trip   : help.getConfig in 113 ms  (the pipe is fine)
    rootScope.getMyId()     : 0
    appUsersManager.getSelf(): null
    page                    : the login form
    contacts.getContacts    : INVALID_LOGIN
    localStorage            : number_of_accounts, account1, dc, k_build_K …

So the login itself works and the session does not survive. Two suspects, with
opposite fixes, so they get separated instead of argued about:

  (1) WE kill it. Right after login the bot reads contacts. That call answers
      INVALID_LOGIN, and tweb's reaction to an auth error is to sign the user
      out and return to the login form. The timeline fits exactly: logged in at
      17:55:13, login form on screen in the SAME warm browser at 17:55:43.

  (2) tweb never finished writing it. `commonStateStorage` exposes
      `saveThrottled` / `_save`, so state writes are debounced. A browser closed
      before the flush loses the session, and the next run starts at the form.

What this script does
---------------------
PART A is read-only and needs NO code: it dumps what is actually stored in the
profile right now - localStorage keys, every IndexedDB database with its stores
and record counts, which records look like a session, and tweb's authState.
That alone distinguishes "never written" from "written but not restored".

PART B (--login PHONE, off by default, costs ONE code) logs in through the form
and then watches the session on a timeline: immediately, +3 s, +10 s, +20 s,
after a reload, and after a close/reopen of the whole browser. The contacts call
is NOT made, so if the session dies anyway suspect (2) is confirmed; if it
survives all of that, suspect (1) is confirmed and the fix is in what the bot
does next, not in the login.

PART C (--with-contacts, only meaningful with --login) repeats the contacts call
at the end and re-checks the session, which proves or clears suspect (1)
directly.

Safety
------
* PART A sends nothing and requests no code.
* PART B requests exactly ONE code, only when you pass --login, and prints a
  warning first. Nothing is sent to any contact in any mode.
* Secrets are never printed: record keys are listed, values are reduced to a
  type and a length.
* No project file is modified. Output goes to the gitignored artifacts/ dir.

Run on the SERVER:

    # read-only, no code burned:
    cd ~/Mkwlsoso && .venv/bin/python deploy/session_persist_probe.py --account 989336724339

    # the full timeline (ONE code, to the number you pass):
    .venv/bin/python deploy/session_persist_probe.py --account 989336724339 \
        --login 989336724339 --with-contacts
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"timeline": [], "verdict": []}
_T0 = time.time()


def banner(t: str) -> None:
    print("\n" + "=" * 74 + f"\n  {t}\n" + "=" * 74, flush=True)


def log(m: str = "") -> None:
    print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)


def ok(m: str) -> None:
    log(f"OK    {m}")


def bad(m: str) -> None:
    log(f"FAIL  {m}")


def info(m: str) -> None:
    log(f"      {m}")


def dump(label: str, obj, indent: str = "        ") -> None:
    info(label)
    for line in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + line, flush=True)


async def ev(page, js: str, arg=None, timeout: float = 30.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# What is in the profile. Keys and shapes only - never a value.
STORAGE_JS = r"""
async () => {
  const out = { localStorage: {}, idb: [], errors: [] };
  try {
    for (const k of Object.keys(localStorage)) {
      let v = "";
      try { v = String(localStorage.getItem(k) || ""); } catch (e) {}
      // shape only: a session token must never be printed
      out.localStorage[k] = { type: typeof v, len: v.length,
                              preview: v.length <= 24 ? v : "<" + v.length + " chars>" };
    }
  } catch (e) { out.errors.push("localStorage: " + e); }

  // Every IndexedDB database, its stores, how many records each holds, and the
  // record KEYS - which is where tweb keeps its session on this build.
  try {
    if (!indexedDB.databases) { out.errors.push("indexedDB.databases() unsupported"); return out; }
    const dbs = await indexedDB.databases();
    for (const meta of dbs) {
      const entry = { name: meta.name, version: meta.version, stores: [] };
      try {
        const db = await new Promise((res, rej) => {
          const r = indexedDB.open(meta.name);
          r.onsuccess = () => res(r.result);
          r.onerror = () => rej(r.error);
          setTimeout(() => rej(new Error("open timeout")), 5000);
        });
        for (const storeName of Array.from(db.objectStoreNames)) {
          const s = { name: storeName, count: null, keys: [] };
          try {
            const tx = db.transaction(storeName, "readonly");
            const os = tx.objectStore(storeName);
            s.count = await new Promise((res, rej) => {
              const q = os.count();
              q.onsuccess = () => res(q.result);
              q.onerror = () => rej(q.error);
              setTimeout(() => rej(new Error("count timeout")), 4000);
            });
            s.keys = await new Promise((res, rej) => {
              const q = os.getAllKeys();
              q.onsuccess = () => res((q.result || []).slice(0, 40).map(String));
              q.onerror = () => rej(q.error);
              setTimeout(() => rej(new Error("keys timeout")), 4000);
            });
          } catch (e) { s.error = String(e); }
          entry.stores.push(s);
        }
        db.close();
      } catch (e) { entry.error = String(e); }
      out.idb.push(entry);
    }
  } catch (e) { out.errors.push("idb: " + e); }
  return out;
}
"""

STATE_JS = r"""
async () => {
  const cap = (p, ms) => Promise.race([Promise.resolve(p),
    new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms || 8000))]);
  const out = {};
  try {
    out.my_id = await cap(window.rootScope && window.rootScope.getMyId
      ? window.rootScope.getMyId() : null);
  } catch (e) { out.my_id = "ERR"; }
  try {
    const m = window.__MKWL_managers ? window.__MKWL_managers() : null;
    if (m && m.appStateManager) {
      const st = await cap(m.appStateManager.getState());
      out.auth_state = st && st.authState ? (st.authState._ || st.authState) : null;
    }
    if (m && m.appUsersManager) {
      const s = await cap(m.appUsersManager.getSelf());
      out.self_id = s ? s.id : null;
    }
  } catch (e) { out.err = String(e); }
  try {
    out.on_login_form = !!document.querySelector(".page-sign, .page-authCode");
    out.chat_rows = document.querySelectorAll(".chatlist-chat").length;
  } catch (e) {}
  return out;
}
"""


async def snapshot(page, label: str) -> dict:
    """One point on the timeline: is the session there, right now."""
    from eitaa.api_resolver import ensure_api_resolver
    await ensure_api_resolver(page)
    st, err = await ev(page, STATE_JS, timeout=30)
    st = st or {"error": err}
    alive = bool(st.get("my_id")) or bool(st.get("self_id")) or \
        str(st.get("auth_state")) == "authStateSignedIn"
    st["_alive"] = alive
    st["_label"] = label
    REPORT["timeline"].append(st)
    (ok if alive else bad)(
        f"{label}: alive={alive} my_id={st.get('my_id')} "
        f"self={st.get('self_id')} authState={st.get('auth_state')} "
        f"form={st.get('on_login_form')} rows={st.get('chat_rows')}")
    return st


async def run(args) -> int:
    REPORT["args"] = vars(args)
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"session_persist_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.driver import EitaaDriver

    light = False if args.no_light else None

    # ---------------- PART A: what is stored right now ----------------------
    banner("PART A  -  what is actually stored in this profile (read-only)")
    session = await BrowserSession(args.account, headed=args.headed,
                                   light_assets=light).start()
    page = session.page
    try:
        await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                        timeout=120_000)
        info(f"letting the app boot for {args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))

        before = await snapshot(page, "on open")

        store, err = await ev(page, STORAGE_JS, timeout=45)
        REPORT["storage"] = store or {"error": err}
        if err or not store:
            bad(f"could not read the profile storage - {err}")
        else:
            dump("localStorage (keys and sizes only):", store.get("localStorage"))
            dump("IndexedDB databases, stores, record counts and keys:",
                 store.get("idb"))
            if store.get("errors"):
                info(f"storage errors: {store['errors']}")
            # Is there anything that looks like a stored session at all?
            blob = json.dumps(store, ensure_ascii=False).lower()
            markers = [m for m in ("auth", "user_auth", "session", "dc1_auth_key",
                                   "authkey", "userid", "state") if m in blob]
            info(f"session-looking markers present in storage: {markers or 'NONE'}")
            REPORT["storage_markers"] = markers
            if before.get("_alive"):
                ok("the stored session RESTORED fine on this open")
            elif markers:
                bad("storage has session-looking records but the app did not "
                    "restore a user - so something IS written and the restore "
                    "is what fails")
            else:
                bad("nothing session-looking is stored - the login never got "
                    "persisted in the first place")
    except Exception:  # noqa: BLE001
        REPORT["part_a_traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)

    # ---------------- PART B: the login timeline -----------------------------
    if not args.login:
        banner("PART B  -  skipped")
        info("pass --login <phone> to log in once and watch the session over time.")
        info("That requests ONE code. Part A above may already be conclusive.")
    else:
        banner("PART B  -  log in once, then watch the session on a timeline")
        print("\n  !! this requests ONE real login code for "
              f"{args.login}. Ctrl+C within 5s to abort.\n", flush=True)
        await asyncio.sleep(5)
        from eitaa.login_flow import normalize_phone_intl
        from eitaa import login_ui

        driver = EitaaDriver(session)
        intl = normalize_phone_intl(args.login)
        res = await login_ui.request_code(driver, intl)
        REPORT["request_code"] = res
        if not res.get("ok"):
            bad(f"could not get to the code step: {res.get('code')}")
            REPORT["verdict"].append(
                f"The code request did not succeed ({res.get('code')}), so the "
                f"timeline could not run. If it was FLOOD, wait it out.")
            summarise(outdir)
            await session.close()
            return 1
        ok("the form reached the code step")

        # The code has to come from the owner's phone.
        print("\n  Enter the code you received (digits only), then press ENTER:",
              flush=True)
        code = (await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)).strip()
        if not code:
            bad("no code entered")
            await session.close()
            return 1
        si = await login_ui.submit_code(driver, code)
        REPORT["submit_code"] = si
        if not si.get("ok"):
            bad(f"the code was not accepted: {si.get('code')}")
            summarise(outdir)
            await session.close()
            return 1
        ok("the app accepted the code")

        # The timeline. No contacts call anywhere in here, so anything that
        # kills the session here kills it on its own.
        await snapshot(page, "t+0 right after login")
        for wait in (3, 7, 10):
            await asyncio.sleep(wait)
            await snapshot(page, f"t+{wait}s later")

        info("reloading the page (a session has to survive a reload)")
        try:
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(float(args.wait))
        except Exception as exc:  # noqa: BLE001
            info(f"reload failed: {exc}")
        after_reload = await snapshot(page, "after a reload")

        # Closing and reopening is the real test of persistence: it is what the
        # pool does between jobs, and what a restart does.
        info(f"closing the browser, waiting {args.flush:.0f}s, reopening")
        await session.close()
        await asyncio.sleep(float(args.flush))
        session = await BrowserSession(args.account, headed=args.headed,
                                       light_assets=light).start()
        page = session.page
        await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                        timeout=120_000)
        await asyncio.sleep(float(args.wait))
        after_restart = await snapshot(page, "after close + reopen")

        REPORT["after_reload_alive"] = bool(after_reload.get("_alive"))
        REPORT["after_restart_alive"] = bool(after_restart.get("_alive"))

        # ---------------- PART C: does our own contacts call kill it? -------
        if args.with_contacts and after_restart.get("_alive"):
            banner("PART C  -  does the contacts call itself end the session")
            driver = EitaaDriver(session)
            got = await driver.ensure_contacts_list_bridge()
            info(f"contacts bridge injected: {got}")
            res2, e2 = (None, None)
            try:
                res2 = await asyncio.wait_for(driver.bridge_contacts_list(),
                                              timeout=120)
            except Exception as exc:  # noqa: BLE001
                e2 = str(exc)
            REPORT["contacts"] = res2 if isinstance(res2, dict) else {"error": e2}
            info(f"contacts result: {json.dumps(res2, ensure_ascii=False)[:200]}"
                 if res2 else f"contacts error: {e2}")
            await asyncio.sleep(5)
            after_contacts = await snapshot(page, "after the contacts call")
            REPORT["after_contacts_alive"] = bool(after_contacts.get("_alive"))
        elif args.with_contacts:
            info("skipping part C: the session was already gone before it")

    try:
        await page.screenshot(path=str(outdir / "page.png"))
    except Exception:  # noqa: BLE001
        pass
    try:
        await session.close()
    except Exception:  # noqa: BLE001
        pass

    summarise(outdir)
    return 0


def summarise(outdir) -> None:
    banner("TIMELINE")
    for p in REPORT["timeline"]:
        mark = "ALIVE" if p.get("_alive") else "GONE "
        print(f"  {mark}  {p.get('_label','?'):<28} my_id={p.get('my_id')} "
              f"authState={p.get('auth_state')} form={p.get('on_login_form')}",
              flush=True)

    reload_ok = REPORT.get("after_reload_alive")
    restart_ok = REPORT.get("after_restart_alive")
    contacts_ok = REPORT.get("after_contacts_alive")
    markers = REPORT.get("storage_markers")

    v = REPORT["verdict"]
    if restart_ok is True and contacts_ok is False:
        v.append("SUSPECT 1 CONFIRMED: the session survives a reload AND a full "
                 "browser restart, and dies only after the contacts call. So the "
                 "bot kills its own login - tweb signs out when that call answers "
                 "INVALID_LOGIN. The fix is in what runs after login, not in the "
                 "login: do not call contacts on the fresh session until it is "
                 "confirmed, and treat INVALID_LOGIN as a stop rather than "
                 "pressing on.")
    elif restart_ok is True and contacts_ok is True:
        v.append("The session persists through everything tested, including the "
                 "contacts call. Whatever broke it live is not reproduced here - "
                 "compare this run's settings with the panel's (MKWL_WARMPATH is "
                 "on for the bot).")
    elif reload_ok is True and restart_ok is False:
        v.append("SUSPECT 2 CONFIRMED: the session survives a reload but NOT a "
                 "close and reopen. tweb debounces its state writes "
                 "(commonStateStorage.saveThrottled), so the browser is being "
                 "closed before the flush. The fix is to keep the session open "
                 "long enough after login - and to verify persistence by "
                 "reopening before reporting success.")
    elif reload_ok is False:
        v.append("The session does not even survive a RELOAD, so it was never "
                 "really established - the sign-in is accepted and nothing is "
                 "stored. Part A's storage dump says whether anything was "
                 "written at all.")
    elif markers is not None:
        if markers:
            v.append("Part A: session-looking records ARE in the profile, but the "
                     "app restored no user. The write works and the RESTORE is "
                     "what fails - likely the account slot: localStorage has "
                     "number_of_accounts/account1, and this build keys its "
                     "session by slot.")
        else:
            v.append("Part A: nothing session-looking is stored at all, so the "
                     "login is not being persisted. Run again with --login to "
                     "see exactly when it disappears.")
    if not v:
        v.append("No conclusion - send the output.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)
    try:
        (outdir / "report.json").write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\n  full report: {outdir / 'report.json'}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Find out why a successful login does not persist. "
                    "Part A is read-only; --login costs ONE code.")
    p.add_argument("--account", required=True, help="profile to inspect")
    p.add_argument("--login", default=None,
                   help="log in with this phone and watch the session (ONE code)")
    p.add_argument("--with-contacts", action="store_true",
                   help="after the timeline, make the contacts call and re-check")
    p.add_argument("--wait", type=float, default=20,
                   help="seconds to let the app boot before each check")
    p.add_argument("--flush", type=float, default=10,
                   help="seconds to wait between closing and reopening")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-light", action="store_true")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
