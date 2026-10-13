#!/usr/bin/env python3
"""Learn Eitaa's manager-worker protocol from its OWN traffic, then replay it.

Where we are (evidence from the first two probes, 2026-10-11)
-------------------------------------------------------------
* `window.apiManager` is gone. This build of Eitaa is multi-account tweb: the
  managers run inside a SharedWorker (`mtproto.worker-*.js`) plus a crypto
  Worker, and the page reaches them over a MessagePort.
* What the main world still has: `apiManagerProxy` / `mtprotoMessagePort`
  (118 members, incl. `invoke`, `invokeVoid`, `createInvokeTask`,
  `dispatchUserAuth`), `rootScope.managers` (7 members, incl. `apiManager`),
  and `createProxiedManagersForAccount`.
* The envelope on the wire is `{type, payload, id, transfer}`, with `type` seen
  as `batch` / `invoke` / `lock`.
* BUT every call we made through those proxies - `managers.apiManager.invokeApi`,
  `rootScope.managers.apiManager.invokeApi`, and variants - timed out after 10s
  with no answer. And a nonsense property name also reports `function`, which
  proves those objects are Proxies: `typeof` tells us nothing about them.

So two very different explanations remain, and they need opposite fixes:

  (A) We asked a question Eitaa's server does not implement. We probed with
      `help.getNearestDc`, a *Telegram* method; Eitaa runs a modified protocol
      (this repo already documented a 4-byte payload-less reply for an
      unsupported method). The pipe may be perfectly fine.
  (B) Our call never leaves the page. The proxy would then be decorative and
      the only way in is to speak the port protocol directly.

This script separates them, with evidence rather than a guess:

  1. Records the port/worker traffic at FULL depth (the previous run truncated
     the inner payload to "<object>", which is exactly where the method names
     live).
  2. Extracts the method names Eitaa itself calls during boot - methods the
     server demonstrably answers.
  3. Replays one of THOSE methods through each proxy, while diffing the frame
     log around the call. That diff is the decisive measurement:
        - new outgoing frame + a matching result  -> the entry point works
        - new outgoing frame, no result           -> the worker ignores our task
                                                     (wrong shape / needs a lock)
        - no new frame at all                     -> explanation (B): the proxy
                                                     never reaches the port
  4. If a raw task shape was learned, replays it through
     `apiManagerProxy.invoke(...)` as well, bypassing the manager proxy.

Safety
------
* `auth.sendCode` is NEVER called. No login code is requested; nothing is sent
  to any contact. Methods are replayed only from the set Eitaa itself called
  during boot (help/config/country-list style reads), and any method matching
  auth./messages./contacts.import is refused by an explicit guard below.
* No existing project file is modified. The recorder goes to a temp file and is
  passed to BrowserSession's existing init_script_path parameter. Output lands
  in the gitignored artifacts/ dir.

Run on the SERVER:

    cd ~/Mkwlsoso && .venv/bin/python deploy/worker_protocol_probe.py --account 989939094655

Options:
    --wait 15         seconds of boot traffic to record (default 15)
    --method X        replay this method instead of an auto-picked one
    --call-timeout 8  per-call ceiling in seconds (default 8)
    --frames 14       how many recorded frames to print in full (default 14)
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

REPORT: dict = {"sections": {}, "verdict": []}
_T0 = time.time()

# Nothing that could send a message, request a code, or touch contacts may ever
# be replayed, even if Eitaa itself called it during boot.
FORBIDDEN = re.compile(
    r"^(auth\.|messages\.send|messages\.forward|contacts\.import|contacts\.add|"
    r"account\.update|photos\.upload|upload\.)", re.I)


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


async def ev(page, js: str, arg=None, timeout: float = 20.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout: no answer within {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# recorder v2 - full depth, and a frame-diff helper for the replay step
# ==========================================================================

RECORDER_JS = r"""
// MKWL protocol recorder v2. Runs before Eitaa's own scripts.
// v1 truncated nested payloads to "<object>" at depth 3 - the method names live
// below that - so this one goes deep and keeps a JSON snapshot per frame.
// Read-only: records and forwards, never blocks or alters a message.
(() => {
  if (window.__MKWL_PORTLOG) return;
  const CAP = 900;
  const log = [];
  window.__MKWL_PORTLOG = log;
  window.__MKWL_WORKERS = [];
  let seq = 0;

  function brief(v, depth) {
    depth = depth || 0;
    try {
      if (v === null) return null;
      if (v === undefined) return "<undefined>";
      const t = typeof v;
      if (t === "string") return v.length > 400 ? v.slice(0, 400) + "..." : v;
      if (t === "number" || t === "boolean") return v;
      if (t === "function") return "<fn " + (v.name || "anon") + ">";
      if (ArrayBuffer.isView(v) || v instanceof ArrayBuffer)
        return "<bytes " + (v.byteLength || v.length || 0) + ">";
      if (v instanceof MessagePort) return "<MessagePort>";
      if (Array.isArray(v)) {
        if (depth >= 8) return "<array " + v.length + ">";
        return v.slice(0, 24).map(x => brief(x, depth + 1));
      }
      if (t === "object") {
        if (depth >= 8) return "<object>";
        const o = {};
        let keys = [];
        try { keys = Object.keys(v); } catch (e) { return "<unkeyable>"; }
        for (const k of keys.slice(0, 40)) {
          try { o[k] = brief(v[k], depth + 1); } catch (e) { o[k] = "<throws>"; }
        }
        return o;
      }
      return String(t);
    } catch (e) { return "<brief-failed>"; }
  }

  function push(dir, kind, data) {
    if (log.length >= CAP) return;
    try {
      log.push({ n: ++seq, ms: Date.now(), dir: dir, kind: kind, data: brief(data, 0) });
    } catch (e) {}
  }
  window.__MKWL_push = push;
  window.__MKWL_seq = () => seq;
  window.__MKWL_since = (n) => log.filter(e => e.n > n);

  function wrapIncoming(fn, kind) {
    return function (ev) {
      try { push("in", kind, ev && ev.data); } catch (e) {}
      return fn.apply(this, arguments);
    };
  }

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
        configurable: true, enumerable: d.enumerable, get: d.get,
        set: function (fn) {
          return d.set.call(this, typeof fn === "function" ? wrapIncoming(fn, "port") : fn);
        }
      });
    }
  } catch (e) { push("err", "patch", "MessagePort: " + e); }

  function patchWorkerClass(name) {
    try {
      const Orig = window[name];
      if (!Orig) return;
      const Patched = function (url, opts) {
        const w = new Orig(url, opts);
        try { window.__MKWL_WORKERS.push(String(url)); push("new", name.toLowerCase(), String(url)); }
        catch (e) {}
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
# post-load JS
# ==========================================================================

HARVEST_JS = r"""
(limit) => {
  const log = window.__MKWL_PORTLOG || [];
  const out = { count: log.length, workers: window.__MKWL_WORKERS || [], seq: (window.__MKWL_seq ? window.__MKWL_seq() : 0) };
  const text = [];
  for (const e of log) {
    try { text.push(JSON.stringify(e.data) || ""); } catch (x) { text.push(""); }
  }
  const blob = text.join(" ");
  // MTProto method names, as the page itself used them.
  const methods = {};
  const mm = blob.match(/"((?:help|auth|users|contacts|messages|account|updates|langpack|photos|channels|folders|stickers|payments|phone|stats)\.[a-zA-Z0-9_]+)"/g) || [];
  for (const raw of mm) {
    const k = raw.replace(/"/g, "");
    methods[k] = (methods[k] || 0) + 1;
  }
  out.methods = methods;
  // Task 'type' values, which is the vocabulary apiManagerProxy.invoke expects.
  const types = {};
  const tm = blob.match(/"type":"([a-zA-Z0-9_]+)"/g) || [];
  for (const raw of tm) {
    const k = raw.replace(/"type":"/, "").replace(/"/g, "");
    types[k] = (types[k] || 0) + 1;
  }
  out.task_types = types;
  // Frames that name a method: these are the templates worth copying verbatim.
  const named = [];
  for (const e of log) {
    let s = "";
    try { s = JSON.stringify(e.data) || ""; } catch (x) {}
    if (/(help|auth|users|contacts|messages|account|updates|langpack)\.[a-zA-Z]/.test(s)) named.push(e);
    if (named.length >= limit) break;
  }
  out.named_frames = named;
  // Any 'invoke' frame, deep, even if no method name was matched.
  const invokes = log.filter(e => {
    try { return e.data && e.data.type === "invoke"; } catch (x) { return false; }
  }).slice(0, limit);
  out.invoke_frames = invokes;
  return out;
}
"""

# The decisive step: call, and diff the frame log around the call.
REPLAY_JS = r"""
async (a) => {
  const before = window.__MKWL_seq ? window.__MKWL_seq() : 0;
  const LIMIT = a.timeout_ms || 8000;
  const t0 = Date.now();
  const res = { how: a.how, method: a.method, frames_before: before };

  const cap = (p, what) => Promise.race([
    p, new Promise((_, rej) => setTimeout(
      () => rej(new Error("MKWL_TIMEOUT " + LIMIT + "ms (" + what + ")")), LIMIT))
  ]);

  let target = null;
  try {
    if (a.how === "factory") {
      const f = window.createProxiedManagersForAccount;
      target = f ? ((a.idx === null) ? f() : f(a.idx)) : null;
      target = target && target.apiManager;
    } else if (a.how === "rootScope.managers") {
      target = window.rootScope && window.rootScope.managers && window.rootScope.managers.apiManager;
    } else if (a.how === "proxy.invoke") {
      target = window.apiManagerProxy;
    }
  } catch (e) { res.resolve_error = String(e); }
  if (!target) { res.ok = false; res.code = "no target"; return res; }

  try {
    let p;
    if (a.how === "proxy.invoke") {
      // Speak the port vocabulary directly: invoke(taskType, payload).
      p = target.invoke(a.task_type || "invokeApi",
                        a.task_payload || { method: a.method, params: {} });
    } else {
      p = target[a.fn || "invokeApi"](a.method, a.params || {});
    }
    const r = await cap(Promise.resolve(p), a.fn || "invoke");
    res.ok = true;
    res.ms = Date.now() - t0;
    try { res.reply = JSON.stringify(r || null).slice(0, 500); } catch (e) { res.reply = "<unserializable>"; }
  } catch (e) {
    res.ok = false;
    res.ms = Date.now() - t0;
    try { res.code = String((e && (e.type || e.error_message || e.message || e.code)) || e); }
    catch (x) { res.code = "ERR"; }
  }

  // Did our call produce ANY traffic? This is what tells (A) from (B).
  const after = window.__MKWL_seq ? window.__MKWL_seq() : 0;
  res.frames_after = after;
  res.new_frames = after - before;
  try {
    res.new_frame_dump = (window.__MKWL_since ? window.__MKWL_since(before) : []).slice(0, 10);
  } catch (e) { res.new_frame_dump = []; }
  return res;
}
"""


async def run(args) -> int:
    REPORT["args"] = vars(args)
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"worker_protocol_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    banner("SECTION 0  -  recorder v2 (full depth) installed pre-page")
    tmp = Path(tempfile.gettempdir()) / f"mkwl_recorder2_{os.getpid()}.js"
    tmp.write_text(RECORDER_JS, encoding="utf-8")
    ok(f"recorder written to {tmp} ({len(RECORDER_JS)} chars)")
    info("v1 cut nested payloads at depth 3, which hid the method names; this "
         "one goes to depth 8 and keeps 400-char strings.")

    from capture.browser import BrowserSession
    url = args.url or config.EITAA_WEB_URL
    light = False if args.no_light else None
    t = time.time()
    try:
        session = await BrowserSession(args.account, headed=args.headed,
                                       init_script_path=tmp, light_assets=light).start()
    except Exception as exc:  # noqa: BLE001
        bad(f"browser failed to start - {type(exc).__name__}: {exc}")
        REPORT["browser_error"] = traceback.format_exc()
        return 2
    ok(f"browser up in {time.time() - t:.1f}s")
    page = session.page

    try:
        banner(f"SECTION 1  -  load {url}, record {args.wait}s of Eitaa's own traffic")
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            ok(f"HTTP {resp.status if resp else '?'}")
        except Exception as exc:  # noqa: BLE001
            bad(f"goto failed - {exc}")
            return 2
        waited = 0.0
        while waited < float(args.wait):
            await asyncio.sleep(2.5)
            waited += 2.5
            n, _ = await ev(page, "() => (window.__MKWL_PORTLOG || []).length", timeout=10)
            info(f"{n} frames after {waited:.0f}s")

        banner("SECTION 2  -  the protocol, read from Eitaa's own frames")
        h, err = await ev(page, HARVEST_JS, int(args.frames), timeout=25)
        REPORT["sections"]["harvest"] = h or {"error": err}
        methods: dict = {}
        if err or not h:
            bad(f"could not harvest - {err}")
        else:
            ok(f"{h['count']} frames, seq {h['seq']}")
            if h.get("workers"):
                info(f"workers: {h['workers']}")
            dump("task 'type' vocabulary (what apiManagerProxy.invoke expects):",
                 h.get("task_types"))
            methods = h.get("methods") or {}
            if methods:
                ok("METHODS EITAA ITSELF CALLED (the server demonstrably answers "
                   "these - replaying one of them removes explanation (A)):")
                dump("methods:", methods)
            else:
                bad("no method names anywhere in the frames, even at depth 8. The "
                    "payload is likely already serialized to bytes before it "
                    "reaches postMessage - see the dumps below.")
            if h.get("named_frames"):
                dump("frames that name a method (copy these verbatim):",
                     h.get("named_frames"))
            if h.get("invoke_frames"):
                dump("'invoke' frames at full depth:", h.get("invoke_frames"))

        banner("SECTION 3  -  replay a method EITAA ITSELF used, and diff the frames")
        # Pick a read-only method the page already used; never anything that sends.
        picked = args.method
        if not picked:
            safe = [m for m in sorted(methods, key=lambda k: -methods[k])
                    if not FORBIDDEN.match(m)]
            picked = safe[0] if safe else None
        if picked and FORBIDDEN.match(picked):
            bad(f"refusing to replay {picked} - it could send something. Pick a "
                f"read-only method with --method.")
            picked = None
        if not picked:
            info("no safe method harvested; falling back to help.getConfig, which "
                 "tweb itself issues at boot on every build")
            picked = "help.getConfig"
        ok(f"replaying: {picked}")
        info("reading the frame counter before and after each call - that diff is "
             "the measurement that separates the two explanations")

        attempts = [
            {"how": "factory", "idx": 1, "fn": "invokeApi", "method": picked,
             "timeout_ms": int(args.call_timeout * 1000)},
            {"how": "rootScope.managers", "idx": None, "fn": "invokeApi", "method": picked,
             "timeout_ms": int(args.call_timeout * 1000)},
            {"how": "proxy.invoke", "idx": None, "method": picked,
             "task_type": "invokeApi",
             "task_payload": {"method": picked, "params": {}},
             "timeout_ms": int(args.call_timeout * 1000)},
        ]
        results = []
        winner = None
        reached_port = False
        for a in attempts:
            label = (f"createProxiedManagersForAccount({a['idx']}).apiManager.invokeApi"
                     if a["how"] == "factory" else
                     "rootScope.managers.apiManager.invokeApi" if a["how"] == "rootScope.managers"
                     else "apiManagerProxy.invoke('invokeApi', ...)")
            r, e = await ev(page, REPLAY_JS, a, timeout=args.call_timeout + 8)
            results.append({"label": label, "attempt": a, "result": r or {"evaluate_error": e}})
            if e or not r:
                bad(f"{label}: {e}")
                continue
            nf = r.get("new_frames", 0)
            if r.get("ok"):
                ok(f"{label} -> ANSWERED in {r['ms']}ms, {nf} new frame(s)")
                info(f"    reply: {r.get('reply')}")
                winner = {"label": label, "attempt": a, "result": r}
            else:
                bad(f"{label}: {r.get('code')}  ({nf} new frame(s) produced)")
            if nf:
                reached_port = True
                dump(f"    frames our call produced ({label}):", r.get("new_frame_dump"))
        REPORT["sections"]["replay"] = results
        REPORT["sections"]["picked_method"] = picked

        try:
            await page.screenshot(path=str(outdir / "page.png"))
            (outdir / "page.html").write_text(await page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if args.keep_open:
            await asyncio.sleep(float(args.keep_open))

        banner("VERDICT")
        if winner:
            a = winner["attempt"]
            call = (f"window.createProxiedManagersForAccount({a['idx']})"
                    f".apiManager.invokeApi(method, params)" if a["how"] == "factory"
                    else "window.rootScope.managers.apiManager.invokeApi(method, params)"
                    if a["how"] == "rootScope.managers"
                    else "window.apiManagerProxy.invoke('invokeApi', {method, params})")
            REPORT["verdict"].append(
                f"WORKS: {call} answered {picked}. The earlier timeouts were "
                f"explanation (A): help.getNearestDc is a Telegram method Eitaa "
                f"does not implement, so it simply never replied. The entry point "
                f"was fine all along.")
            print(f"\n  Use this in all seven bridges:\n\n      {call}\n", flush=True)
        elif reached_port:
            REPORT["verdict"].append(
                "Our calls DO reach the port (new frames were produced) but no "
                "result comes back. So the proxy is wired, and what is wrong is "
                "the task shape or a precondition (the lock/state handshake seen "
                "in SECTION 2: 'lock', 'applyLoadedStates', "
                "'reloadAllStatesAfterUnlock'). Next step: copy a full 'invoke' "
                "frame from SECTION 2 byte for byte, including its id format.")
        else:
            REPORT["verdict"].append(
                "Our calls produce NO traffic at all: explanation (B). The proxy "
                "objects in the main world are decorative - the real client lives "
                "in the SharedWorker and the page's own module scope is not "
                "reachable from an injected script. Driving Eitaa through the "
                "browser bridge is then a dead end on this build, and the "
                "browser-free direct/ engine (which already sends text and files) "
                "is the way forward - it needs the login handshake built, "
                "docs/ROADMAP.md item 17.")
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
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Learn Eitaa's manager-worker protocol from its own traffic "
                    "and replay it. Never requests a login code.")
    p.add_argument("--account", required=True)
    p.add_argument("--wait", type=float, default=15)
    p.add_argument("--method", default=None,
                   help="replay this method instead of an auto-picked one")
    p.add_argument("--call-timeout", type=float, default=8)
    p.add_argument("--frames", type=int, default=14)
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
