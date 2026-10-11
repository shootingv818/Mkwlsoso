#!/usr/bin/env python3
"""GO / NO-GO preflight: test the proposed fix BEFORE any project file changes.

Why this script exists
----------------------
Three probes established the following, with evidence:

* `window.apiManager` is gone. Eitaa now runs the multi-account build of
  Telegram Web K: the managers live in a SharedWorker (`mtproto.worker-*.js`)
  and the page reaches them over a MessagePort.
* `rootScope.managers.apiManager` produces a task frame byte-identical to the
  one Eitaa's own code sends:
      {type:"manager", payload:{name:"apiManager", method:"invokeApi",
                                args:[...], accountNumber:1}, withAck:false}
* The port round-trip is healthy: a raw `apiManagerProxy.invoke("invokeApi", …)`
  came back in the same millisecond with
      {type:"result", payload:{taskId:45.68…, error:{message:"no listener"}}}
  - a matching taskId, which proves requests and results correlate. "no
  listener" simply means the top-level task type must be `manager`, not
  `invokeApi`.
* The one method we replayed, `help.getNearestDc`, never answers - and Eitaa's
  OWN call to it never answered either. That was a bad probe choice, not a
  broken pipe.

What is therefore still unproven, and is the only thing standing between us and
editing the code: a call that comes back with a REAL VALUE. This script proves
or disproves exactly that, then checks every other precondition the fix needs,
and ends with a GO / NO-GO table. Nothing is changed until it says GO.

How it tests the fix without applying it
----------------------------------------
Section 2 injects `__MKWL_api()` - the exact resolver that would go into the
seven bridge files - and runs the real calls through it. If it works here, it
works there, because it is the same code.

Safety
------
* `auth.sendCode` is NEVER called: no login code is requested, nothing is sent
  to anybody, and a FORBIDDEN guard refuses any method that could.
* Every call is read-only (getConfig / getState / help.getConfig).
* No existing project file is touched. The recorder goes to a temp file and is
  passed to BrowserSession's existing init_script_path parameter; output lands
  in the gitignored artifacts/ dir.
* Every call is capped, so the script always finishes and always prints a
  verdict.

Run on the SERVER:

    cd ~/Mkwlsoso && .venv/bin/python deploy/preflight_fix_check.py --account 989939094655

Options:
    --wait 10           seconds to let the app settle (default 10)
    --call-timeout 8    per-call ceiling (default 8s)
    --acc 1             accountNumber used in manager tasks (default 1)
    --headed / --no-light / --keep-open N
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"checks": {}, "calls": [], "verdict": []}
CHECKS: list = []          # (name, passed: bool|None, detail)
_T0 = time.time()

FORBIDDEN = re.compile(
    r"^(auth\.|sendCode|signIn|messages\.send|messages\.forward|contacts\.import|"
    r"contacts\.add|account\.update|upload\.|photos\.upload)", re.I)


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


def check(name: str, passed, detail: str = "") -> None:
    CHECKS.append((name, passed, detail))
    REPORT["checks"][name] = {"passed": passed, "detail": detail}
    mark = "OK  " if passed is True else ("FAIL" if passed is False else "WARN")
    log(f"{mark}  [{name}] {detail}")


def dump(label: str, obj, indent: str = "        ") -> None:
    info(label)
    for line in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + line, flush=True)


async def ev(page, js: str, arg=None, timeout: float = 20.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout: no answer within {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# recorder with request/result correlation
# ==========================================================================

RECORDER_JS = r"""
// MKWL preflight recorder: records port/worker traffic AND correlates a
// request's task id with the result frame that carries the same taskId, which
// is what lets the probe say "answered" rather than "probably answered".
// Read-only: it records and forwards; it never blocks or alters a message.
(() => {
  if (window.__MKWL_PORTLOG) return;
  const CAP = 1200;
  const log = [];
  const results = {};          // taskId -> result payload
  window.__MKWL_PORTLOG = log;
  window.__MKWL_RESULTS = results;
  let seq = 0;

  function brief(v, d) {
    d = d || 0;
    try {
      if (v === null) return null;
      if (v === undefined) return "<undefined>";
      const t = typeof v;
      if (t === "string") return v.length > 300 ? v.slice(0, 300) + "..." : v;
      if (t === "number" || t === "boolean") return v;
      if (t === "function") return "<fn>";
      if (ArrayBuffer.isView(v) || v instanceof ArrayBuffer)
        return "<bytes " + (v.byteLength || v.length || 0) + ">";
      if (v instanceof MessagePort) return "<MessagePort>";
      if (Array.isArray(v)) return d >= 8 ? "<array>" : v.slice(0, 20).map(x => brief(x, d + 1));
      if (t === "object") {
        if (d >= 8) return "<object>";
        const o = {};
        let keys = [];
        try { keys = Object.keys(v); } catch (e) { return "<unkeyable>"; }
        for (const k of keys.slice(0, 40)) {
          try { o[k] = brief(v[k], d + 1); } catch (e) { o[k] = "<throws>"; }
        }
        return o;
      }
      return String(t);
    } catch (e) { return "<brief-failed>"; }
  }

  // Pull every {type:'result'} out of a frame, including inside a batch.
  function harvestResults(data) {
    try {
      const walk = (x) => {
        if (!x || typeof x !== "object") return;
        if (Array.isArray(x)) { x.forEach(walk); return; }
        if (x.type === "result" && x.payload && x.payload.taskId !== undefined) {
          results[String(x.payload.taskId)] = brief(x.payload, 0);
        }
        if (x.payload) walk(x.payload);
      };
      walk(data);
    } catch (e) {}
  }

  function push(dir, kind, data) {
    if (dir === "in") harvestResults(data);
    if (log.length >= CAP) return;
    try { log.push({ n: ++seq, ms: Date.now(), dir: dir, kind: kind, data: brief(data, 0) }); }
    catch (e) {}
  }
  window.__MKWL_seq = () => seq;
  window.__MKWL_since = (n) => log.filter(e => e.n > n);
  // The ids our own call produced, so the probe can look up their results.
  window.__MKWL_taskIdsSince = (n) => {
    const ids = [];
    const walk = (x) => {
      if (!x || typeof x !== "object") return;
      if (Array.isArray(x)) { x.forEach(walk); return; }
      if (x.type === "invoke" && x.id !== undefined) ids.push(x.id);
      if (x.payload) walk(x.payload);
    };
    log.filter(e => e.n > n && e.dir === "out").forEach(e => walk(e.data));
    return ids;
  };

  function wrapIn(fn, kind) {
    return function (ev) {
      try { push("in", kind, ev && ev.data); } catch (e) {}
      return fn.apply(this, arguments);
    };
  }
  try {
    const P = MessagePort.prototype;
    const op = P.postMessage;
    P.postMessage = function () {
      try { push("out", "port", arguments[0]); } catch (e) {}
      return op.apply(this, arguments);
    };
    const oa = P.addEventListener;
    P.addEventListener = function (type, fn) {
      if (type === "message" && typeof fn === "function") {
        const a = Array.prototype.slice.call(arguments);
        a[1] = wrapIn(fn, "port");
        return oa.apply(this, a);
      }
      return oa.apply(this, arguments);
    };
    const d = Object.getOwnPropertyDescriptor(P, "onmessage");
    if (d && d.set) {
      Object.defineProperty(P, "onmessage", {
        configurable: true, enumerable: d.enumerable, get: d.get,
        set: function (fn) {
          return d.set.call(this, typeof fn === "function" ? wrapIn(fn, "port") : fn);
        }
      });
    }
  } catch (e) {}
  ["Worker", "SharedWorker"].forEach((name) => {
    try {
      const Orig = window[name];
      if (!Orig) return;
      const P2 = function (url, opts) {
        const w = new Orig(url, opts);
        try {
          const t = name === "SharedWorker" ? w.port : w;
          const op2 = t.postMessage.bind(t);
          t.postMessage = function () {
            try { push("out", name.toLowerCase(), arguments[0]); } catch (e) {}
            return op2.apply(null, arguments);
          };
          t.addEventListener("message", (e2) => {
            try { push("in", name.toLowerCase(), e2 && e2.data); } catch (e3) {}
          });
          if (name === "SharedWorker" && t.start) t.start();
        } catch (e) {}
        return w;
      };
      P2.prototype = Orig.prototype;
      window[name] = P2;
    } catch (e) {}
  });
})();
"""


# ==========================================================================
# THE PROPOSED FIX, injected verbatim so the preflight tests the real thing
# ==========================================================================

RESOLVER_JS = r"""
// ==== THE CANDIDATE FIX ====================================================
// This is exactly what would be shared by the seven bridge files. It is
// injected here unchanged, so whatever this preflight proves is what the
// bridges will do.
//
// Order matters: the OLD global is preferred when present, so the fix keeps
// working if Eitaa ever serves the old build again, or on a host that cached
// it. Only then does it fall back to the worker-backed managers proxy.
(() => {
  window.__MKWL_apiSource = null;

  window.__MKWL_managers = function () {
    // new build A: the managers object tweb itself uses
    try {
      const r = window.rootScope && window.rootScope.managers;
      if (r && r.apiManager) { window.__MKWL_apiSource = "rootScope.managers"; return r; }
    } catch (e) {}
    // new build B: build a fresh proxied set for this account slot
    try {
      const f = window.createProxiedManagersForAccount;
      if (typeof f === "function") {
        const m = f(window.__MKWL_accountNumber || 1);
        if (m && m.apiManager) { window.__MKWL_apiSource = "createProxiedManagersForAccount"; return m; }
      }
    } catch (e) {}
    return null;
  };

  // Returns an object with .invokeApi(method, params), whichever build we are on.
  window.__MKWL_api = function () {
    // old build: the global the current bridges hardcode
    try {
      if (window.apiManager && window.apiManager.invokeApi) {
        window.__MKWL_apiSource = "window.apiManager";
        return window.apiManager;
      }
    } catch (e) {}
    const m = window.__MKWL_managers();
    return m ? m.apiManager : null;
  };

  // Ready means: we can resolve something that exposes invokeApi. The old
  // readiness check polled for window.apiManager only, which is why login
  // reported 'auth bridge unavailable' on this build.
  window.__MKWL_apiReady = function () {
    try {
      const a = window.__MKWL_api();
      return !!(a && typeof a.invokeApi === "function");
    } catch (e) { return false; }
  };
})();
"""

CALL_JS = r"""
async (a) => {
  const before = window.__MKWL_seq ? window.__MKWL_seq() : 0;
  const LIMIT = a.timeout_ms || 8000;
  const t0 = Date.now();
  const out = { label: a.label, before: before };
  const cap = (p) => Promise.race([
    p, new Promise((_, rej) => setTimeout(() => rej(new Error("MKWL_TIMEOUT " + LIMIT + "ms")), LIMIT))
  ]);

  let target = null;
  try {
    if (a.on === "api") target = window.__MKWL_api();
    else {
      const m = window.__MKWL_managers();
      target = m ? m[a.on] : null;
    }
  } catch (e) { out.resolve_error = String(e); }
  out.source = window.__MKWL_apiSource;
  if (!target) { out.ok = false; out.code = "could not resolve " + a.on; return out; }

  try {
    const fn = target[a.fn];
    if (typeof fn !== "function") { out.ok = false; out.code = "no " + a.fn; return out; }
    const r = await cap(Promise.resolve(fn.apply(target, a.args || [])));
    out.ok = true;
    out.ms = Date.now() - t0;
    try {
      out.value_type = typeof r;
      out.value_keys = (r && typeof r === "object") ? Object.keys(r).slice(0, 20) : null;
      out.value = JSON.stringify(r === undefined ? null : r).slice(0, 600);
    } catch (e) { out.value = "<unserializable>"; }
  } catch (e) {
    out.ok = false;
    out.ms = Date.now() - t0;
    try { out.code = String((e && (e.type || e.error_message || e.message || e.code)) || e); }
    catch (x) { out.code = "ERR"; }
  }

  // Correlate: did the worker answer the very task our call produced?
  try {
    const ids = window.__MKWL_taskIdsSince ? window.__MKWL_taskIdsSince(before) : [];
    out.task_ids = ids;
    const res = {};
    for (const id of ids) {
      const r = window.__MKWL_RESULTS[String(id)];
      if (r) res[String(id)] = r;
    }
    out.results_for_our_tasks = res;
    out.answered_count = Object.keys(res).length;
    out.new_frames = (window.__MKWL_seq ? window.__MKWL_seq() : 0) - before;
  } catch (e) {}
  return out;
}
"""

SURFACE_JS = r"""
() => {
  const t = (p) => { try { return typeof p.split(".").reduce((a, k) => (a == null ? a : a[k]), window); } catch (e) { return "<throws>"; } };
  const src = (p) => {
    try {
      const v = p.split(".").reduce((a, k) => (a == null ? a : a[k]), window);
      return typeof v === "function" ? String(v).slice(0, 200) : null;
    } catch (e) { return null; }
  };
  const out = {};
  // the old finalize path the current code uses
  out["apiManager (old global)"] = t("apiManager");
  out["apiManager.setUserAuth (old)"] = t("apiManager.setUserAuth");
  out["apiManagerProxy.setUserAuth (old)"] = t("apiManagerProxy.setUserAuth");
  out["appUsersManager (old global)"] = t("appUsersManager");
  // the new finalize path
  out["apiManagerProxy.dispatchUserAuth"] = t("apiManagerProxy.dispatchUserAuth");
  out["apiManagerProxy.dispatchUserAuth src"] = src("apiManagerProxy.dispatchUserAuth");
  out["apiManagerProxy.invoke"] = t("apiManagerProxy.invoke");
  out["apiManagerProxy.switchAccount"] = t("apiManagerProxy.switchAccount");
  out["telegramMeWebManager.setAuthorized"] = t("telegramMeWebManager.setAuthorized");
  out["rootScope.dispatchEvent"] = t("rootScope.dispatchEvent");
  // in-page api credentials fallback used by login_flow.resolve_creds_with_page
  try {
    const c = (window.Config && window.Config.App) || window.App || window.appConfig || null;
    out["in-page api creds"] = c ? { id: c.id || c.api_id || c.apiId || null,
                                     hash: !!(c.hash || c.api_hash || c.apiHash) } : null;
  } catch (e) { out["in-page api creds"] = null; }
  // managers the OTHER bridges need (send, contacts, stats, presence)
  try {
    const m = window.__MKWL_managers ? window.__MKWL_managers() : null;
    out["managers resolved"] = !!m;
    out["managers source"] = window.__MKWL_apiSource;
    if (m) {
      for (const n of ["apiManager", "appUsersManager", "appMessagesManager",
                       "appPeersManager", "appStateManager", "appProfileManager"]) {
        out["managers." + n] = (() => { try { return typeof m[n]; } catch (e) { return "<throws>"; } })();
      }
    }
  } catch (e) {}
  out["login UI drawn"] = !!document.querySelector("#auth-pages, .page-signIn, input.input-field-input");
  out["chat UI drawn"] = !!document.querySelector("input.input-search-input, .chatlist-chat");
  return out;
}
"""


async def run(args) -> int:
    REPORT["args"] = vars(args)
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"preflight_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    banner("SECTION 0  -  setup")
    tmp = Path(tempfile.gettempdir()) / f"mkwl_preflight_{os.getpid()}.js"
    tmp.write_text(RECORDER_JS, encoding="utf-8")
    ok(f"recorder (with request/result correlation) -> {tmp}")
    info("auth.sendCode is never called by this script; a guard refuses it.")

    from capture.browser import BrowserSession
    url = args.url or config.EITAA_WEB_URL
    light = False if args.no_light else None
    t = time.time()
    try:
        session = await BrowserSession(args.account, headed=args.headed,
                                       init_script_path=tmp, light_assets=light).start()
    except Exception as exc:  # noqa: BLE001
        bad(f"browser failed to start - {type(exc).__name__}: {exc}")
        check("browser starts", False, str(exc))
        REPORT["browser_error"] = traceback.format_exc()
        return 2
    ok(f"browser up in {time.time() - t:.1f}s")
    check("browser starts", True, f"{time.time() - t:.1f}s")
    page = session.page

    try:
        banner(f"SECTION 1  -  load {url}")
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            st = resp.status if resp else None
            check("page loads", st == 200, f"HTTP {st}")
        except Exception as exc:  # noqa: BLE001
            check("page loads", False, str(exc))
            bad(f"goto failed - {exc}")
            return 2
        await asyncio.sleep(float(args.wait))
        n, _ = await ev(page, "() => (window.__MKWL_PORTLOG || []).length", timeout=10)
        check("app boots and talks to its worker", bool(n and n > 20), f"{n} frames recorded")

        banner("SECTION 2  -  inject the CANDIDATE FIX and see what it resolves")
        info("this is the same resolver that would go into the seven bridges")
        _, err = await ev(page, f"() => {{ {RESOLVER_JS} }}", timeout=15)
        if err:
            check("resolver injects", False, err)
            bad(f"could not inject the resolver - {err}")
        else:
            check("resolver injects", True, "ok")
        await ev(page, f"(n) => {{ window.__MKWL_accountNumber = n; }}", int(args.acc), timeout=10)

        src, err = await ev(page, "() => { const a = window.__MKWL_api(); "
                                  "return {resolved: !!a, source: window.__MKWL_apiSource, "
                                  "ready: window.__MKWL_apiReady()}; }", timeout=15)
        REPORT["resolver"] = src or {"error": err}
        if src and src.get("resolved"):
            ok(f"resolver picked: {src.get('source')}   ready={src.get('ready')}")
            check("resolver finds an api object", True, str(src.get("source")))
            check("new readiness check returns true", bool(src.get("ready")),
                  "this is what replaces the 15s wait for window.apiManager")
        else:
            bad(f"resolver found nothing - {err or src}")
            check("resolver finds an api object", False, str(err or src))

        banner("SECTION 3  -  PROOF: calls that must come back with a real value")
        info("each call is correlated by taskId with the worker's result frame,")
        info("so 'answered' means the worker really replied to OUR task.")
        calls = [
            # getConfig is what Eitaa's own code called in frame 76, right next to
            # the getNearestDc that never answers. It is a manager method, not a
            # raw invokeApi, and it is the single most important check here.
            {"label": "apiManager.getConfig()", "on": "api", "fn": "getConfig", "args": []},
            # the same channel the login will use, with a method that exists
            {"label": "apiManager.invokeApi('help.getConfig', {})", "on": "api",
             "fn": "invokeApi", "args": ["help.getConfig", {}]},
            # a second manager, to prove the whole managers set is reachable and
            # not just apiManager (the send/contacts bridges need these)
            {"label": "appStateManager.getState()", "on": "appStateManager",
             "fn": "getState", "args": []},
        ]
        answered = 0
        for c in calls:
            for a in c["args"]:
                if isinstance(a, str) and FORBIDDEN.match(a):
                    bad(f"refusing {c['label']} - guarded method")
                    c = None
                    break
            if not c:
                continue
            if FORBIDDEN.match(c["fn"]):
                bad(f"refusing {c['label']} - guarded method")
                continue
            c["timeout_ms"] = int(args.call_timeout * 1000)
            r, e = await ev(page, CALL_JS, c, timeout=args.call_timeout + 8)
            REPORT["calls"].append({"call": c, "result": r or {"evaluate_error": e}})
            if e or not r:
                bad(f"{c['label']}: {e}")
                continue
            if r.get("ok"):
                answered += 1
                ok(f"{c['label']} -> ANSWERED in {r['ms']}ms via {r.get('source')}")
                info(f"    type={r.get('value_type')} keys={r.get('value_keys')}")
                info(f"    value: {str(r.get('value'))[:400]}")
            else:
                bad(f"{c['label']}: {r.get('code')}  "
                    f"({r.get('new_frames')} frames, "
                    f"{r.get('answered_count')} of our tasks answered)")
            if r.get("results_for_our_tasks"):
                dump("    worker result frames matching OUR task ids:",
                     r.get("results_for_our_tasks"))

        check("a real call returns a real value", answered > 0,
              f"{answered} of {len(calls)} calls answered")

        banner("SECTION 4  -  the login-specific surface")
        surf, err = await ev(page, SURFACE_JS, timeout=15)
        REPORT["surface"] = surf or {"error": err}
        if surf:
            dump("what exists on this build:", surf)
            check("old globals really are gone",
                  surf.get("apiManager (old global)") == "undefined",
                  f"window.apiManager is {surf.get('apiManager (old global)')} - "
                  f"confirms the resolver must fall back, and that the old code "
                  f"could never have worked")
            new_finalize = surf.get("apiManagerProxy.dispatchUserAuth") == "function"
            check("a replacement for setUserAuth exists", new_finalize,
                  f"apiManagerProxy.dispatchUserAuth is "
                  f"{surf.get('apiManagerProxy.dispatchUserAuth')}; old "
                  f"setUserAuth is {surf.get('apiManager.setUserAuth (old)')}")
            if surf.get("apiManagerProxy.dispatchUserAuth src"):
                info(f"    dispatchUserAuth source: "
                     f"{surf['apiManagerProxy.dispatchUserAuth src'][:160]}")
            check("telegramMeWebManager.setAuthorized present",
                  surf.get("telegramMeWebManager.setAuthorized") == "function",
                  "the other half of finalizing a login on this build")
            needed = ["managers.apiManager", "managers.appUsersManager",
                      "managers.appMessagesManager", "managers.appPeersManager"]
            missing = [k for k in needed if surf.get(k) in (None, "undefined", "<throws>")]
            check("managers needed by the other bridges resolve", not missing,
                  f"missing: {missing}" if missing else
                  "apiManager / appUsersManager / appMessagesManager / appPeersManager "
                  "all resolve (send, contacts, stats, presence can use the same fix)")
            check("page is on the login screen", bool(surf.get("login UI drawn")),
                  "so a bridge login is the right next test")

        try:
            await page.screenshot(path=str(outdir / "page.png"))
            (outdir / "page.html").write_text(await page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if args.keep_open:
            await asyncio.sleep(float(args.keep_open))

        # ---- GO / NO-GO -----------------------------------------------------
        banner("GO / NO-GO")
        width = max(len(n) for n, _, _ in CHECKS) if CHECKS else 10
        for name, passed, detail in CHECKS:
            mark = "PASS" if passed is True else ("FAIL" if passed is False else "WARN")
            print(f"  {mark}  {name.ljust(width)}  {detail}", flush=True)
        fails = [n for n, p, _ in CHECKS if p is False]
        critical = {"resolver finds an api object", "a real call returns a real value",
                    "new readiness check returns true"}
        blocking = [n for n in fails if n in critical]
        REPORT["failed_checks"] = fails

        print("", flush=True)
        if not blocking:
            REPORT["verdict"].append("GO")
            print("  GO. The resolver works on the live page and a real call came "
                  "back with a real\n  value, so the fix is safe to apply:\n", flush=True)
            print("    1. eitaa/login_flow.py  - readiness check and the bridge "
                  "injection stop\n       looking for window.apiManager only; they use the "
                  "resolver instead.\n"
                  "    2. the seven bridge .js files - one shared resolver, the old "
                  "global still\n       preferred when present, so an older Eitaa build "
                  "keeps working.\n"
                  "    3. login_bridge.js finalize - dispatchUserAuth (+ "
                  "telegramMeWebManager.\n       setAuthorized) instead of setUserAuth.\n"
                  "    4. then a real login with your phone; once the account is in, "
                  "test a send.", flush=True)
        else:
            REPORT["verdict"].append("NO-GO: " + ", ".join(blocking))
            print(f"  NO-GO. These critical checks failed: {blocking}\n", flush=True)
            print("  Do not change the project code yet. Send this output - the "
                  "failing check\n  tells us which assumption was wrong, and the "
                  "'result frames matching OUR task\n  ids' section above says whether "
                  "the worker answered at all.", flush=True)
    except Exception:  # noqa: BLE001
        REPORT["traceback"] = traceback.format_exc()
        print(traceback.format_exc(), flush=True)
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            tmp.unlink()
        except Exception:  # noqa: BLE001
            pass

    (outdir / "report.json").write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n  full report: {outdir / 'report.json'}", flush=True)
    return 0 if REPORT.get("verdict") == ["GO"] else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="GO/NO-GO preflight for the apiManager fix. "
                    "Never requests a login code.")
    p.add_argument("--account", required=True)
    p.add_argument("--wait", type=float, default=10)
    p.add_argument("--call-timeout", type=float, default=8)
    p.add_argument("--acc", type=int, default=1,
                   help="accountNumber used in manager tasks (default 1)")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-light", action="store_true")
    p.add_argument("--url", default=None)
    p.add_argument("--keep-open", type=float, default=0)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
