#!/usr/bin/env python3
"""Capture the direct-engine context WITHOUT breaking the worker channel.

The problem, now proven
-----------------------
hook_vs_nohook showed worker_capture.js kills invokeApi on this build: it
rebuilds each Worker from a blob that importScripts()es the original, and the
current Eitaa app is the multi-account build whose managers live in a SharedWorker
reached over MessagePorts - rebuilding that worker breaks the message channel, so
every call hangs. The direct context needs the ed77be7a envelope, which only
appears in the worker's raw network bytes, so the old hook was the only way to
get it - and it no longer works.

The idea here
-------------
Observe, do not rebuild. On this build the page talks to its MTProto worker over
MessagePort.postMessage (we saw this when the apiManager move was first mapped).
Wrapping ONLY MessagePort.prototype.postMessage / onmessage - recording the bytes
and forwarding them untouched - should capture the same traffic WITHOUT
substituting the worker, so the channel keeps working.

But there is a subtlety: the raw ed77be7a envelope is built INSIDE the worker,
after the main thread hands over a structured task - so the main-thread port may
only carry the structured task, not the final bytes. This probe finds out what
the port actually carries, and whether a usable token can be lifted from it:

  1. clean page (no Worker rebuild), inject a passive MessagePort recorder
  2. make one real invokeApi (which must still work - that is the whole point)
  3. dump what the recorder saw and look for: the ed77be7a envelope, OR any
     field that looks like the routing tokens, OR a structured task we can learn
     the tokens from

If the tokens are reachable this way, the fix is clear: replace the
worker-rebuild hook with a passive MessagePort recorder, and the direct context
builds on this build without hanging anything.

Read-only. One invokeApi (help.getConfig). No code, no send. Own account.

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/capture_v2_probe.py --account 989939094655
    systemctl start mkwlsoso-bot
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def banner(t): print("\n" + "=" * 72 + f"\n  {t}\n" + "=" * 72, flush=True)
def log(m=""): print(f"  {m}", flush=True)


# Passive recorder: wrap MessagePort + Worker/SharedWorker postMessage and
# onmessage to RECORD and FORWARD. It never substitutes the worker, so unlike
# worker_capture.js it cannot break the channel. Installed as an init script.
RECORDER_JS = r"""
(() => {
  if (window.__CV2) return;
  const LOG = [];
  window.__CV2 = { log: LOG, workers: [] };
  const CAP = 2000;

  function brief(v, d) {
    d = d || 0;
    try {
      if (v == null) return v === null ? "null" : "undef";
      const t = typeof v;
      if (t === "string") return v.length > 200 ? v.slice(0,200)+"..." : v;
      if (t === "number" || t === "boolean") return v;
      if (v instanceof ArrayBuffer || ArrayBuffer.isView(v)) {
        const b = v instanceof ArrayBuffer ? new Uint8Array(v)
                 : new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
        let h = ""; const n = Math.min(b.length, 64);
        for (let i=0;i<n;i++) h += b[i].toString(16).padStart(2,"0");
        return { bytes: b.length, head: h };
      }
      if (v instanceof MessagePort) return "<MessagePort>";
      if (Array.isArray(v)) return d>=5 ? "<array>" : v.slice(0,12).map(x=>brief(x,d+1));
      if (t === "object") {
        if (d>=5) return "<object>";
        const o = {}; let ks=[];
        try { ks = Object.keys(v); } catch(e){ return "<unkeyable>"; }
        for (const k of ks.slice(0,30)) { try { o[k]=brief(v[k],d+1); } catch(e){ o[k]="<throws>"; } }
        return o;
      }
      return String(t);
    } catch(e){ return "<brief-failed>"; }
  }
  function push(dir, kind, data) {
    if (LOG.length >= CAP) return;
    try { LOG.push({ dir, kind, data: brief(data,0), t: Date.now() }); } catch(e){}
  }
  window.__CV2.dump = function(){ const r = LOG.slice(); return r; };

  // ---- MessagePort: the main<->worker channel on this build ----
  try {
    const P = MessagePort.prototype;
    const op = P.postMessage;
    P.postMessage = function(){ try { push("out","port",arguments[0]); } catch(e){} return op.apply(this, arguments); };
    const oa = P.addEventListener;
    P.addEventListener = function(type, fn){
      if (type==="message" && typeof fn==="function") {
        const a = Array.prototype.slice.call(arguments);
        a[1] = function(ev){ try { push("in","port",ev&&ev.data); } catch(e){} return fn.apply(this, arguments); };
        return oa.apply(this, a);
      }
      return oa.apply(this, arguments);
    };
    const d = Object.getOwnPropertyDescriptor(P,"onmessage");
    if (d && d.set) Object.defineProperty(P,"onmessage",{configurable:true,enumerable:d.enumerable,get:d.get,
      set:function(fn){ return d.set.call(this, typeof fn==="function"
        ? function(ev){ try{push("in","port",ev&&ev.data);}catch(e){} return fn.apply(this,arguments);} : fn); }});
  } catch(e){}

  // ---- Worker/SharedWorker: record only, DO NOT rebuild ----
  ["Worker","SharedWorker"].forEach(function(name){
    try {
      const Orig = window[name];
      if (!Orig) return;
      const W = function(url, opts){
        const w = new Orig(url, opts);
        try { window.__CV2.workers.push(String(url)); push("new",name.toLowerCase(),String(url)); } catch(e){}
        try {
          const t = name==="SharedWorker" ? w.port : w;
          const op2 = t.postMessage.bind(t);
          t.postMessage = function(){ try{push("out",name.toLowerCase(),arguments[0]);}catch(e){} return op2.apply(null, arguments); };
          t.addEventListener("message", function(ev){ try{push("in",name.toLowerCase(),ev&&ev.data);}catch(e){} });
          if (name==="SharedWorker" && t.start) t.start();
        } catch(e){}
        return w;
      };
      W.prototype = Orig.prototype;
      window[name] = W;
    } catch(e){}
  });
})()
"""

CALL_JS = r"""
async () => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
  const t=Date.now();
  try{
    const c=await Promise.race([AM.invokeApi("help.getConfig",{}),
      new Promise((_,j)=>setTimeout(()=>j(new Error("to")),15000))]);
    return {ok:true, ms:Date.now()-t, dcs:(c&&c.dc_options||[]).length};
  }catch(e){ return {ok:false, ms:Date.now()-t, code:String(e)}; }
}
"""

ANALYZE_JS = r"""
() => {
  const log = (window.__CV2 && window.__CV2.dump()) || [];
  const out = { total: log.length, workers: (window.__CV2||{}).workers||[], shapes:{}, envelope_hits:[], token_candidates:[] };
  for (const e of log) {
    const key = e.dir+" "+e.kind;
    out.shapes[key] = (out.shapes[key]||0)+1;
    let s = "";
    try { s = JSON.stringify(e.data) || ""; } catch(x){}
    // the ed77be7a envelope, if it ever crosses the port in raw bytes
    if (s.indexOf("ed77be7a") !== -1) out.envelope_hits.push(e);
    // anything that looks like a routing token field
    if (/token/i.test(s)) {
      if (out.token_candidates.length < 8) out.token_candidates.push(e);
    }
  }
  out.first = log.slice(0,10);
  // the invoke-shaped frames, which is where a structured task would carry auth
  out.invoke_frames = log.filter(e=>{ try{ return e.data && (e.data.type==="invoke"||e.data.type==="batch"); }catch(x){return false;} }).slice(0,6);
  return out;
}
"""


async def run(args):
    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from config import config
    import tempfile

    banner("capture v2 - record the worker channel WITHOUT rebuilding the worker")
    log(f"account {args.account}")
    tmp = os.path.join(tempfile.gettempdir(), f"cv2_{os.getpid()}.js")
    open(tmp, "w", encoding="utf-8").write(RECORDER_JS)

    session = await BrowserSession(args.account, headed=False, light_assets=None,
                                   init_script_path=tmp).start()
    page = session.page
    try:
        await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                        timeout=120_000)
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)

        banner("STEP 1  -  does invokeApi still work with the PASSIVE recorder?")
        try:
            r = await asyncio.wait_for(page.evaluate(CALL_JS), timeout=22)
        except asyncio.TimeoutError:
            r = {"ok": False, "code": "python timeout 22s"}
        if r.get("ok"):
            log(f"  help.getConfig -> OK in {r['ms']}ms ({r.get('dcs')} dc_options)")
            log("  the passive recorder does NOT break the channel (unlike the old hook)")
        else:
            log(f"  help.getConfig -> FAILED: {r.get('code')}")
            log("  even passive recording breaks it - then capture must come from")
            log("  somewhere other than the port; see the analysis below")

        # a couple more calls to give the recorder material
        for _ in range(2):
            try:
                await asyncio.wait_for(page.evaluate(CALL_JS), timeout=18)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1)

        banner("STEP 2  -  what crossed the channel, and is a token in it?")
        a, err = await asyncio.wait_for(page.evaluate(ANALYZE_JS), timeout=30), None
        rep = a or {"error": err}
        log(f"  frames recorded: {rep.get('total')}")
        log(f"  workers created: {rep.get('workers')}")
        log("  frame shapes:")
        for k, v in (rep.get("shapes") or {}).items():
            log(f"      {k}: {v}")
        env = rep.get("envelope_hits") or []
        tok = rep.get("token_candidates") or []
        log(f"  ed77be7a envelope seen on the channel: {len(env)}")
        log(f"  token-looking frames: {len(tok)}")
        if rep.get("invoke_frames"):
            banner("sample invoke/batch frames (where auth would ride)")
            print(json.dumps(rep["invoke_frames"], ensure_ascii=False, indent=2)[:3000], flush=True)
        if env:
            banner("ENVELOPE FRAMES - the direct tokens are in here")
            print(json.dumps(env[:3], ensure_ascii=False, indent=2)[:2000], flush=True)

        banner("VERDICT")
        if r.get("ok") and env:
            log("BEST CASE: the channel survives passive recording AND the ed77be7a")
            log("envelope crosses it. Replace worker_capture.js with this passive")
            log("recorder and the direct context builds on this build, no hang.")
        elif r.get("ok") and not env:
            log("The channel survives (good), but the raw ed77be7a envelope does NOT")
            log("cross the main-thread port - it is built inside the worker. So the")
            log("tokens are not liftable from the port directly. Next: read the")
            log("invoke/batch frames above - if they carry the auth fields in a")
            log("structured form, the direct context can be built from THOSE instead")
            log("of the raw envelope; if not, the direct engine cannot be fed from a")
            log("non-breaking capture, and the bridge is the only path on this build.")
        else:
            log("Even passive recording breaks invokeApi, which is surprising and")
            log("would mean the recorder itself must be lighter still. Send this")
            log("output and I will narrow what in the wrap is responsible.")

        try:
            (config.ARTIFACTS_DIR / f"cv2_{int(time.time())}.json").write_text(
                json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--account", required=True)
    p.add_argument("--wait", type=float, default=20)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
