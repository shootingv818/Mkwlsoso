#!/usr/bin/env python3
"""One interactive run: log in with your own number, then measure everything.

You give it a phone number, it drives Eitaa's login form, you paste the code it
asks for, and then it measures - with numbers, not guesses - whether the session
is written to disk, whether it survives a reload, whether it survives the
browser being closed and reopened (which is what restarting the bot does), and
whether reading contacts destroys it.

Why this shape
--------------
The pieces were tested separately and that cost a lot of time and a few login
codes. Everything the diagnosis still needs happens in ONE login, in order, so
a single run answers all of it.

What is already settled, and is NOT re-litigated here:

    API object resolvable      rootScope.managers     (the apiManager fix holds)
    a real API round trip      help.getConfig 113 ms  (the pipe is fine)
    the form login             code arrives, accepted

What is open, and what this measures:

  * Is the session written to the profile AT ALL? Measured by diffing the
    profile's localStorage and IndexedDB record keys BEFORE and AFTER the login.
    A login that writes nothing explains "the sessions disappear whenever we
    update the bot": the session would only ever live in the running browser's
    memory, so it looks fine until the browser goes away.
  * Does it survive a reload?
  * Does it survive a close and reopen? That is exactly what `systemctl restart`
    does to a warm pooled browser.
  * Does the contacts read kill it? Right after login the bot reads contacts,
    that call answered INVALID_LOGIN live, and tweb signs out on an auth error -
    so the bot may be destroying its own login.

Safety
------
* Exactly ONE login code is requested, for the number YOU type, and only after
  it shows you the number and waits for you to confirm.
* Nothing is sent to any contact. `--send-self` optionally sends one line to
  YOUR OWN Saved Messages and nothing else.
* No token or message is printed: storage values are reduced to a type and a
  length, keys are listed by name.
* No project file is modified. Output goes to the gitignored artifacts/ dir.

Run on the SERVER (stop the bot first so it does not hold the profile):

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/login_e2e.py
    systemctl start mkwlsoso-bot

It will ask for the phone number. To skip the prompt: --phone 989123456789
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"timeline": [], "stats": {}, "verdict": []}
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


async def ask(prompt: str) -> str:
    """Read a line from the operator without blocking the event loop."""
    print("\n" + prompt, flush=True)
    line = await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
    return (line or "").strip()


async def ev(page, js: str, arg=None, timeout: float = 45.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ---- in-page measurements -------------------------------------------------

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
      out.self_name = s ? (s.first_name || null) : null;
    }
  } catch (e) { out.managers_error = String(e); }
  try {
    out.on_login_form = !!document.querySelector(".page-sign, .page-authCode");
    out.chat_rows = document.querySelectorAll(".chatlist-chat").length;
  } catch (e) {}
  return out;
}
"""

# Keys and sizes only. A session token must never reach the terminal or the
# report file.
STORAGE_JS = r"""
async () => {
  const out = { ls: {}, idb: {}, errors: [] };
  try {
    for (const k of Object.keys(localStorage)) {
      let v = "";
      try { v = String(localStorage.getItem(k) || ""); } catch (e) {}
      out.ls[k] = v.length;
    }
  } catch (e) { out.errors.push("localStorage: " + e); }
  try {
    if (!indexedDB.databases) { out.errors.push("indexedDB.databases() unsupported"); return out; }
    const dbs = await indexedDB.databases();
    for (const meta of dbs) {
      const db = await new Promise((res, rej) => {
        const r = indexedDB.open(meta.name);
        r.onsuccess = () => res(r.result);
        r.onerror = () => rej(r.error);
        setTimeout(() => rej(new Error("open timeout")), 5000);
      }).catch(e => { out.errors.push(meta.name + ": " + e); return null; });
      if (!db) continue;
      for (const storeName of Array.from(db.objectStoreNames)) {
        const path = meta.name + "/" + storeName;
        try {
          const os_ = db.transaction(storeName, "readonly").objectStore(storeName);
          const keys = await new Promise((res, rej) => {
            const q = os_.getAllKeys();
            q.onsuccess = () => res((q.result || []).map(String));
            q.onerror = () => rej(q.error);
            setTimeout(() => rej(new Error("keys timeout")), 4000);
          });
          out.idb[path] = keys.sort();
        } catch (e) { out.idb[path] = ["<error: " + e + ">"]; }
      }
      db.close();
    }
  } catch (e) { out.errors.push("idb: " + e); }
  return out;
}
"""


def storage_diff(before: dict, after: dict) -> dict:
    """What the login actually wrote to the profile."""
    b = before or {}
    a = after or {}
    out = {"localStorage_new": [], "localStorage_changed": [], "idb_new_records": {},
            "idb_new_stores": []}
    bl, al = b.get("ls") or {}, a.get("ls") or {}
    for k, size in al.items():
        if k not in bl:
            out["localStorage_new"].append({"key": k, "bytes": size})
        elif bl[k] != size:
            out["localStorage_changed"].append({"key": k, "from": bl[k], "to": size})
    bi, ai = b.get("idb") or {}, a.get("idb") or {}
    for path, keys in ai.items():
        if path not in bi:
            out["idb_new_stores"].append({"store": path, "records": len(keys),
                                          "keys": keys[:25]})
            continue
        new_keys = [k for k in keys if k not in set(bi[path])]
        if new_keys:
            out["idb_new_records"][path] = new_keys[:25]
    return out


async def snapshot(page, label: str) -> dict:
    from eitaa.api_resolver import ensure_api_resolver
    await ensure_api_resolver(page)
    st, err = await ev(page, STATE_JS)
    st = st or {"error": err}
    alive = bool(st.get("my_id")) or bool(st.get("self_id")) or \
        str(st.get("auth_state")) == "authStateSignedIn"
    st["_alive"] = alive
    st["_label"] = label
    st["_at"] = round(time.time() - _T0, 1)
    REPORT["timeline"].append(st)
    (ok if alive else bad)(
        f"{label}: alive={alive}  my_id={st.get('my_id')}  "
        f"self={st.get('self_id')}  authState={st.get('auth_state')}  "
        f"form={st.get('on_login_form')}  rows={st.get('chat_rows')}")
    return st


async def open_browser(account: str, args):
    from capture.browser import BrowserSession
    light = False if args.no_light else None
    t = time.time()
    s = await BrowserSession(account, headed=args.headed, light_assets=light).start()
    info(f"browser up in {time.time() - t:.1f}s")
    return s


async def goto_and_settle(page, args):
    from config import config
    t = time.time()
    resp = await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                           timeout=120_000)
    await asyncio.sleep(float(args.wait))
    return (resp.status if resp else None), round(time.time() - t, 1)


async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items() if k != "phone"}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"login_e2e_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from eitaa.login_flow import normalize_phone_intl
    from eitaa.driver import EitaaDriver
    from eitaa import login_ui

    banner("STEP 1  -  the number")
    raw = args.phone or await ask(
        "  Phone number?  /  شماره تلفن را وارد کن:\n"
        "  (e.g. 09104938987 or 989104938987)")
    intl = normalize_phone_intl(raw)
    digits = re.sub(r"\D", "", intl)
    if not digits or len(digits) < 10:
        bad(f"that does not look like a phone number: {raw!r} -> {intl!r}")
        return 2
    account = digits
    REPORT["account"] = account
    ok(f"number {intl}  ->  profile '{account}'")
    info(f"the form will be typed with the national part: "
         f"{login_ui.national_part(intl)}")

    # Where the profile really lives. PROFILES_DIR is relative by default, so a
    # command run from a different directory writes a DIFFERENT profile - which
    # on its own would look like "the sessions disappear".
    pdir = Path(config.profile_dir(account)).resolve()
    info(f"profile path: {pdir}")
    info(f"cwd         : {Path.cwd()}")
    if Path.cwd().resolve() != Path(_ROOT).resolve():
        bad(f"you are NOT in the repo root ({_ROOT}). PROFILES_DIR is relative, "
            f"so this run uses a different profile directory than the bot does.")
    REPORT["profile_path"] = str(pdir)
    REPORT["existed_before"] = pdir.is_dir()

    confirmed = args.yes or (await ask(
        f"  This requests ONE login code for {intl}. Type y to continue:\n"
        f"  یک کد ورود برای همین شماره درخواست می‌شود. برای ادامه y بزن:")).lower() in ("y", "yes")
    if not confirmed:
        info("aborted, nothing was requested")
        return 0

    banner("STEP 2  -  open the browser and read the profile BEFORE the login")
    session = await open_browser(account, args)
    page = session.page
    try:
        status, took = await goto_and_settle(page, args)
        ok(f"page loaded: HTTP {status} in {took}s")

        pre_state = await snapshot(page, "before login")
        store_before, err = await ev(page, STORAGE_JS)
        REPORT["storage_before"] = store_before or {"error": err}
        if store_before:
            info(f"localStorage keys: {sorted((store_before.get('ls') or {}).keys())}")
            info(f"IndexedDB stores : "
                 f"{ {k: len(v) for k, v in (store_before.get('idb') or {}).items()} }")
        if pre_state.get("_alive"):
            ok("this account is ALREADY logged in on this profile")
            if not args.force:
                info("nothing to log in. Re-run with --force to log in anyway, "
                     "or just run the measurements below on the live session.")
                args.skip_login = True

        # ---------------- the login ----------------------------------------
        if not getattr(args, "skip_login", False):
            banner("STEP 3  -  the login form")
            driver = EitaaDriver(session)
            t = time.time()
            res = await login_ui.request_code(driver, intl)
            REPORT["request_code"] = res
            REPORT["stats"]["request_code_s"] = round(time.time() - t, 1)
            if not res.get("ok"):
                bad(f"could not reach the code step: {res.get('code')}")
                fw = login_ui.flood_seconds(str(res.get("code")))
                if fw:
                    bad(f"rate limited for {fw}s (~{fw // 60}m). Wait it out - "
                        f"asking again makes it longer.")
                REPORT["verdict"].append(
                    f"The code request failed: {res.get('code')}")
                summarise(outdir)
                return 1
            ok(f"reached the code step in {REPORT['stats']['request_code_s']}s")

            code = await ask("  Enter the code Eitaa sent you  /  کدی که ایتا "
                             "فرستاد را وارد کن:")
            if not re.sub(r"\D", "", code):
                bad("no code entered")
                return 1
            t = time.time()
            si = await login_ui.submit_code(driver, code)
            REPORT["submit_code"] = si
            REPORT["stats"]["submit_code_s"] = round(time.time() - t, 1)
            if not si.get("ok"):
                bad(f"the code was not accepted: {si.get('code')}")
                if si.get("needs_password"):
                    bad("this account has a 2FA password, which is not built yet")
                REPORT["verdict"].append(f"Sign-in failed: {si.get('code')}")
                summarise(outdir)
                return 1
            ok(f"the app accepted the code in {REPORT['stats']['submit_code_s']}s")

            # ---------------- does anything get WRITTEN ---------------------
            banner("STEP 4  -  what did the login write to the profile")
            await snapshot(page, "t+0 after login")
            await asyncio.sleep(3)
            await snapshot(page, "t+3s")
            store_after, err = await ev(page, STORAGE_JS)
            REPORT["storage_after"] = store_after or {"error": err}
            diff = storage_diff(store_before, store_after)
            REPORT["storage_diff"] = diff
            dump("what the login added to the profile:", diff)
            wrote = bool(diff["localStorage_new"] or diff["localStorage_changed"]
                         or diff["idb_new_records"] or diff["idb_new_stores"])
            REPORT["stats"]["login_wrote_to_profile"] = wrote
            (ok if wrote else bad)(
                "the login DID write to the profile" if wrote else
                "the login wrote NOTHING - the session exists only in this "
                "browser's memory, which is exactly why it disappears whenever "
                "the bot is restarted")

            info(f"waiting {args.flush:.0f}s for tweb's throttled state writes "
                 f"to flush (commonStateStorage.saveThrottled)")
            await asyncio.sleep(float(args.flush))
            store_flushed, _ = await ev(page, STORAGE_JS)
            diff2 = storage_diff(store_before, store_flushed)
            REPORT["storage_diff_after_flush"] = diff2
            if diff2 != diff:
                ok("more was written after waiting - the writes ARE debounced, "
                   "so closing the browser too early loses them")
                dump("after the flush wait:", diff2)
            else:
                info("nothing further was written during the wait")

        # ---------------- survival tests -----------------------------------
        banner("STEP 5  -  does the session survive a reload")
        try:
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(float(args.wait))
        except Exception as exc:  # noqa: BLE001
            info(f"reload failed: {exc}")
        after_reload = await snapshot(page, "after a reload")
        REPORT["stats"]["survives_reload"] = bool(after_reload.get("_alive"))

        banner("STEP 6  -  does it survive the browser closing  (= a bot restart)")
        info("closing the browser completely, then reopening the same profile")
        await session.close()
        await asyncio.sleep(float(args.flush))
        session = await open_browser(account, args)
        page = session.page
        status, took = await goto_and_settle(page, args)
        info(f"reopened: HTTP {status} in {took}s")
        after_restart = await snapshot(page, "after close + reopen")
        REPORT["stats"]["survives_restart"] = bool(after_restart.get("_alive"))

        # ---------------- the rest of the product ---------------------------
        driver = EitaaDriver(session)
        if after_restart.get("_alive"):
            banner("STEP 7  -  contacts  (the call that may be killing the login)")
            got = await driver.ensure_contacts_list_bridge()
            info(f"contacts bridge injected: {got}")
            t = time.time()
            res2 = None
            err2 = None
            try:
                res2 = await asyncio.wait_for(driver.bridge_contacts_list(),
                                              timeout=args.contacts_timeout)
            except Exception as exc:  # noqa: BLE001
                err2 = f"{type(exc).__name__}: {exc}"
            REPORT["contacts"] = res2 if isinstance(res2, dict) else {"error": err2}
            REPORT["stats"]["contacts_s"] = round(time.time() - t, 1)
            if isinstance(res2, dict) and res2.get("ok"):
                items = res2.get("contacts") or []
                withhash = sum(1 for c in items if c.get("access_hash"))
                ok(f"contacts read: {len(items)} in {REPORT['stats']['contacts_s']}s "
                   f"({withhash} with access_hash, raw={res2.get('raw')}, "
                   f"skipped={res2.get('skipped')})")
                REPORT["stats"]["contacts_count"] = len(items)
            else:
                bad(f"contacts failed: "
                    f"{(res2 or {}).get('code') if isinstance(res2, dict) else err2}")
                REPORT["stats"]["contacts_count"] = 0
            await asyncio.sleep(5)
            after_contacts = await snapshot(page, "after the contacts call")
            REPORT["stats"]["survives_contacts"] = bool(after_contacts.get("_alive"))

            banner("STEP 8  -  the rest of the send path")
            selfpeer = None
            try:
                selfpeer = await asyncio.wait_for(driver.self_peer_id(), timeout=60)
            except Exception as exc:  # noqa: BLE001
                info(f"self_peer_id raised: {exc}")
            (ok if selfpeer else bad)(f"own peer id (Saved Messages): {selfpeer}")
            REPORT["stats"]["self_peer_id"] = selfpeer
            ok(f"send bridge injected: {await driver.ensure_bridge()}")
            ok(f"file bridge injected: {await driver.ensure_file_bridge()}")
            if await driver.ensure_stats_bridge():
                try:
                    st = await asyncio.wait_for(driver.bridge_stats(with_pvs=False),
                                                timeout=90)
                    (ok if st else bad)(f"stats: {json.dumps(st, ensure_ascii=False)[:160]}"
                                        if st else "stats returned nothing")
                    REPORT["stats"]["stats"] = st
                except Exception as exc:  # noqa: BLE001
                    bad(f"stats raised: {exc}")

            if args.send_self and selfpeer:
                banner("STEP 9  -  one real send, to YOUR OWN Saved Messages")
                msg = f"mkwl e2e {time.strftime('%H:%M:%S')}"
                t = time.time()
                try:
                    r = await asyncio.wait_for(
                        driver.bridge_send(str(selfpeer), msg), timeout=120)
                except Exception as exc:  # noqa: BLE001
                    r = {"ok": False, "code": f"{type(exc).__name__}: {exc}"}
                REPORT["send_self"] = r
                REPORT["stats"]["send_s"] = round(time.time() - t, 1)
                (ok if r.get("ok") else bad)(
                    f"send: {json.dumps(r, ensure_ascii=False)[:200]} "
                    f"in {REPORT['stats']['send_s']}s")
                REPORT["stats"]["send_ok"] = bool(r.get("ok"))
                await asyncio.sleep(3)
                await snapshot(page, "after the send")
        else:
            banner("STEPS 7-9  -  skipped")
            info("the session was already gone after reopening the browser, so "
                 "contacts, stats and sending cannot be measured. Fixing "
                 "persistence comes first.")

        try:
            await page.screenshot(path=str(outdir / "page.png"))
            (outdir / "page.html").write_text(await page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        REPORT["traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)
    finally:
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
        print(f"  {mark}  t={str(p.get('_at','?')).rjust(6)}s  "
              f"{str(p.get('_label','?')):<26} my_id={p.get('my_id')} "
              f"authState={p.get('auth_state')} form={p.get('on_login_form')}",
              flush=True)

    banner("NUMBERS")
    s = REPORT["stats"]
    for k in ("request_code_s", "submit_code_s", "login_wrote_to_profile",
              "survives_reload", "survives_restart", "survives_contacts",
              "contacts_count", "contacts_s", "self_peer_id", "send_ok", "send_s"):
        if k in s:
            print(f"  {k.ljust(26)} {s[k]}", flush=True)

    wrote = s.get("login_wrote_to_profile")
    reload_ok = s.get("survives_reload")
    restart_ok = s.get("survives_restart")
    contacts_alive = s.get("survives_contacts")

    banner("VERDICT")
    v = REPORT["verdict"]
    if wrote is False:
        v.append("ROOT CAUSE: the login writes NOTHING to the profile. The "
                 "session lives only in the running browser's memory, which is "
                 "exactly why 'the sessions disappear whenever we update the "
                 "bot' - a restart throws the browser away. The storage diff in "
                 "STEP 4 is the evidence; next step is to make Eitaa persist it "
                 "(or to stop relying on the browser profile for the session).")
    elif restart_ok is False and reload_ok is True:
        v.append("The session survives a reload but NOT the browser closing. So "
                 "it is held in memory and the write never lands - tweb "
                 "debounces state writes (commonStateStorage.saveThrottled), and "
                 "the browser is going away before the flush. Keeping the page "
                 "open longer after login, and verifying by reopening before "
                 "reporting success, is the fix.")
    elif restart_ok is False and reload_ok is False:
        v.append("The session does not even survive a reload, so it is never "
                 "really established - the sign-in is accepted and nothing "
                 "sticks. STEP 4's diff says whether anything was written.")
    elif restart_ok is True and contacts_alive is False:
        v.append("The session persists through a restart and dies only after the "
                 "contacts call. The bot destroys its own login: that call "
                 "answers INVALID_LOGIN and tweb signs out on an auth error. Fix "
                 "what runs after login, not the login.")
    elif restart_ok is True and contacts_alive is True:
        v.append("The session persists through a reload, a browser restart AND "
                 "the contacts call. Login and persistence are healthy; whatever "
                 "went wrong in the panel is elsewhere - compare the panel's "
                 "environment (MKWL_WARMPATH=1 is on for the bot) with this run.")
    if s.get("contacts_count") == 0 and restart_ok:
        v.append("Contacts still read as empty - see STEP 7's error.")
    if not v:
        v.append("No conclusion - send this output.")
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
        description="Interactive end-to-end login test and measurement. "
                    "Requests ONE code, for the number you type.")
    p.add_argument("--phone", default=None, help="skip the prompt")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.add_argument("--force", action="store_true",
                   help="log in again even if the profile is already logged in")
    p.add_argument("--send-self", action="store_true",
                   help="also send ONE line to YOUR OWN Saved Messages")
    p.add_argument("--wait", type=float, default=20,
                   help="seconds to let the app boot after each load (default 20)")
    p.add_argument("--flush", type=float, default=15,
                   help="seconds to wait for throttled writes / between close and "
                        "reopen (default 15)")
    p.add_argument("--contacts-timeout", type=float, default=180)
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
