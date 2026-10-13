#!/usr/bin/env python3
"""Full health check: every subsystem of the bot, against the live account.

Run it after a change, after an Eitaa update, or whenever something "just stops
working". It opens ONE browser session and walks the whole product in the order
a real job does, reporting PASS / FAIL / SKIP per subsystem with the numbers
behind each answer, then prints a summary table and a verdict that names the
first broken link rather than everything downstream of it.

Covered
-------
   1. config + environment          what the bot will actually do
   2. offline test suites           the repo's own gate (optional, --tests)
   3. browser + pool                launch cost, pool settings, stale locks
   4. page + the API resolver       where apiManager was found on this build
   5. session identity              does the APP know who we are (getMyId/getSelf)
   6. login detection               is_logged_in() vs reality + a selector hunt
   7. contacts                      API read, cache, access_hash coverage
   8. peers / send readiness        engine selection, own peer id, bridge defined
   9. stats                         the stats bridge
  10. direct engine                 capture freshness, token availability
  11. session check                 the checker module
  12. stores                        contacts / progress / blocked ledgers
  13. logbus + portal               gating (both are off-by-default features)
  14. optional live actions         a send to YOUR OWN Saved Messages (--send-self)
                                    and the presence bridge (--presence)

Safety
------
* READ-ONLY by default. Nothing is sent, no login code is requested, no
  setting, contact, ledger or progress file is written.
* `--send-self` sends one timestamped line to YOUR OWN Saved Messages, nobody
  else. It is off by default and the target is resolved from your own user id.
* `--presence` is separate because the presence bridge marks YOUR account
  online (unavoidable: a client that reports itself offline is not sent
  updates). Off by default.
* No project file is modified. Output goes to the gitignored artifacts/ dir.

Run on the SERVER, with a logged-in account:

    cd ~/Mkwlsoso && .venv/bin/python deploy/bot_health_check.py --account 989104938987

    # include the repo's offline suites, and a send to your own Saved Messages:
    .venv/bin/python deploy/bot_health_check.py --account 989104938987 --tests --send-self
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"sections": {}, "verdict": []}
CHECKS: list = []            # (section, name, passed, detail)
_T0 = time.time()
_SECTION = "init"


def banner(t: str) -> None:
    global _SECTION
    _SECTION = t.split("-")[0].strip()
    print("\n" + "=" * 74 + f"\n  {t}\n" + "=" * 74, flush=True)


def log(m: str = "") -> None:
    print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)


def info(m: str) -> None:
    log(f"      {m}")


def ok(m: str) -> None:
    log(f"OK    {m}")


def bad(m: str) -> None:
    log(f"FAIL  {m}")


def check(name: str, passed, detail: str = "") -> None:
    CHECKS.append((_SECTION, name, passed, detail))
    REPORT["sections"].setdefault(_SECTION, {})[name] = {
        "passed": passed, "detail": detail}
    mark = "OK  " if passed is True else ("FAIL" if passed is False else "SKIP")
    log(f"{mark}  {name}" + (f" -> {detail}" if detail else ""))


def passed(name: str):
    for _, n, p, _d in CHECKS:
        if n == name:
            return p
    return None


def dump(label: str, obj, indent: str = "        ") -> None:
    info(label)
    for line in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + line, flush=True)


async def ev(page, js: str, arg=None, timeout: float = 25.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


async def guard(label: str, coro, timeout: float = 60.0):
    """Await anything without letting it hang or kill the run."""
    try:
        return (await asyncio.wait_for(coro, timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"{label}: timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{label}: {type(exc).__name__}: {exc}")


IDENTITY_JS = r"""
async () => {
  const cap = (p, ms) => Promise.race([
    Promise.resolve(p),
    new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms || 8000))]);
  const out = {};
  try {
    out.getMyId = await cap(window.rootScope && window.rootScope.getMyId
      ? window.rootScope.getMyId() : null);
  } catch (e) { out.getMyId = "ERR " + e; }
  try {
    const m = window.__MKWL_managers ? window.__MKWL_managers() : null;
    out.source = window.__MKWL_apiSource;
    if (m && m.appUsersManager) {
      try {
        const s = await cap(m.appUsersManager.getSelf());
        out.self = s ? { id: s.id, first_name: s.first_name, _: s._,
                         phone: s.phone ? "..." + String(s.phone).slice(-4) : null } : s;
      } catch (e) { out.self = "ERR " + e; }
    }
  } catch (e) { out.err = String(e); }
  try { out.lsKeys = Object.keys(localStorage); } catch (e) { out.lsKeys = []; }
  return out;
}
"""

DOM_JS = r"""
(repo) => {
  const vis = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
    } catch (e) { return false; }
  };
  const test = (list) => (list || []).map(sel => {
    let n = 0, v = 0;
    try {
      const els = document.querySelectorAll(sel);
      n = els.length;
      els.forEach(e => { if (vis(e)) v++; });
    } catch (e) { return { sel: sel, error: true }; }
    return { sel: sel, matches: n, visible: v };
  }).filter(r => r.error || r.matches);
  const d = (el) => ({ tag: el.tagName.toLowerCase(),
    cls: String(el.className || "").slice(0, 80) || null,
    ce: el.getAttribute("contenteditable") || null,
    visible: vis(el) });
  return {
    chat_rows: document.querySelectorAll(".chatlist-chat").length,
    containers: ["#column-left", "#column-center", ".chatlist", ".sidebar-header",
                 "#auth-pages", ".page-sign"].filter(s => {
      try { return !!document.querySelector(s); } catch (e) { return false; } }),
    repo_SEARCH_INPUT: test(repo.SEARCH_INPUT),
    // candidates that know this build turned fields into contenteditable divs
    candidates: test([".chatlist-chat", "#column-left .chatlist-chat",
      "div.input-search-input", "[contenteditable='true'].input-search-input",
      ".input-search [contenteditable='true']", "#column-left [contenteditable='true']",
      ".input-search", ".chatlist", ".sidebar-header", "#column-center"]),
    inputish: Array.prototype.slice.call(document.querySelectorAll(
      "input, [contenteditable='true']")).map(d).slice(0, 20),
    search_like: Array.prototype.slice.call(document.querySelectorAll(
      "[class*='search']")).map(d).slice(0, 15)
  };
}
"""


# ==========================================================================
# 1-2. offline: config and the repo's own test gate
# ==========================================================================

def section_config(args) -> None:
    banner("1  -  config and environment")
    from config import config

    info(f"python {sys.version.split()[0]}   repo {_ROOT}")
    try:
        import playwright  # noqa: F401
        info("playwright importable")
    except Exception as exc:  # noqa: BLE001
        check("playwright importable", False, str(exc))

    rows = {
        "EITAA_WEB_URL": config.EITAA_WEB_URL,
        "engine (config.ENGINE)": getattr(config, "ENGINE", "?"),
        "ENABLE_DIRECT": getattr(config, "ENABLE_DIRECT", "?"),
        "HEADED / HEADED_JOBS": f"{config.HEADED} / {config.HEADED_JOBS}",
        "WARMPATH": getattr(config, "WARMPATH", "?"),
        "profiles dir": str(config.PROFILES_DIR),
        "artifacts dir": str(config.ARTIFACTS_DIR),
    }
    dump("effective config:", rows)

    from eitaa.api_resolver import READY_TIMEOUT
    check("api_resolver is present (the Eitaa-moved-apiManager fix)", True,
          f"READY_TIMEOUT={READY_TIMEOUT}s")

    pdir = config.profile_dir(args.account)
    check("account profile exists", pdir.is_dir(), str(pdir))
    try:
        from capture.browser import BrowserSession
        pid = BrowserSession(args.account)._profile_in_use()
        check("no other chromium holds this profile", pid is None,
              f"pid {pid} is using it - stop the bot first" if pid else "free")
    except Exception as exc:  # noqa: BLE001
        check("no other chromium holds this profile", None, str(exc))

    # Two gaps recorded in docs/ROADMAP.md; cheap to re-verify here.
    req = (Path(_ROOT) / "requirements.txt").read_text(encoding="utf-8")
    missing = [p for p in ("fastapi", "uvicorn", "httpx", "asyncssh") if p not in req]
    check("requirements.txt covers the optional features", not missing,
          f"missing: {missing} (portal/worker import them lazily, so those "
          f"features stay silently off)" if missing else "complete")


def section_tests(args) -> None:
    banner("2  -  the repo's offline test suites")
    if not args.tests:
        check("offline suites", None, "skipped (pass --tests to run them)")
        return
    suites = ["api_resolver", "bot_logic", "send_loop", "engines", "scenarios",
              "multi_parallel", "contacts_boost", "photo_export", "warmpath",
              "portal", "portal_login", "worker", "logbus", "live_card",
              "login_settle", "pool", "send_order", "send_order_feature",
              "status_freshness"]
    failed = []
    for s in suites:
        try:
            r = subprocess.run([sys.executable, "-m", f"bot.tests.test_{s}"],
                               cwd=_ROOT, capture_output=True, timeout=180)
            if r.returncode != 0:
                failed.append(s)
                bad(f"{s}: exit {r.returncode}")
                tail = (r.stdout or b"").decode("utf-8", "ignore").strip().splitlines()[-4:]
                for ln in tail:
                    info("    " + ln[:120])
            else:
                ok(s)
        except Exception as exc:  # noqa: BLE001
            failed.append(s)
            bad(f"{s}: {exc}")
    try:
        r = subprocess.run([sys.executable, "-m", "direct.tests.test_direct"],
                           cwd=_ROOT, capture_output=True, timeout=180)
        (ok if r.returncode == 0 else bad)("direct")
        if r.returncode != 0:
            failed.append("direct")
    except Exception as exc:  # noqa: BLE001
        failed.append("direct")
        bad(f"direct: {exc}")
    check("all offline suites pass", not failed, f"failed: {failed}" if failed
          else f"{len(suites) + 1} suites")


# ==========================================================================
# live sections
# ==========================================================================

async def run(args) -> int:
    REPORT["args"] = vars(args)
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"health_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    section_config(args)
    section_tests(args)

    from capture.browser import BrowserSession
    from eitaa.driver import EitaaDriver
    from eitaa import selectors as S
    from eitaa.api_resolver import ensure_api_resolver, api_info, api_ready

    banner("3  -  browser and pool")
    try:
        from capture.pool import pool as session_pool
        dump("pool settings:", {"max_open": session_pool.max_open,
                                "idle_ttl": session_pool.idle_ttl,
                                "max_uses": session_pool.max_uses,
                                "max_age": session_pool.max_age})
    except Exception as exc:  # noqa: BLE001
        info(f"pool not inspectable: {exc}")
    t = time.time()
    light = False if args.no_light else None
    try:
        session = await BrowserSession(args.account, headed=args.headed,
                                       light_assets=light).start()
    except Exception as exc:  # noqa: BLE001
        check("browser starts", False, f"{type(exc).__name__}: {exc}")
        REPORT["browser_error"] = traceback.format_exc()
        summarise(outdir)
        return 2
    boot = time.time() - t
    check("browser starts", True, f"{boot:.1f}s")
    page = session.page
    driver = EitaaDriver(session)

    try:
        banner("4  -  page load and the API resolver")
        t = time.time()
        try:
            resp = await page.goto(config.EITAA_WEB_URL,
                                   wait_until="domcontentloaded", timeout=120_000)
            check("page loads", (resp.status if resp else 0) == 200,
                  f"HTTP {resp.status if resp else '?'} in {time.time() - t:.1f}s")
        except Exception as exc:  # noqa: BLE001
            check("page loads", False, str(exc))
            summarise(outdir)
            return 2
        info(f"letting the app boot for {args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))
        check("resolver injects", await ensure_api_resolver(page))
        ready = await api_ready(page)
        nfo = await api_info(page)
        check("API object resolvable", ready, f"source={nfo.get('source')} "
              f"legacy_global={nfo.get('legacy_global')} "
              f"managers_proxy={nfo.get('managers_proxy')}")
        REPORT["api_info"] = nfo
        # the call that proves the pipe, not just the lookup
        r, e = await ev(page, r"""
        async () => { const t=Date.now();
          const a = window.__MKWL_api ? window.__MKWL_api() : null;
          if (!a) return {ok:false, code:"no api"};
          try { const c = await Promise.race([a.invokeApi("help.getConfig", {}),
            new Promise((_,j)=>setTimeout(()=>j(new Error("timeout")),10000))]);
            return {ok:true, ms:Date.now()-t, this_dc:c&&c.this_dc,
                    dcs:(c&&c.dc_options||[]).length}; }
          catch (x) { return {ok:false, ms:Date.now()-t, code:String(x)}; } }
        """, timeout=20)
        if r and r.get("ok"):
            check("a real API round trip works", True,
                  f"help.getConfig in {r['ms']}ms, {r.get('dcs')} dc_options")
        else:
            check("a real API round trip works", False,
                  str((r or {}).get("code") or e))

        banner("5  -  session identity (ask the app, not the DOM)")
        ident, e = await ev(page, IDENTITY_JS, timeout=30)
        REPORT["identity"] = ident or {"error": e}
        my_id = (ident or {}).get("getMyId")
        self_ = (ident or {}).get("self")
        real = (isinstance(my_id, int) and my_id != 0) or (
            isinstance(self_, dict) and self_.get("id"))
        if ident:
            dump("what the app says about itself:", ident)
        check("the app knows who we are (session is real)", bool(real),
              f"getMyId={my_id}"
              + (f" self.id={self_.get('id')}" if isinstance(self_, dict) else ""))

        banner("6  -  login detection")
        t = time.time()
        detected, e = await guard("is_logged_in", driver.is_logged_in(), timeout=30)
        check("driver.is_logged_in() returns True", detected is True,
              f"{time.time() - t:.1f}s"
              + ("" if detected else " - this is what _wait_logged_in() polls for, "
                                     "so a False here is the 'LOGIN NOT CONFIRMED' card"))
        dom, e2 = await ev(page, DOM_JS,
                           {"SEARCH_INPUT": list(getattr(S, "SEARCH_INPUT", []))},
                           timeout=25)
        REPORT["dom"] = dom or {"error": e2}
        if dom:
            info(f"containers: {dom.get('containers')}   "
                 f"chat rows: {dom.get('chat_rows')}")
            dump("the repo's SEARCH_INPUT candidates that match:",
                 dom.get("repo_SEARCH_INPUT"))
            dump("replacement candidates that match:", dom.get("candidates"))
            if not dom.get("repo_SEARCH_INPUT"):
                dump("every input / contenteditable on the page:", dom.get("inputish"))
                dump("anything with 'search' in its class:", dom.get("search_like"))
            check("SEARCH_INPUT still matches the live DOM",
                  bool(dom.get("repo_SEARCH_INPUT")),
                  "nothing matches - every candidate is an `input`, and this build "
                  "uses contenteditable divs" if not dom.get("repo_SEARCH_INPUT")
                  else f"{len(dom['repo_SEARCH_INPUT'])} selector(s) match")
            check("the chat list is rendered", bool(dom.get("chat_rows")),
                  f"{dom.get('chat_rows')} rows")

        banner("7  -  contacts")
        got, e = await guard("ensure_contacts_list_bridge",
                             driver.ensure_contacts_list_bridge(), timeout=60)
        check("contacts bridge injects", got is True, str(e or ""))
        api_items = None
        if got:
            t = time.time()
            res, e = await guard("bridge_contacts_list",
                                 driver.bridge_contacts_list(), timeout=args.contacts_timeout)
            REPORT["contacts_api"] = res if isinstance(res, dict) else {"error": e}
            if isinstance(res, dict) and res.get("ok"):
                api_items = res.get("contacts") or []
                withhash = sum(1 for c in api_items if c.get("access_hash"))
                check("contacts read from the API", True,
                      f"{len(api_items)} contacts in {time.time() - t:.1f}s "
                      f"(raw={res.get('raw')} skipped={res.get('skipped')})")
                check("access_hash present (needed for browser-free send)",
                      withhash > 0, f"{withhash}/{len(api_items)}")
            else:
                check("contacts read from the API", False,
                      str((res or {}).get("code") if isinstance(res, dict) else e)[:160])

        from bot import contacts_store
        cached = contacts_store.count(args.account)
        age = contacts_store.age_hours(args.account)
        check("contacts cache on disk", cached > 0,
              f"{cached} cached, age "
              f"{'never saved' if age is None else f'{age:.1f}h'}")

        banner("8  -  send readiness (nothing is sent here)")
        from bot.runner import effective_engine
        from bot.store import store
        try:
            settings = store.settings(args.account) if hasattr(store, "settings") else {}
        except Exception:  # noqa: BLE001
            settings = {}
        eng = effective_engine(settings if isinstance(settings, dict) else {})
        check("engine selection resolves", bool(eng),
              f"effective engine = {eng}")
        got, e = await guard("ensure_bridge", driver.ensure_bridge(), timeout=60)
        check("send bridge injects (window.__MKWL_send)", got is True, str(e or ""))
        gotf, e = await guard("ensure_file_bridge", driver.ensure_file_bridge(), timeout=60)
        check("file bridge injects", gotf is True, str(e or ""))
        selfpeer, e = await guard("self_peer_id", driver.self_peer_id(), timeout=60)
        check("own peer id resolves (Saved Messages target)", bool(selfpeer),
              str(selfpeer or e))

        banner("9  -  stats bridge")
        gots, e = await guard("ensure_stats_bridge",
                             driver.ensure_stats_bridge(), timeout=60)
        check("stats bridge injects", gots is True, str(e or ""))
        if gots:
            st, e = await guard("bridge_stats", driver.bridge_stats(with_pvs=False),
                                timeout=90)
            REPORT["stats"] = st if isinstance(st, dict) else {"error": e}
            if isinstance(st, dict) and st.get("ok") is not False:
                check("stats readable", True, json.dumps(
                    {k: v for k, v in list(st.items())[:6]}, ensure_ascii=False)[:160])
            else:
                check("stats readable", False, str((st or {}).get("code") or e)[:160])

        banner("10  -  direct engine (browser-free path)")
        try:
            from config import config as c2
            check("direct engine enabled", bool(getattr(c2, "ENABLE_DIRECT", False)),
                  "MKWL_ENABLE_DIRECT=1 is required; without it the panel falls "
                  "back to hybrid")
        except Exception:  # noqa: BLE001
            pass
        try:
            exported, e = await guard("export_session", driver.export_session(), timeout=60)
            has_tok = bool(exported and (exported.get("token") or exported.get("token1")))
            check("session export yields a token", has_tok,
                  f"keys={list(exported.keys())[:8]}" if isinstance(exported, dict)
                  else str(e))
        except Exception as exc:  # noqa: BLE001
            check("session export yields a token", None, str(exc))

        banner("11  -  session check module")
        try:
            from session_check import checker
            fn = getattr(checker, "check_account", None) or getattr(checker, "check", None)
            if fn is None:
                check("session_check exposes a check function", None,
                      f"module members: {[m for m in dir(checker) if not m.startswith('_')][:8]}")
            else:
                check("session_check exposes a check function", True, fn.__name__)
        except Exception as exc:  # noqa: BLE001
            check("session_check imports", False, str(exc))

        banner("12  -  stores and ledgers")
        from bot import progress_store, blocked_store
        try:
            check("blocked-recipient store readable", True,
                  f"{blocked_store.count(args.account)} refused peers remembered")
        except Exception as exc:  # noqa: BLE001
            check("blocked-recipient store readable", False, str(exc))
        try:
            p = progress_store.path_for(args.account)
            check("progress ledger path resolves", True,
                  f"{p} (exists={p.exists()})")
        except Exception as exc:  # noqa: BLE001
            check("progress ledger path resolves", False, str(exc))

        banner("13  -  logbus and portal (both off by default)")
        try:
            from bot import logbus
            gid = os.environ.get("MKWL_LOG_GROUP_ID")
            check("logbus is inert unless configured", True,
                  f"MKWL_LOG_GROUP_ID={'set' if gid else 'unset -> mirroring off'}")
        except Exception as exc:  # noqa: BLE001
            check("logbus imports", False, str(exc))
        pe = os.environ.get("MKWL_PORTAL_ENABLED")
        check("portal gating", True,
              f"MKWL_PORTAL_ENABLED={pe or 'unset -> off'}; needs fastapi/uvicorn/"
              f"httpx + cloudflared, which requirements.txt does not declare")

        banner("14  -  optional live actions")
        if args.send_self and selfpeer:
            msg = f"mkwl health check {time.strftime('%Y-%m-%d %H:%M:%S')}"
            info(f"sending ONE line to your own Saved Messages (peer {selfpeer})")
            res, e = await guard("bridge_send",
                                 driver.bridge_send(str(selfpeer), msg), timeout=120)
            REPORT["send_self"] = res if isinstance(res, dict) else {"error": e}
            if isinstance(res, dict) and res.get("ok"):
                check("a real send works", True,
                      f"method={res.get('method')} msg_id={res.get('msg_id')}")
            else:
                check("a real send works", False,
                      str((res or {}).get("code") or e)[:160])
        else:
            check("a real send works", None,
                  "skipped (pass --send-self; it targets only your own Saved Messages)"
                  if not args.send_self else "skipped: own peer id unresolved")

        if args.presence:
            got, e = await guard("ensure_presence_bridge",
                                 driver.ensure_presence_bridge(), timeout=60)
            check("presence bridge injects", got is True, str(e or ""))
            if got:
                pr, e = await guard("bridge_live_presence",
                                    driver.bridge_live_presence(batch=50), timeout=120)
                REPORT["presence"] = pr if isinstance(pr, dict) else {"error": e}
                if isinstance(pr, dict) and pr.get("ok"):
                    check("presence readable", True,
                          json.dumps({k: pr.get(k) for k in list(pr)[:6]},
                                     ensure_ascii=False)[:160])
                else:
                    check("presence readable", False,
                          str((pr or {}).get("code") or e)[:160])
        else:
            check("presence readable", None,
                  "skipped (pass --presence; it marks YOUR account online)")

        try:
            await page.screenshot(path=str(outdir / "page.png"))
            (outdir / "page.html").write_text(await page.content(), encoding="utf-8")
            ok(f"screenshot + DOM saved to {outdir}")
        except Exception:  # noqa: BLE001
            pass
        if args.keep_open:
            await asyncio.sleep(float(args.keep_open))
    except Exception:  # noqa: BLE001
        REPORT["traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass

    summarise(outdir)
    return 0 if not [c for c in CHECKS if c[2] is False] else 1


def summarise(outdir: Path) -> None:
    banner("SUMMARY")
    if CHECKS:
        w = max(len(n) for _, n, _, _ in CHECKS)
        last = None
        for sec, name, p, detail in CHECKS:
            if sec != last:
                print(f"\n  -- section {sec} --", flush=True)
                last = sec
            mark = "PASS" if p is True else ("FAIL" if p is False else "SKIP")
            print(f"  {mark}  {name.ljust(w)}  {detail[:110]}", flush=True)
    fails = [n for _, n, p, _ in CHECKS if p is False]
    REPORT["failed"] = fails
    print("", flush=True)

    # The verdict names the FIRST broken link, because everything after a broken
    # link fails for the same reason and listing all of it hides the cause.
    real = passed("the app knows who we are (session is real)")
    detect = passed("driver.is_logged_in() returns True")
    sel = passed("SEARCH_INPUT still matches the live DOM")
    api = passed("a real API round trip works")
    contacts = passed("contacts read from the API")

    v = REPORT["verdict"]
    if api is False:
        v.append("The API channel itself is down - nothing downstream can work. "
                 "Start there: run deploy/preflight_fix_check.py, which isolates "
                 "the resolver from everything else.")
    elif real and detect is False and sel is False:
        v.append("CONFIRMED: the login is fine and the DETECTOR is wrong. The app "
                 "reports a real user id, while is_logged_in() searches for "
                 "`input` elements that this Eitaa build no longer has - its "
                 "fields are contenteditable divs. Fix detection (ask the app via "
                 "getMyId/getSelf, with the matching selectors from section 6 as "
                 "backup) and 'LOGIN NOT CONFIRMED' goes away. The session needs "
                 "no repair.")
    elif real and detect is False:
        v.append("The session is real but is_logged_in() still returns False, and "
                 "SEARCH_INPUT does match something - so the detector's selector is "
                 "not the whole story. Compare section 6's matches against what "
                 "_first_visible() requires (it needs a VISIBLE match).")
    elif real is False:
        v.append("The app does not report a user, so the session did not survive. "
                 "Look at the finalize step in login_bridge.js (dispatchUserAuth) "
                 "and re-add the account.")
    elif real and detect:
        v.append("Session and detection agree. If the panel still showed 'LOGIN NOT "
                 "CONFIRMED', it was a boot-time race: raise "
                 "MKWL_LOGIN_SETTLE_TIMEOUT.")
    if contacts is False:
        v.append("Contacts could not be read - that is a separate failure from the "
                 "login, and section 7 has the error.")
    elif contacts:
        v.append("Contacts read fine, so once detection is fixed the normal flow "
                 "(cache contacts, then send) should run.")
    if not v:
        v.append("No blocking failure identified; see the table above.")
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
        description="Full bot health check. Read-only unless you pass --send-self "
                    "or --presence; never requests a login code.")
    p.add_argument("--account", required=True, help="a LOGGED-IN account id")
    p.add_argument("--wait", type=float, default=25,
                   help="seconds to let the app boot before looking (default 25)")
    p.add_argument("--contacts-timeout", type=float, default=180,
                   help="ceiling for the contacts read (default 180s)")
    p.add_argument("--tests", action="store_true",
                   help="also run the repo's 19 offline suites")
    p.add_argument("--send-self", action="store_true",
                   help="send ONE line to YOUR OWN Saved Messages as a live proof")
    p.add_argument("--presence", action="store_true",
                   help="also test the presence bridge (marks YOUR account online)")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-light", action="store_true")
    p.add_argument("--keep-open", type=float, default=0)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
