#!/usr/bin/env python3
"""Bridge-login diagnostic: find the EXACT point where login breaks.

Why this exists
---------------
The panel reports only `auth bridge unavailable` for a bridge login. That one
string is produced by `eitaa/login_flow.py:122` and it covers FOUR different
failures, because `ensure_login_bridge()` swallows every exception:

  1. `login_bridge.js` could not be read       -> _LOGIN_SRC is empty
  2. `page.evaluate(_LOGIN_SRC)` raised        -> exception discarded
  3. the first `typeof` probe raised           -> page/renderer is dead
  4. `window.apiManager.invokeApi` never       -> only 30 x 500 ms = 15 s are
     appeared within the wait window              waited, and the cause can be
                                                  (a) the page never loaded,
                                                  (b) Eitaa renamed/moved the
                                                      global, or
                                                  (c) the SPA simply booted in
                                                      more than 15 s.

This script walks the same chain the bot walks, in order, and prints what each
step returned INCLUDING the exceptions production throws away. It never retries
blindly and it stops at the first broken link with a verdict.

Safety / read-only
------------------
* It does NOT request a login code. `auth.sendCode` is never called, so no code
  is burned and no number can be rate-limited by running this.
  (For the delivery question - App vs SMS vs call - use `probe_login_sms.py`.)
* It only READS the project's modules; no settings, contacts, ledger or state
  file is written. Output goes to `artifacts/login_diag_<ts>/` (gitignored).
* The only RPC it makes is `help.getNearestDc`, an unauthenticated, harmless
  call, and only to prove the transport works. Skip it with --no-rpc.

Run on the SERVER:

    cd ~/Mkwlsoso && .venv/bin/python deploy/login_diag.py --account 989939094655

Useful variants:

    --wait 180        how long to watch for the SPA globals (default 120 s)
    --pool            lease through capture/pool.py, exactly like the bot does
    --headed          real window (needs DISPLAY=:99 / xvfb) instead of headless
    --no-light        load images/fonts/media too (MKWL_LIGHT_ASSETS=0 behaviour)
    --url <url>       override EITAA_WEB_URL for one run
    --no-rpc          skip the harmless help.getNearestDc probe
    --keep-open 60    keep the browser alive N seconds at the end (for xvfb/VNC)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import ssl
import sys
import time
import traceback
from pathlib import Path

# repo root on sys.path so project imports work regardless of CWD
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"stages": {}, "findings": [], "verdict": []}
_T0 = time.time()


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

def banner(text: str) -> None:
    print("\n" + "=" * 74)
    print("  " + text)
    print("=" * 74, flush=True)


def log(msg: str = "") -> None:
    print(f"  [{time.time() - _T0:6.1f}s] {msg}" if msg else "", flush=True)


def ok(msg: str) -> None:
    log(f"OK    {msg}")


def bad(msg: str) -> None:
    log(f"FAIL  {msg}")
    REPORT["findings"].append(msg)


def info(msg: str) -> None:
    log(f"      {msg}")


def verdict(line: str) -> None:
    REPORT["verdict"].append(line)


def short(v, n: int = 300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + f"... (+{len(s) - n} chars)"


async def safe_eval(page, js: str, arg=None):
    """page.evaluate that returns (value, error_text) instead of raising.

    Production code hides these errors; the whole point here is to see them.
    """
    try:
        return (await (page.evaluate(js, arg) if arg is not None else page.evaluate(js)), None)
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# STAGE 1 - host / environment
# --------------------------------------------------------------------------

def stage_env(args) -> dict:
    banner("STAGE 1  -  host and configuration")
    from config import config

    out: dict = {}
    out["python"] = sys.version.split()[0]
    out["cwd"] = os.getcwd()
    out["repo"] = _ROOT
    log(f"python {out['python']}   repo {_ROOT}")

    try:
        import playwright  # noqa: F401
        from playwright import __version__ as pwv
        out["playwright"] = pwv
    except Exception as exc:  # noqa: BLE001
        out["playwright"] = f"<import failed: {exc}>"
    info(f"playwright: {out['playwright']}")

    # RAM / CPU: Chromium needs ~1 GB here; an OOM kill looks like a dead page.
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            if k.strip() in ("MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree"):
                mem[k.strip()] = v.strip()
        out["meminfo"] = mem
        info(f"memory: MemTotal={mem.get('MemTotal')}  MemAvailable={mem.get('MemAvailable')}")
        avail_kb = int(re.sub(r"\D", "", mem.get("MemAvailable", "0")) or 0)
        if avail_kb and avail_kb < 450_000:
            bad(f"only {avail_kb // 1024} MB RAM available - Chromium's renderer "
                f"can be OOM-killed mid-boot, which looks exactly like "
                f"'auth bridge unavailable'")
    except Exception:  # noqa: BLE001
        pass
    try:
        out["loadavg"] = Path("/proc/loadavg").read_text().strip()
        info(f"loadavg: {out['loadavg']}")
    except Exception:  # noqa: BLE001
        pass
    out["display"] = os.environ.get("DISPLAY", "") or "<unset>"

    # the env that actually decides how the bot behaves
    flags = {k: os.environ.get(k) for k in (
        "EITAA_WEB_URL", "HEADED", "MKWL_HEADED_JOBS", "MKWL_LIGHT_ASSETS",
        "MKWL_SESSION_POOL", "MKWL_POOL_MAX_OPEN", "MKWL_POOL_IDLE_TTL",
        "MKWL_POOL_MAX_USES", "MKWL_POOL_MAX_AGE", "MKWL_WARMPATH",
        "MKWL_CONTEXT_MAX_AGE_H", "MKWL_LOGIN_SETTLE_TIMEOUT", "MKWL_MODE",
        "EITAA_API_ID", "EITAA_API_HASH") if os.environ.get(k) is not None}
    if flags.get("EITAA_API_HASH"):
        flags["EITAA_API_HASH"] = "...set (" + str(len(flags["EITAA_API_HASH"])) + " chars)"
    out["env"] = flags
    info(f"env set in this process: {flags or '<none - defaults apply>'}")

    url = args.url or config.EITAA_WEB_URL
    out["url"] = url
    info(f"target URL   : {url}")
    info(f"headless     : {not args.headed}   (the bot's jobs use "
         f"HEADED_JOBS={config.HEADED_JOBS})")
    info(f"light assets : {'0 (load everything)' if args.no_light else '1 (block image/media/font)'}")
    info(f"via pool     : {bool(args.pool)}")

    # api_id/api_hash: needed AFTER the bridge is up, but a missing pair gives a
    # different error ('missing api_id/api_hash'), so record it for context.
    from eitaa.login_flow import resolve_api_creds
    aid, ah = resolve_api_creds()
    out["api_id"] = aid
    out["api_hash_set"] = bool(ah)
    if aid and ah:
        ok(f"api creds resolved offline: api_id={aid} (hash ...{str(ah)[-4:]})")
    else:
        info("no api creds offline - the in-page fallback (window.Config.App) "
             "will be tried; this is NOT the current error")

    # the profile that holds the session
    pdir = config.profile_dir(args.account)
    out["profile_dir"] = str(pdir)
    out["profile_exists"] = pdir.is_dir()
    if pdir.is_dir():
        try:
            size = sum(f.stat().st_size for f in pdir.rglob("*") if f.is_file())
            out["profile_mb"] = round(size / 1e6, 1)
        except Exception:  # noqa: BLE001
            out["profile_mb"] = None
        locks = [n for n in ("SingletonLock", "SingletonSocket", "SingletonCookie")
                 if (pdir / n).exists() or (pdir / n).is_symlink()]
        out["profile_locks"] = locks
        ok(f"profile exists: {pdir}  ({out.get('profile_mb')} MB)")
        if locks:
            info(f"leftover singleton locks present: {locks} (BrowserSession clears "
                 f"these only when no live chromium holds the profile)")
    else:
        info(f"profile does not exist yet: {pdir} (a first login creates it)")

    # a chromium already holding this profile blocks the launch entirely
    try:
        from capture.browser import BrowserSession
        pid = BrowserSession(args.account)._profile_in_use()
        out["profile_in_use_pid"] = pid
        if pid:
            bad(f"a live chromium (pid {pid}) is already using this profile - "
                f"the bot's own browser, or a stuck one. Two processes cannot "
                f"share the profile.")
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------
# STAGE 2 - reachability without a browser
# --------------------------------------------------------------------------

def _probe_host(host: str, port: int = 443, timeout: float = 8.0) -> dict:
    r: dict = {"host": host}
    t = time.time()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        r["ips"] = sorted({i[4][0] for i in infos})
        r["dns_ms"] = int((time.time() - t) * 1000)
    except Exception as exc:  # noqa: BLE001
        r["dns_error"] = f"{type(exc).__name__}: {exc}"
        return r
    t = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            r["tcp_ms"] = int((time.time() - t) * 1000)
            t = time.time()
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(s, server_hostname=host) as ts:
                r["tls_ms"] = int((time.time() - t) * 1000)
                r["tls"] = ts.version()
                t = time.time()
                req = (f"GET / HTTP/1.1\r\nHost: {host}\r\n"
                       f"User-Agent: mkwl-login-diag\r\nConnection: close\r\n\r\n")
                ts.sendall(req.encode())
                head = ts.recv(4096).decode("utf-8", "ignore")
                r["http_ms"] = int((time.time() - t) * 1000)
                r["status"] = head.split("\r\n", 1)[0][:80]
    except Exception as exc:  # noqa: BLE001
        r["tcp_error"] = f"{type(exc).__name__}: {exc}"
    return r


def stage_net(args) -> dict:
    banner("STAGE 2  -  can this host reach Eitaa at all (no browser)")
    from config import config
    url = args.url or config.EITAA_WEB_URL
    main = re.sub(r"^https?://", "", url).split("/")[0]
    hosts = [main] + [h for h in ("majid.eitaa.com", "fateme.eitaa.com") if h != main]
    out = {}
    for h in hosts:
        r = _probe_host(h)
        out[h] = r
        if r.get("dns_error"):
            bad(f"{h}: DNS failed - {r['dns_error']}")
        elif r.get("tcp_error"):
            bad(f"{h}: resolved {r.get('ips')} but TCP/TLS failed - {r['tcp_error']}")
        else:
            ok(f"{h}: {r.get('ips')}  dns {r.get('dns_ms')}ms  tcp {r.get('tcp_ms')}ms  "
               f"tls {r.get('tls_ms')}ms {r.get('tls')}  ->  {r.get('status')}")
    if out.get(main, {}).get("tcp_error") or out.get(main, {}).get("dns_error"):
        verdict(f"Eitaa is NOT reachable from this host ({main}). The browser will "
                f"load nothing, so apiManager can never appear and the panel can "
                f"only say 'auth bridge unavailable'. Fix egress/hosting first - "
                f"nothing in the login code is wrong in that case.")
    return out


# --------------------------------------------------------------------------
# STAGE 3/4 - browser, page, SPA boot
# --------------------------------------------------------------------------

_STATE_JS = r"""
() => {
  const q = (s) => { try { return !!document.querySelector(s); } catch (e) { return false; } };
  const o = {};
  o.url = location.href;
  o.ready = document.readyState;
  o.title = document.title;
  o.htmlLen = ((document.documentElement && document.documentElement.innerHTML) || "").length;
  o.bodyText = ((document.body && document.body.innerText) || "").slice(0, 400);
  o.scripts = Array.prototype.slice.call(document.querySelectorAll("script[src]"))
      .map(s => s.src).slice(0, 25);
  o.apiManager      = !!window.apiManager;
  o.invokeApi       = !!(window.apiManager && window.apiManager.invokeApi);
  o.apiManagerProxy = !!window.apiManagerProxy;
  o.proxyInvokeApi  = !!(window.apiManagerProxy && window.apiManagerProxy.invokeApi);
  o.appUsersManager = !!window.appUsersManager;
  o.rootScope       = !!window.rootScope;
  o.bridgeSend      = (typeof window.__MKWL_sendCode === "function");
  try {
    const c = (window.Config && window.Config.App) || window.App || window.appConfig || null;
    o.pageCreds = c ? { id: c.id || c.api_id || c.apiId || null,
                        hash: !!(c.hash || c.api_hash || c.apiHash) } : null;
  } catch (e) { o.pageCreds = null; }
  o.loginUI = q("#auth-pages") || q(".page-signIn") || q(".page-sign") ||
              q("input.input-field-input");
  o.chatUI  = q("input.input-search-input") || q(".chatlist-chat");
  try { o.lsKeys = Object.keys(localStorage).slice(0, 40); }
  catch (e) { o.lsKeys = ["<localStorage blocked: " + e + ">"]; }
  try { o.idbNames = (window.indexedDB && indexedDB.databases) ? "supported" : "n/a"; }
  catch (e) { o.idbNames = "n/a"; }
  return o;
}
"""

_GLOBALS_JS = r"""
() => {
  const out = [];
  let keys = [];
  try { keys = Object.getOwnPropertyNames(window); } catch (e) { keys = []; }
  const rx = /manager|rootscope|appstate|auth|storage|session|api|proxy|invoke|mtproto|network/i;
  for (const k of keys) {
    if (!rx.test(k)) continue;
    let t = "?", inv = false, meth = [];
    try {
      const v = window[k];
      t = typeof v;
      if (v && (t === "object" || t === "function")) {
        inv = typeof v.invokeApi === "function";
        try {
          meth = Object.keys(v).filter(n => typeof v[n] === "function").slice(0, 8);
          if (!meth.length) {
            const p = Object.getPrototypeOf(v);
            if (p) meth = Object.getOwnPropertyNames(p)
                .filter(n => { try { return typeof v[n] === "function"; } catch (e) { return false; } })
                .slice(0, 8);
          }
        } catch (e) {}
      }
    } catch (e) { t = "<throws>"; }
    out.push({ key: k, type: t, invokeApi: inv, methods: meth });
  }
  return { total: keys.length, matched: out.slice(0, 80) };
}
"""


async def stage_page(session, args) -> dict:
    """Navigate like the bot does and watch the SPA boot, with timings."""
    from config import config
    page = session.page
    out: dict = {"console_errors": [], "page_errors": [], "failed_requests": [],
                 "doc_status": None, "crashed": False}

    page.on("console", lambda m: (
        out["console_errors"].append(f"{m.type}: {m.text[:240]}")
        if m.type in ("error", "warning") and len(out["console_errors"]) < 40 else None))
    page.on("pageerror", lambda e: out["page_errors"].append(str(e)[:240])
            if len(out["page_errors"]) < 20 else None)
    page.on("requestfailed", lambda r: out["failed_requests"].append(
        f"{r.resource_type} {r.url[:120]} <- {(r.failure or '')}")
        if len(out["failed_requests"]) < 40 else None)
    page.on("crash", lambda p: out.__setitem__("crashed", True))

    url = args.url or config.EITAA_WEB_URL
    banner(f"STAGE 4  -  load {url}  (same wait_until the driver uses)")
    t = time.time()
    try:
        # identical to capture/browser.py: goto(wait_until="domcontentloaded")
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        out["goto_s"] = round(time.time() - t, 1)
        out["doc_status"] = resp.status if resp else None
        ok(f"goto returned in {out['goto_s']}s   HTTP {out['doc_status']}")
        if resp is not None and resp.status >= 400:
            bad(f"the main document answered HTTP {resp.status} - the app is not "
                f"being served to this client (WAF/geo-block/changed URL)")
    except Exception as exc:  # noqa: BLE001
        out["goto_error"] = f"{type(exc).__name__}: {exc}"
        out["goto_s"] = round(time.time() - t, 1)
        bad(f"goto FAILED after {out['goto_s']}s - {out['goto_error']}")
        verdict("The page never loaded, so no in-page bridge can exist. This is a "
                "network/URL problem, not a login-code problem.")
        return out

    # EitaaDriver.open() then waits a flat 4 s before anything touches the page
    await page.wait_for_timeout(4000)

    st, err = await safe_eval(page, _STATE_JS)
    out["state_after_4s"] = st
    if err:
        out["state_error"] = err
        bad(f"could not read page state - {err}")
        return out
    info(f"url after load : {st['url']}")
    info(f"readyState     : {st['ready']}   title: {st['title']!r}")
    info(f"html bytes     : {st['htmlLen']}   scripts: {len(st['scripts'])}")
    info(f"localStorage   : {len(st.get('lsKeys') or [])} keys {short(st.get('lsKeys'), 160)}")
    info(f"login UI drawn : {st['loginUI']}    chat UI drawn: {st['chatUI']}")
    if st["chatUI"]:
        ok("this profile is ALREADY logged in (chat list is rendered) - a bridge "
           "login would have returned 'already logged in'")
    if st["htmlLen"] < 2000 and not st["loginUI"]:
        bad(f"the document is nearly empty ({st['htmlLen']} bytes) - the app shell "
            f"did not render")
    if st["bodyText"]:
        info(f"body text      : {short(st['bodyText'], 240)}")

    # ---- the decisive measurement: WHEN (if ever) the globals appear --------
    banner(f"STAGE 5  -  watch for the SPA globals (up to {args.wait}s)")
    info("ensure_login_bridge() in production waits only 30 x 500 ms = 15 s for")
    info("window.apiManager.invokeApi. This stage measures the real boot time.")
    watch = ["apiManager", "invokeApi", "apiManagerProxy", "proxyInvokeApi",
             "appUsersManager", "rootScope", "loginUI", "chatUI"]
    first: dict = {k: None for k in watch}
    deadline = time.time() + float(args.wait)
    last = st
    polls = 0
    while time.time() < deadline:
        polls += 1
        cur, e = await safe_eval(page, _STATE_JS)
        if e:
            out["watch_error"] = e
            bad(f"page stopped answering during the watch - {e}")
            break
        last = cur
        el = round(time.time() - (deadline - float(args.wait)), 1)
        for k in watch:
            if first[k] is None and cur.get(k):
                first[k] = el
                ok(f"{k} appeared after {el}s")
        if first["invokeApi"] is not None:
            break
        await asyncio.sleep(1.0)
    out["first_seen"] = first
    out["polls"] = polls
    out["state_final"] = last
    for k in watch:
        if first[k] is None:
            info(f"{k}: NEVER appeared within {args.wait}s")

    if out["crashed"]:
        bad("the renderer CRASHED (page 'crash' event) - almost always OOM on a "
            "1 GB host; every later evaluate would raise and be swallowed")

    if out["page_errors"]:
        info(f"page errors ({len(out['page_errors'])}): {short(out['page_errors'], 400)}")
    if out["console_errors"]:
        info(f"console ({len(out['console_errors'])}): {short(out['console_errors'], 400)}")
    blocked_expected = {"image", "media", "font"}
    hard = [f for f in out["failed_requests"]
            if not any(f.startswith(t + " ") for t in blocked_expected)]
    if hard:
        bad(f"{len(hard)} non-asset request(s) failed - e.g. {short(hard[:5], 400)}")
    elif out["failed_requests"]:
        info(f"{len(out['failed_requests'])} blocked asset requests (expected when "
             f"MKWL_LIGHT_ASSETS=1)")

    # ---- what globals DOES this build expose -------------------------------
    banner("STAGE 6  -  which auth/api globals this Eitaa build actually exposes")
    g, e = await safe_eval(page, _GLOBALS_JS)
    out["globals"] = g
    if e:
        bad(f"could not enumerate globals - {e}")
    else:
        info(f"window has {g['total']} own properties; {len(g['matched'])} look "
             f"api/auth/manager related:")
        for item in g["matched"]:
            mark = "  <-- has invokeApi" if item["invokeApi"] else ""
            print(f"        {item['key']:<28} {item['type']:<9}{mark}"
                  f"   {item['methods'][:5]}", flush=True)
        holders = [i["key"] for i in g["matched"] if i["invokeApi"]]
        out["invoke_holders"] = holders
        if holders:
            ok(f"objects that own invokeApi: {holders}")
        else:
            bad("NO global on this page owns invokeApi - every bridge in this repo "
                "(login, send, contacts, stats, presence) depends on "
                "window.apiManager.invokeApi")
    return out


# --------------------------------------------------------------------------
# STAGE 7 - replay ensure_login_bridge() step by step
# --------------------------------------------------------------------------

async def stage_bridge(session, args) -> dict:
    banner("STAGE 7  -  replay ensure_login_bridge() with the errors NOT swallowed")
    page = session.page
    out: dict = {}

    src_path = Path(_ROOT) / "eitaa" / "login_bridge.js"
    try:
        src = src_path.read_text(encoding="utf-8")
        out["src_len"] = len(src)
        ok(f"step 0: login_bridge.js read, {len(src)} chars ({src_path})")
    except Exception as exc:  # noqa: BLE001
        out["src_error"] = str(exc)
        bad(f"step 0: cannot read {src_path} - {exc}")
        verdict("login_flow._LOGIN_SRC is empty, so ensure_login_bridge() returns "
                "False immediately. This alone produces 'auth bridge unavailable'.")
        return out

    # step 1 - the typeof probe (production turns ANY error here into has=False)
    has, err = await safe_eval(page, "() => typeof window.__MKWL_sendCode === 'function'")
    out["already_injected"] = bool(has)
    out["probe_error"] = err
    if err:
        bad(f"step 1: the typeof probe raised - {err}")
        verdict("The page/renderer is not usable (closed, crashed or navigating). "
                "login_flow catches this and reports 'auth bridge unavailable'.")
    else:
        info(f"step 1: bridge already present? {bool(has)}")

    # step 2 - injection
    if not has:
        t = time.time()
        _, err = await safe_eval(page, src)
        out["inject_error"] = err
        out["inject_s"] = round(time.time() - t, 2)
        if err:
            bad(f"step 2: INJECTION FAILED - {err}")
            verdict("page.evaluate(login_bridge.js) raised. login_flow.py:118 "
                    "swallows exactly this exception. Cause is usually a dead page "
                    "or a navigation in flight - not the JS itself.")
            return out
        ok(f"step 2: injected in {out['inject_s']}s")
        now, err2 = await safe_eval(page, "() => typeof window.__MKWL_sendCode")
        out["after_inject_typeof"] = now
        if now != "function":
            bad(f"step 2b: after injection typeof __MKWL_sendCode == {now!r} - the "
                f"IIFE did not define it")

    # step 3 - the 15 s wait for apiManager.invokeApi, measured honestly
    t = time.time()
    seen = None
    for i in range(30):                     # same 30 x 500 ms as production
        v, err = await safe_eval(page, "() => !!(window.apiManager && window.apiManager.invokeApi)")
        if err:
            out["ready_error"] = err
            bad(f"step 3: the readiness probe raised - {err}")
            break
        if v:
            seen = round(time.time() - t, 2)
            break
        await asyncio.sleep(0.5)
    out["ready_within_15s"] = seen
    if seen is not None:
        ok(f"step 3: apiManager.invokeApi was ready after {seen}s -> "
           f"ensure_login_bridge() WOULD return True now")
    else:
        bad("step 3: apiManager.invokeApi NOT ready within the production 15 s "
            "window -> ensure_login_bridge() returns False -> the panel prints "
            "'auth bridge unavailable'. THIS IS THE FAILING LINK.")

    # what the bridge's own probe sees (works even when invokeApi is missing)
    probe, err = await safe_eval(
        page, "() => window.__MKWL_authProbe ? window.__MKWL_authProbe() : null")
    out["auth_probe"] = probe
    out["auth_probe_error"] = err
    if probe:
        info("__MKWL_authProbe():")
        for line in json.dumps(probe, ensure_ascii=False, indent=2).splitlines():
            print("        " + line, flush=True)

    # the real production call, for the record - returns the exact panel string
    try:
        from eitaa.driver import EitaaDriver
        from eitaa.login_flow import ensure_login_bridge
        res = await ensure_login_bridge(EitaaDriver(session))
        out["ensure_login_bridge"] = bool(res)
        (ok if res else bad)(f"production ensure_login_bridge() -> {res}")
        if not res:
            verdict("Reproduced: ensure_login_bridge() returns False, which is "
                    "exactly the 'auth bridge unavailable' the panel showed.")
    except Exception as exc:  # noqa: BLE001
        out["ensure_error"] = f"{type(exc).__name__}: {exc}"
        bad(f"production ensure_login_bridge() raised - {out['ensure_error']}")
    return out


# --------------------------------------------------------------------------
# STAGE 8 - prove the transport without burning a code
# --------------------------------------------------------------------------

_RPC_JS = r"""
async (which) => {
  const AM = window[which];
  if (!AM || !AM.invokeApi) return { ok: false, code: "no invokeApi on " + which };
  const t = Date.now();
  try {
    const res = await AM.invokeApi("help.getNearestDc", {});
    return { ok: true, ms: Date.now() - t, reply: res };
  } catch (e) {
    let s = "ERR";
    try { s = String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) {}
    return { ok: false, ms: Date.now() - t, code: s };
  }
}
"""


async def stage_rpc(session, holders, args) -> dict:
    banner("STAGE 8  -  harmless RPC: help.getNearestDc  (no login code is sent)")
    out = {}
    cands = [h for h in (holders or []) if h] or ["apiManager", "apiManagerProxy"]
    for name in dict.fromkeys(cands):
        r, err = await safe_eval(session.page, _RPC_JS, name)
        out[name] = r or {"evaluate_error": err}
        if err:
            bad(f"{name}: evaluate error - {err}")
        elif r and r.get("ok"):
            ok(f"{name}.invokeApi('help.getNearestDc') -> {r['ms']}ms  "
               f"{short(r.get('reply'), 200)}")
            verdict(f"The in-page MTProto transport WORKS through window.{name} "
                    f"({r['ms']}ms). So the pipe to Eitaa is fine; the break is "
                    f"above it.")
        elif r:
            bad(f"{name}: invokeApi failed after {r.get('ms')}ms - {r.get('code')}")
    if not out:
        info("no candidate object to call - skipped")
    return out


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------

def build_verdict(env, net, pg, br) -> None:
    banner("VERDICT  -  where the chain breaks, and what to do")
    pg = pg or {}
    br = br or {}
    first = pg.get("first_seen") or {}
    st = pg.get("state_final") or pg.get("state_after_4s") or {}
    holders = pg.get("invoke_holders") or []

    if not pg or pg.get("goto_error"):
        pass  # already explained in stage 2/4
    elif pg.get("crashed"):
        verdict("The renderer crashed (OOM is the usual cause on ~1 GB). Raise the "
                "host's RAM/swap, keep MKWL_POOL_MAX_OPEN=1, and re-run.")
    elif first.get("invokeApi") is None and holders:
        verdict(f"ROOT CAUSE: this Eitaa build does NOT expose "
                f"window.apiManager.invokeApi, but {holders} DOES own invokeApi. "
                f"Every bridge in the repo hardcodes window.apiManager "
                f"(login_bridge.js, bridge_send.js, bridge_file_send.js, "
                f"contacts_bridge.js, contacts_list.js, stats_bridge.js, "
                f"presence.js). The fix is one shared resolver that prefers the "
                f"global that actually has invokeApi - i.e. Eitaa changed its web "
                f"app, not your server.")
    elif first.get("invokeApi") is None and not holders:
        if st.get("loginUI") or st.get("chatUI"):
            verdict("ROOT CAUSE: the Eitaa app RENDERS (so network, URL and profile "
                    "are fine) but no global exposes invokeApi any more. Eitaa's "
                    "web build changed - the apiManager is no longer reachable from "
                    "the main world (most likely it moved into a worker / module "
                    "scope). The bridges need a new entry point; see STAGE 6's list "
                    "for what this build does expose.")
        else:
            verdict("ROOT CAUSE: the app never booted at all - no login UI, no chat "
                    "UI, no globals. Look at STAGE 2 (reachability) and the failed "
                    "requests in STAGE 5: the shell HTML arrives but its JS does "
                    "not, which is what a filtered/partial egress looks like.")
    elif br.get("ready_within_15s") is None and first.get("invokeApi") is not None:
        verdict(f"ROOT CAUSE: apiManager.invokeApi DOES appear, but only after "
                f"~{first['invokeApi']}s, while ensure_login_bridge() gives up at "
                f"15 s (30 x 500 ms, eitaa/login_flow.py:96-104). The login is "
                f"failing on a timeout, not on a missing feature. Raise that window "
                f"(and make it configurable) and this login starts working.")
    elif br.get("ensure_login_bridge"):
        verdict("The bridge comes up fine in this run. So the failure is "
                "intermittent/conditional - the most likely differences between "
                "this run and the bot's: a POOLED page that was already navigated "
                "or half-dead (re-run with --pool), headless vs headed, "
                "MKWL_WARMPATH=1 reusing a stale page, or load/RAM at that moment. "
                "Re-run with --pool and compare.")

    if not REPORT["verdict"]:
        verdict("No single broken link was identified - send the JSON report.")
    for i, line in enumerate(REPORT["verdict"], 1):
        print(f"\n  {i}) {line}", flush=True)

    print("\n  Next, if you want the delivery question (App vs SMS) answered, use "
          "the existing\n  probe_login_sms.py - that one DOES request a real code. "
          "This script never does.", flush=True)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

async def run(args) -> int:
    REPORT["args"] = vars(args)
    REPORT["started"] = time.strftime("%Y-%m-%d %H:%M:%S")

    env = net = pg = br = None
    try:
        env = stage_env(args)
        REPORT["stages"]["env"] = env
    except Exception:  # noqa: BLE001
        REPORT["stages"]["env_traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)
    try:
        net = stage_net(args)
        REPORT["stages"]["net"] = net
    except Exception:  # noqa: BLE001
        REPORT["stages"]["net_traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)

    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"login_diag_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)
    REPORT["outdir"] = str(outdir)

    banner("STAGE 3  -  open the browser the way the bot does")
    light = False if args.no_light else None     # None = honour MKWL_LIGHT_ASSETS
    t = time.time()
    session = None
    lease = None
    try:
        if args.pool:
            from capture.pool import session_pool
            info("leasing from capture/pool.py (identical to bot/runner.py)")
            lease = session_pool.lease(args.account, headed=args.headed)
            session = await lease.__aenter__()
        else:
            from capture.browser import BrowserSession
            info("fresh BrowserSession (pool bypassed, so the pool is ruled out)")
            session = await BrowserSession(args.account, headed=args.headed,
                                           light_assets=light).start()
        ok(f"browser up in {time.time() - t:.1f}s")
        REPORT["stages"]["browser_s"] = round(time.time() - t, 1)
        try:
            v = session.context.browser.version if session.context.browser else "?"
            info(f"chromium: {v}")
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        bad(f"browser FAILED to start after {time.time() - t:.1f}s - "
            f"{type(exc).__name__}: {exc}")
        REPORT["stages"]["browser_error"] = traceback.format_exc()
        verdict("Chromium never started, so the bot could not even reach the bridge "
                "step. Causes seen on this host: a leftover profile lock, another "
                "chromium on the same profile, no playwright browsers installed "
                "(`playwright install chromium`), or OOM.")
        build_verdict(env, net, None, None)
        (outdir / "report.json").write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n  report: {outdir / 'report.json'}", flush=True)
        return 2

    try:
        pg = await stage_page(session, args)
        REPORT["stages"]["page"] = pg
        br = await stage_bridge(session, args)
        REPORT["stages"]["bridge"] = br
        if not args.no_rpc:
            REPORT["stages"]["rpc"] = await stage_rpc(
                session, pg.get("invoke_holders"), args)

        # artifacts: a screenshot and the DOM are worth more than any guess
        try:
            await session.page.screenshot(path=str(outdir / "page.png"), full_page=False)
            html = await session.page.content()
            (outdir / "page.html").write_text(html, encoding="utf-8")
            info(f"saved page.png + page.html ({len(html)} chars) to {outdir}")
        except Exception as exc:  # noqa: BLE001
            info(f"could not save artifacts: {exc}")

        if args.keep_open:
            info(f"keeping the browser open for {args.keep_open}s "
                 f"(look at it over VNC if you have one)")
            await asyncio.sleep(float(args.keep_open))
    except Exception:  # noqa: BLE001
        REPORT["stages"]["traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)
    finally:
        try:
            if lease is not None:
                await lease.__aexit__(None, None, None)
            elif session is not None:
                await session.close()
        except Exception:  # noqa: BLE001
            pass

    build_verdict(env, net, pg, br)
    REPORT["findings_count"] = len(REPORT["findings"])
    (outdir / "report.json").write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  full machine-readable report: {outdir / 'report.json'}", flush=True)
    print(f"  screenshot / DOM            : {outdir}", flush=True)
    return 0 if (br or {}).get("ensure_login_bridge") else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pinpoint where a bridge login breaks. Never requests a login code.")
    p.add_argument("--account", required=True,
                   help="account id exactly as the panel uses it, e.g. 989939094655")
    p.add_argument("--wait", type=float, default=120,
                   help="seconds to watch for the SPA globals (default 120)")
    p.add_argument("--pool", action="store_true",
                   help="lease through capture/pool.py like the bot does")
    p.add_argument("--headed", action="store_true",
                   help="real window (needs DISPLAY, e.g. xvfb) instead of headless")
    p.add_argument("--no-light", action="store_true",
                   help="do not block image/media/font (MKWL_LIGHT_ASSETS=0)")
    p.add_argument("--url", default=None, help="override EITAA_WEB_URL for this run")
    p.add_argument("--no-rpc", action="store_true",
                   help="skip the harmless help.getNearestDc probe")
    p.add_argument("--keep-open", type=float, default=0,
                   help="keep the browser open N seconds at the end")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
