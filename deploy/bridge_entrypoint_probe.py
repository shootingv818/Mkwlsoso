#!/usr/bin/env python3
"""Find the NEW in-page entry point for Eitaa's API, with proof.

Background (from deploy/login_diag.py on 2026-10-11)
----------------------------------------------------
Eitaa migrated to the multi-account build of Telegram Web K. On that build the
main world no longer has `apiManager`, `appUsersManager` or `appStateManager`:
the managers live inside a Web Worker, and the page talks to them over a
MessagePort. What IS exposed is

    apiManagerProxy                 -> a message port (processResultTask, ...)
    mtprotoMessagePort              -> the same shape
    createProxiedManagersForAccount -> a function
    telegramMeWebManager            -> setAuthorized
    rootScope                       -> dispatchEvent

All seven bridges in this repo hardcode `window.apiManager.invokeApi`, so every
bridge feature (login, send, file send, contacts, contacts list, stats,
presence) is dead on this build. Before rewriting them, this script establishes
WITH EVIDENCE what the replacement call is.

What it does, in order
----------------------
  1. Installs a recorder BEFORE any page script runs, wrapping MessagePort /
     Worker / SharedWorker postMessage + onmessage. Eitaa's own boot traffic
     (help.getConfig, help.getNearestDc, the country list for the login form)
     is then captured verbatim: the exact envelope the worker expects.
  2. Loads the login page and waits for that traffic.
  3. Walks the FULL prototype chain of apiManagerProxy / mtprotoMessagePort /
     rootScope, so a method one level deeper than login_diag looked is found.
  4. Calls createProxiedManagersForAccount() and reports the real shape of what
     comes back, plus its source text.
  5. PROVES the entry point by making a harmless, unauthenticated RPC through
     every candidate and reporting which one answers.
  6. Prints the exact JS line that worked, ready to wire into the bridges.

Safety
------
* `auth.sendCode` is NEVER called. No login code is requested, nothing is sent,
  no number can be rate-limited by this script.
* The only RPCs attempted are `help.getNearestDc` and `help.getCountriesList`
  - both unauthenticated, read-only, and already made by the page itself at
  boot.
* No existing project file is touched. The recorder is written to a temp file
  and passed to BrowserSession's existing init_script_path parameter. Output
  goes to the gitignored artifacts/ dir.

Run on the SERVER:

    cd ~/Mkwlsoso && .venv/bin/python deploy/bridge_entrypoint_probe.py --account 989939094655

Options:
    --wait 25        seconds to record boot traffic (default 25)
    --acc-index 1    account slot to pass to createProxiedManagersForAccount
    --headed         real window instead of headless
    --no-light       load images/fonts too
    --keep-open N    keep the browser open N seconds at the end
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"sections": {}, "proof": [], "verdict": []}
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


async def ev(page, js: str, arg=None):
    """evaluate -> (value, error_text); never raises."""
    try:
        return (await (page.evaluate(js, arg) if arg is not None else page.evaluate(js)), None)
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# 1. the recorder, injected before any page script runs
# ==========================================================================

RECORDER_JS = r"""
// MKWL entry-point recorder. Runs BEFORE Eitaa's own scripts (add_init_script),
// so it sees the very first message the page sends to its manager worker.
// Read-only: it records and forwards, it never blocks or alters a message.
(() => {
  if (window.__MKWL_PORTLOG) return;
  const CAP = 600;
  const log = [];
  window.__MKWL_PORTLOG = log;
  window.__MKWL_WORKERS = [];

  // Describe a message without dragging huge payloads (or binary) into python.
  function brief(v, depth) {
    depth = depth || 0;
    try {
      if (v === null || v === undefined) return v === null ? "null" : "undefined";
      const t = typeof v;
      if (t === "string") return v.length > 160 ? v.slice(0, 160) + "..." : v;
      if (t === "number" || t === "boolean") return v;
      if (t === "function") return "<fn " + (v.name || "anon") + ">";
      if (ArrayBuffer.isView(v) || v instanceof ArrayBuffer)
        return "<bytes " + (v.byteLength || v.length || 0) + ">";
      if (Array.isArray(v)) {
        if (depth >= 3) return "<array " + v.length + ">";
        return v.slice(0, 8).map(x => brief(x, depth + 1));
      }
      if (t === "object") {
        if (depth >= 3) return "<object>";
        const o = {};
        const keys = Object.keys(v).slice(0, 18);
        for (const k of keys) {
          try { o[k] = brief(v[k], depth + 1); } catch (e) { o[k] = "<throws>"; }
        }
        return o;
      }
      return String(t);
    } catch (e) { return "<brief-failed>"; }
  }

  function push(dir, kind, data) {
    if (log.length >= CAP) return;
    try { log.push({ ms: Date.now(), dir: dir, kind: kind, data: brief(data, 0) }); }
    catch (e) {}
  }
  window.__MKWL_push = push;

  function wrapIncoming(fn, kind) {
    return function (ev) {
      try { push("in", kind, ev && ev.data); } catch (e) {}
      return fn.apply(this, arguments);
    };
  }

  // ---- MessagePort (this is how tweb's main thread reaches its managers) ---
  try {
    const P = MessagePort.prototype;
    const origPost = P.postMessage;
    P.postMessage = function () {
      try { push("out", "port", arguments[0]); } catch (e) {}
      return origPost.apply(this, arguments);
    };
    const origAdd = P.addEventListener;
    P.addEventListener = function (type, fn) {
      if (type === "message" && typeof fn === "function") {
        const args = Array.prototype.slice.call(arguments);
        args[1] = wrapIncoming(fn, "port");
        return origAdd.apply(this, args);
      }
      return origAdd.apply(this, arguments);
    };
    const d = Object.getOwnPropertyDescriptor(P, "onmessage");
    if (d && d.set) {
      Object.defineProperty(P, "onmessage", {
        configurable: true, enumerable: d.enumerable,
        get: d.get,
        set: function (fn) {
          return d.set.call(this, typeof fn === "function" ? wrapIncoming(fn, "port") : fn);
        }
      });
    }
  } catch (e) { push("err", "patch", "MessagePort: " + e); }

  // ---- Worker / SharedWorker ----------------------------------------------
  function patchWorkerClass(name) {
    try {
      const Orig = window[name];
      if (!Orig) return;
      const Patched = function (url, opts) {
        const w = new Orig(url, opts);
        try {
          window.__MKWL_WORKERS.push(String(url));
          push("new", name.toLowerCase(), String(url));
        } catch (e) {}
        try {
          const target = name === "SharedWorker" ? w.port : w;
          const op = target.postMessage.bind(target);
          target.postMessage = function () {
            try { push("out", name.toLowerCase(), arguments[0]); } catch (e) {}
            return op.apply(null, arguments);
          };
          target.addEventListener("message", function (e2) {
            try { push("in", name.toLowerCase(), e2 && e2.data); } catch (e3) {}
          });
          if (name === "SharedWorker" && target.start) target.start();
        } catch (e) {}
        return w;
      };
      Patched.prototype = Orig.prototype;
      window[name] = Patched;
    } catch (e) { push("err", "patch", name + ": " + e); }
  }
  patchWorkerClass("Worker");
  patchWorkerClass("SharedWorker");
})();
"""


# ==========================================================================
# JS used after the page is up
# ==========================================================================

# Walk the WHOLE prototype chain - login_diag only looked one level down, which
# is exactly how a method can be missed.
DEEP_JS = r"""
(name) => {
  const out = { name: name, present: false };
  let v;
  try { v = name.split(".").reduce((a, k) => (a == null ? a : a[k]), window); }
  catch (e) { out.error = String(e); return out; }
  if (v === null || v === undefined) return out;
  out.present = true;
  out.type = typeof v;
  try { out.ctor = v.constructor && v.constructor.name; } catch (e) {}
  const seen = {}, chain = [];
  let o = v, level = 0;
  while (o && level < 8) {
    const fns = [], props = [];
    let names = [];
    try { names = Object.getOwnPropertyNames(o); } catch (e) { names = []; }
    for (const n of names) {
      if (n === "constructor") continue;
      let t = "?";
      try { t = typeof v[n]; } catch (e) { t = "<throws>"; }
      if (t === "function") { if (!seen[n]) { seen[n] = 1; fns.push(n); } }
      else if (!seen[n]) { seen[n] = 1; props.push(n); }
    }
    chain.push({
      level: level,
      proto: (o.constructor && o.constructor.name) || "?",
      functions: fns.slice(0, 60),
      props: props.slice(0, 40)
    });
    try { o = Object.getPrototypeOf(o); } catch (e) { break; }
    level++;
  }
  out.chain = chain;
  const all = Object.keys(seen);
  out.invoke_like = all.filter(n => /invoke|request|call|send|api/i.test(n));
  out.auth_like = all.filter(n => /auth|user|login|sign/i.test(n));
  out.total_members = all.length;
  return out;
}
"""

FACTORY_JS = r"""
(idx) => {
  const out = {};
  const f = window.createProxiedManagersForAccount;
  out.type = typeof f;
  if (typeof f !== "function") return out;
  out.arity = f.length;
  try { out.source = String(f).slice(0, 700); } catch (e) {}
  const tries = {};
  for (const a of [idx, 1, 0, undefined]) {
    const key = String(a);
    if (tries[key]) continue;
    const r = {};
    try {
      const m = (a === undefined) ? f() : f(a);
      r.ok = true;
      r.type = typeof m;
      try { r.keys = Object.keys(m || {}).slice(0, 60); } catch (e) { r.keys = "<proxy hides keys>"; }
      for (const n of ["apiManager", "appUsersManager", "appStateManager",
                       "appPeersManager", "appMessagesManager", "appProfileManager"]) {
        let t = "absent";
        try { t = typeof (m && m[n]); } catch (e) { t = "<throws: " + e + ">"; }
        r[n] = t;
      }
      try { r.invokeApi = typeof (m && m.apiManager && m.apiManager.invokeApi); }
      catch (e) { r.invokeApi = "<throws>"; }
      // A Proxy answers 'function' for ANY name, so record a nonsense name too:
      // if it also says 'function', typeof proves nothing and only a real call does.
      try { r.nonsense_control = typeof (m && m.thisManagerDoesNotExist && m.thisManagerDoesNotExist.nope); }
      catch (e) { r.nonsense_control = "<throws>"; }
    } catch (e) {
      r.ok = false;
      r.error = String(e);
    }
    tries[key] = r;
  }
  out.tries = tries;
  return out;
}
"""

# The decisive test: a real, harmless, unauthenticated call through a candidate.
RPC_JS = r"""
async (a) => {
  const t0 = Date.now();
  let target = null, how = a.how;
  try {
    if (how === "factory") {
      const f = window.createProxiedManagersForAccount;
      if (typeof f !== "function") return { ok: false, code: "no factory" };
      const m = (a.idx === null) ? f() : f(a.idx);
      target = m && m.apiManager;
    } else if (how === "rootScope.managers") {
      target = window.rootScope && window.rootScope.managers && window.rootScope.managers.apiManager;
    } else if (how === "apiManagerProxy.getManagers") {
      const p = window.apiManagerProxy;
      if (!p || typeof p.getManagers !== "function") return { ok: false, code: "no getManagers" };
      const m = await p.getManagers();
      target = m && m.apiManager;
    } else {
      target = window[how];
    }
  } catch (e) { return { ok: false, code: "resolve: " + String(e) }; }
  if (!target) return { ok: false, code: "target is " + String(target) };

  const method = a.method || "invokeApi";
  let fn = null;
  try { fn = target[method]; } catch (e) { return { ok: false, code: "member: " + String(e) }; }
  if (typeof fn !== "function") return { ok: false, code: "no " + method + " on target" };
  try {
    const res = await target[method](a.rpc, {});
    let shape = null;
    try {
      shape = (res && typeof res === "object")
        ? { _: res._, keys: Object.keys(res).slice(0, 12) }
        : res;
    } catch (e) {}
    return { ok: true, ms: Date.now() - t0, shape: shape,
             json: JSON.stringify(res || null).slice(0, 400) };
  } catch (e) {
    let s = "ERR";
    try { s = String((e && (e.type || e.error_message || e.code || e.message)) || e); }
    catch (x) {}
    return { ok: false, ms: Date.now() - t0, code: s };
  }
}
"""

PORTLOG_JS = r"""
() => {
  const log = window.__MKWL_PORTLOG || [];
  const out = { count: log.length, workers: window.__MKWL_WORKERS || [] };
  // Group by the shape of the envelope so the protocol is visible at a glance.
  const shapes = {};
  const methods = {};
  for (const e of log) {
    const d = e.data;
    let keys = "(primitive)";
    if (d && typeof d === "object" && !Array.isArray(d)) keys = Object.keys(d).sort().join(",");
    else if (Array.isArray(d)) keys = "[array]";
    const k = e.dir + " " + e.kind + " {" + keys + "}";
    shapes[k] = (shapes[k] || 0) + 1;
    // Hunt for an MTProto method name anywhere shallow in the envelope.
    try {
      const s = JSON.stringify(d) || "";
      const m = s.match(/"(help|auth|users|contacts|messages|account|updates)\.[a-zA-Z]+"/g);
      if (m) for (const x of m) methods[x.replace(/"/g, "")] = (methods[x.replace(/"/g, "")] || 0) + 1;
    } catch (e2) {}
  }
  out.shapes = shapes;
  out.methods = methods;
  out.first = log.slice(0, 14);
  // The most useful frames: the ones that name an API method.
  out.api_frames = log.filter(e => {
    try { return /(help|auth|users|contacts|messages|account)\.[a-zA-Z]/.test(JSON.stringify(e.data) || ""); }
    catch (x) { return false; }
  }).slice(0, 10);
  return out;
}
"""


# ==========================================================================
# main flow
# ==========================================================================

async def run(args) -> int:
    REPORT["args"] = vars(args)
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"entrypoint_probe_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    banner("SECTION 0  -  install the recorder before any page script runs")
    tmp = Path(tempfile.gettempdir()) / f"mkwl_recorder_{os.getpid()}.js"
    tmp.write_text(RECORDER_JS, encoding="utf-8")
    ok(f"recorder written to {tmp} ({len(RECORDER_JS)} chars)")
    info("it wraps MessagePort/Worker/SharedWorker postMessage + onmessage,")
    info("records, and forwards untouched. It cannot block a message.")

    from capture.browser import BrowserSession
    url = args.url or config.EITAA_WEB_URL
    light = False if args.no_light else None

    t = time.time()
    try:
        session = await BrowserSession(args.account, headed=args.headed,
                                       init_script_path=tmp,
                                       light_assets=light).start()
    except Exception as exc:  # noqa: BLE001
        bad(f"browser failed to start - {type(exc).__name__}: {exc}")
        REPORT["browser_error"] = traceback.format_exc()
        (outdir / "report.json").write_text(json.dumps(REPORT, ensure_ascii=False,
                                                       indent=2, default=str), encoding="utf-8")
        return 2
    ok(f"browser up in {time.time() - t:.1f}s")
    page = session.page

    try:
        banner(f"SECTION 1  -  load {url} and record {args.wait}s of boot traffic")
        t = time.time()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            ok(f"HTTP {resp.status if resp else '?'} in {time.time() - t:.1f}s")
        except Exception as exc:  # noqa: BLE001
            bad(f"goto failed - {exc}")
            REPORT["goto_error"] = str(exc)
            return 2
        # The page fetches help.getConfig / the country list for the login form
        # on its own. Waiting is all we need - no clicking, no code requested.
        waited = 0.0
        while waited < float(args.wait):
            await asyncio.sleep(2.5)
            waited += 2.5
            n, _ = await ev(page, "() => (window.__MKWL_PORTLOG || []).length")
            info(f"recorded {n} message(s) after {waited:.0f}s")
            if isinstance(n, int) and n >= 40:
                break

        banner("SECTION 2  -  what the page itself says to its manager worker")
        pl, err = await ev(page, PORTLOG_JS)
        REPORT["sections"]["portlog"] = pl or {"error": err}
        if err:
            bad(f"could not read the recorder log - {err}")
        elif not pl or not pl.get("count"):
            bad("NOTHING was recorded. Either the managers are not reached over a "
                "MessagePort/Worker on this build, or the page talks to them before "
                "the init script ran (unlikely).")
        else:
            ok(f"{pl['count']} frames captured")
            if pl.get("workers"):
                info(f"worker scripts created: {pl['workers']}")
            dump("envelope shapes (dir kind {top-level keys}):", pl.get("shapes"))
            if pl.get("methods"):
                ok("MTProto method names seen on the wire - this is the proof that "
                   "API calls flow through these frames:")
                dump("methods:", pl.get("methods"))
            else:
                info("no plain method names in the envelopes (they may be numeric "
                     "task codes or already serialized)")
            dump("first frames:", pl.get("first"))
            if pl.get("api_frames"):
                dump("frames naming an API method (the template to copy):",
                     pl.get("api_frames"))

        banner("SECTION 3  -  full prototype chain of every exposed candidate")
        deep = {}
        for name in ("apiManagerProxy", "mtprotoMessagePort", "rootScope",
                     "telegramMeWebManager", "appStorage", "commonStateStorage",
                     "rootScope.managers", "apiManagerProxy.managers"):
            d, e = await ev(page, DEEP_JS, name)
            deep[name] = d or {"error": e}
            if not d or not d.get("present"):
                info(f"{name}: absent")
                continue
            inv = d.get("invoke_like") or []
            au = d.get("auth_like") or []
            line = (f"{name}: {d.get('ctor')} with {d.get('total_members')} members")
            (ok if inv else info)(line)
            if inv:
                ok(f"    invoke/api-like members: {inv}")
            if au:
                info(f"    auth/user-like members : {au}")
            for lv in (d.get("chain") or [])[:3]:
                info(f"    L{lv['level']} {lv['proto']}: {lv['functions'][:14]}")
        REPORT["sections"]["deep"] = deep

        banner("SECTION 4  -  createProxiedManagersForAccount: what does it return")
        fac, err = await ev(page, FACTORY_JS, args.acc_index)
        REPORT["sections"]["factory"] = fac or {"error": err}
        if err or not fac:
            bad(f"could not probe the factory - {err}")
        elif fac.get("type") != "function":
            bad(f"createProxiedManagersForAccount is {fac.get('type')} - not callable")
        else:
            ok(f"it is a function (arity {fac.get('arity')})")
            if fac.get("source"):
                info("source (truncated):")
                for ln in str(fac["source"]).splitlines()[:12]:
                    print("        " + ln, flush=True)
            dump("called with different account slots:", fac.get("tries"))
            for slot, r in (fac.get("tries") or {}).items():
                if not r.get("ok"):
                    bad(f"  slot {slot}: raised - {r.get('error')}")
                    continue
                ctrl = r.get("nonsense_control")
                note = ("  (NOTE: a nonsense name also reports 'function', so this is "
                        "a Proxy and typeof proves nothing - only SECTION 5 does)"
                        if ctrl == "function" else "")
                ok(f"  slot {slot}: apiManager={r.get('apiManager')} "
                   f"invokeApi={r.get('invokeApi')}{note}")

        banner("SECTION 5  -  PROOF: harmless RPC through each candidate")
        info("calling help.getNearestDc (unauthenticated, read-only, no code sent)")
        attempts = [
            {"how": "factory", "idx": args.acc_index, "method": "invokeApi", "rpc": "help.getNearestDc"},
            {"how": "factory", "idx": 1, "method": "invokeApi", "rpc": "help.getNearestDc"},
            {"how": "factory", "idx": 0, "method": "invokeApi", "rpc": "help.getNearestDc"},
            {"how": "factory", "idx": None, "method": "invokeApi", "rpc": "help.getNearestDc"},
            {"how": "factory", "idx": args.acc_index, "method": "invokeApiSingle", "rpc": "help.getNearestDc"},
            {"how": "factory", "idx": args.acc_index, "method": "invokeApiCacheable", "rpc": "help.getNearestDc"},
            {"how": "rootScope.managers", "idx": None, "method": "invokeApi", "rpc": "help.getNearestDc"},
            {"how": "apiManagerProxy.getManagers", "idx": None, "method": "invokeApi", "rpc": "help.getNearestDc"},
            {"how": "apiManagerProxy", "idx": None, "method": "invokeApi", "rpc": "help.getNearestDc"},
        ]
        results = []
        winner = None
        for a in attempts:
            label = (f"{a['how']}(idx={a['idx']}).{a['method']}" if a["how"] == "factory"
                     else f"{a['how']}.{a['method']}")
            r, e = await ev(page, RPC_JS, a)
            rec = {"label": label, "attempt": a, "result": r or {"evaluate_error": e}}
            results.append(rec)
            if e:
                bad(f"{label}: evaluate error - {e}")
            elif r and r.get("ok"):
                ok(f"{label} -> {r['ms']}ms   reply {r.get('json')}")
                if winner is None:
                    winner = rec
            else:
                bad(f"{label}: {(r or {}).get('code')}")
        REPORT["sections"]["rpc"] = results

        if winner:
            w = winner["attempt"]
            idx = w["idx"]
            call = (f"window.createProxiedManagersForAccount({'' if idx is None else idx})"
                    f".apiManager.{w['method']}(method, params)"
                    if w["how"] == "factory"
                    else f"window.{w['how']}.{w['method']}(method, params)")
            REPORT["proof"].append(call)
            # Second confirmation on a different method, so one lucky call is not
            # mistaken for a working entry point.
            a2 = dict(w)
            a2["rpc"] = "help.getCountriesList"
            r2, _ = await ev(page, RPC_JS, a2)
            REPORT["sections"]["rpc_confirm"] = r2
            if r2 and r2.get("ok"):
                ok(f"confirmed on a second method (help.getCountriesList) in {r2['ms']}ms")
            else:
                info(f"second method did not answer ({(r2 or {}).get('code')}) - "
                     f"getCountriesList may need different params; the first proof "
                     f"still stands")

            banner("SECTION 6  -  does the login path exist through the winner")
            # auth.sendCode is NOT called. We only check that the managers the
            # login needs are reachable, and record whether typeof can be trusted.
            probe_js = r"""
            async (a) => {
              const f = window.createProxiedManagersForAccount;
              const m = a.how === "factory" ? ((a.idx === null) ? f() : f(a.idx))
                                            : window[a.how];
              const out = {};
              const names = ["apiManager", "appUsersManager", "appStateManager",
                             "appPeersManager", "appMessagesManager"];
              for (const n of names) {
                try { out[n] = typeof (m && m[n]); } catch (e) { out[n] = "<throws>"; }
              }
              const am = m && m.apiManager;
              for (const n of ["invokeApi", "invokeApiSingle", "setUserAuth",
                               "logOut", "getNetworker"]) {
                try { out["apiManager." + n] = typeof (am && am[n]); }
                catch (e) { out["apiManager." + n] = "<throws>"; }
              }
              out["telegramMeWebManager.setAuthorized"] =
                typeof (window.telegramMeWebManager && window.telegramMeWebManager.setAuthorized);
              out["rootScope.dispatchEvent"] =
                typeof (window.rootScope && window.rootScope.dispatchEvent);
              return out;
            }
            """
            p6, e6 = await ev(page, probe_js, w)
            REPORT["sections"]["login_surface"] = p6 or {"error": e6}
            if p6:
                dump("managers/methods reachable through the winner:", p6)
                info("reminder: these are Proxy members, so 'function' means "
                     "'callable', not 'implemented'. SECTION 5 is the only real "
                     "proof, and it passed for invokeApi.")

        # artifacts
        try:
            await page.screenshot(path=str(outdir / "page.png"))
            (outdir / "page.html").write_text(await page.content(), encoding="utf-8")
            ok(f"saved page.png + page.html to {outdir}")
        except Exception as exc:  # noqa: BLE001
            info(f"artifacts not saved: {exc}")

        if args.keep_open:
            info(f"keeping the browser open {args.keep_open}s")
            await asyncio.sleep(float(args.keep_open))

        # ---- verdict --------------------------------------------------------
        banner("VERDICT")
        if winner:
            call = REPORT["proof"][0]
            REPORT["verdict"].append(
                f"FOUND the replacement entry point, proven by a live RPC: {call}")
            print(f"\n  The new entry point WORKS. Use this instead of "
                  f"window.apiManager.invokeApi:\n", flush=True)
            print(f"      {call}\n", flush=True)
            print("  All seven bridges need the same one-line resolver:\n"
                  "      eitaa/login_bridge.js, bridge_send.js, bridge_file_send.js,\n"
                  "      contacts_bridge.js, contacts_list.js, stats_bridge.js, presence.js\n"
                  "  plus eitaa/login_flow.py:96-104, whose readiness check also looks\n"
                  "  for window.apiManager and must accept the new shape.", flush=True)
        else:
            REPORT["verdict"].append(
                "No candidate answered a harmless RPC. The managers are not "
                "reachable from the main world on this build.")
            print("\n  No candidate answered. That means the API cannot be driven "
                  "from the page's\n  main world on this build at all. In that case "
                  "SECTION 2's recorded frames are\n  the way forward: speak the "
                  "worker's own message protocol, or stop using the\n  browser for "
                  "this and finish the browser-free `direct/` login instead.", flush=True)
        for i, line in enumerate(REPORT["verdict"], 1):
            print(f"\n  {i}) {line}", flush=True)
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
    return 0 if REPORT["proof"] else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Find and PROVE the new in-page API entry point. "
                    "Never requests a login code.")
    p.add_argument("--account", required=True, help="profile/account id, e.g. 989939094655")
    p.add_argument("--wait", type=float, default=25, help="seconds of boot traffic to record")
    p.add_argument("--acc-index", type=int, default=1,
                   help="account slot passed to createProxiedManagersForAccount (default 1)")
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
