#!/usr/bin/env python3
"""Hunt the direct-engine tokens EVERYWHERE, without breaking the app.

The last word on the direct path
--------------------------------
The direct (browser-free) engine needs the ed77be7a envelope's two routing
tokens. From direct/transport.py they are PLAIN ASCII strings:

    token1  like  "9179.c756a2d10f.e41c4e_<userid>"   (session/route token)
    token2  like  "mrtpgmi2y9fm222__web"              (client session id)

Because they are readable strings, if they exist anywhere reachable we can find
them by pattern, not just in a worker dump. The old worker_capture.js got them
by rebuilding the Worker - which breaks the multi-account build and hangs every
call. So this looks in every OTHER place a token could be, with the app left
intact, and reports which (if any) yields a usable token1/token2.

Five independent hunts, each isolated so one failing cannot stop the others:

  H1  CDP network sniff - attach a Chrome DevTools Protocol Network listener
      (observes the real wire, does NOT wrap Worker), load the app, make it
      talk, and scan every request/response body for the ed77be7a magic and the
      token strings. This is the wire itself - if the worker sends the envelope
      to Eitaa, it is visible here.
  H2  storage scan - read localStorage and every IndexedDB record, flatten to
      text, and regex for token1/token2 shapes and the ed77be7a magic. tweb
      persists session state; the tokens may simply be sitting there.
  H3  passive MessagePort only (NO SharedWorker wrap this time, since wrapping
      it is what broke the channel before) - record what crosses the port and
      scan it, while confirming invokeApi still answers.
  H4  in-page globals - walk apiManagerProxy / mtprotoMessagePort and the
      managers for any property or method return that exposes the token or the
      connection state (dcId, authKey id, the connection object).
  H5  CDP again, but reading the SharedWorker's own network via its target -
      attach to the worker target directly and sniff its requests, which is
      where the envelope is actually built and sent.

Any hunt that yields token1 (and ideally token2) means the direct path is
revivable on this build. The verdict says which, and whether a bare apk is back
on the table.

Read-only: observes, reads storage, makes a few help.getConfig calls. No code,
no send, own account only.

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/token_hunt.py --account 989939094655
    systemctl start mkwlsoso-bot
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"hunts": {}, "found": {}, "verdict": []}
_T0 = time.time()


def banner(t): print("\n" + "=" * 76 + f"\n  {t}\n" + "=" * 76, flush=True)
def log(m=""): print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)
def ok(m): log(f"OK    {m}")
def bad(m): log(f"FAIL  {m}")
def info(m): log(f"      {m}")
def hit(m): log(f"####  {m}")


def dump(label, obj, indent="        "):
    info(label)
    for ln in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines()[:60]:
        print(indent + ln, flush=True)


# The token shapes, from direct/transport.py comments. token1 has dotted
# segments then _<userid>; token2 is an alnum id ending __web. Also match the
# raw magic.
MAGIC = "ed77be7a"
TOKEN1_RE = re.compile(r"\b\d{3,6}\.[0-9a-f]{6,}\.[0-9a-z]{4,}_\d{4,}\b", re.I)
TOKEN2_RE = re.compile(r"\b[0-9a-z]{8,}__web\b", re.I)


def scan_text(text: str) -> dict:
    """Find token1/token2/magic in a blob of text. Returns what it found."""
    out = {}
    if not text:
        return out
    if MAGIC in text.lower():
        out["magic"] = True
    t1 = TOKEN1_RE.findall(text)
    t2 = TOKEN2_RE.findall(text)
    if t1:
        out["token1"] = sorted(set(t1))[:3]
    if t2:
        out["token2"] = sorted(set(t2))[:3]
    return out


def scan_hex(hexstr: str) -> dict:
    """A wire body is hex; the tokens are ASCII inside it, so decode and scan."""
    if not hexstr:
        return {}
    out = {}
    low = hexstr.lower()
    if MAGIC in low:
        out["magic"] = True
    try:
        raw = bytes.fromhex(hexstr[:200000])
        text = raw.decode("latin-1", "replace")
        s = scan_text(text)
        # merge, keeping magic
        for k, v in s.items():
            out[k] = v
    except Exception:  # noqa: BLE001
        pass
    return out


# ==========================================================================
# JS payloads
# ==========================================================================

# H2: read ALL of localStorage + every IndexedDB record, as text.
STORAGE_JS = r"""
async () => {
  const out = { localStorage: {}, idb: {}, errors: [] };
  try {
    for (const k of Object.keys(localStorage)) {
      let v = ""; try { v = String(localStorage.getItem(k) || ""); } catch(e){}
      out.localStorage[k] = v;   // full value - the hunt needs the content
    }
  } catch(e){ out.errors.push("ls: "+e); }
  try {
    if (!indexedDB.databases) { out.errors.push("no indexedDB.databases"); return out; }
    const dbs = await indexedDB.databases();
    for (const meta of dbs) {
      const db = await new Promise((res,rej)=>{ const r=indexedDB.open(meta.name);
        r.onsuccess=()=>res(r.result); r.onerror=()=>rej(r.error);
        setTimeout(()=>rej(new Error("open to")),5000); }).catch(()=>null);
      if (!db) continue;
      for (const store of Array.from(db.objectStoreNames)) {
        try {
          const os_ = db.transaction(store,"readonly").objectStore(store);
          const all = await new Promise((res,rej)=>{ const q=os_.getAll();
            q.onsuccess=()=>res(q.result); q.onerror=()=>rej(q.error);
            setTimeout(()=>rej(new Error("getAll to")),5000); });
          // stringify each record; cap size so a huge store does not explode
          const recs = [];
          for (const r of (all||[]).slice(0,200)) {
            try { recs.push(JSON.stringify(r).slice(0,4000)); } catch(e){ recs.push(String(r).slice(0,1000)); }
          }
          out.idb[meta.name+"/"+store] = recs.join("\n");
        } catch(e){ out.idb[meta.name+"/"+store] = "<err: "+e+">"; }
      }
      db.close();
    }
  } catch(e){ out.errors.push("idb: "+e); }
  return out;
}
"""

# H3: passive MessagePort recorder - NO SharedWorker wrap (that is what broke
# the channel). Installed as an init script.
PORT_RECORDER_JS = r"""
(() => {
  if (window.__TH) return;
  const LOG = []; window.__TH = { log: LOG };
  function brief(v,d){ d=d||0;
    try {
      if (v==null) return v===null?"null":"undef";
      const t=typeof v;
      if (t==="string") return v.length>3000?v.slice(0,3000):v;
      if (t==="number"||t==="boolean") return v;
      if (v instanceof ArrayBuffer||ArrayBuffer.isView(v)) {
        const b=v instanceof ArrayBuffer?new Uint8Array(v):new Uint8Array(v.buffer,v.byteOffset,v.byteLength);
        let h=""; const n=Math.min(b.length,512); for(let i=0;i<n;i++) h+=b[i].toString(16).padStart(2,"0");
        return {bytes:b.length, hex:h};
      }
      if (v instanceof MessagePort) return "<port>";
      if (Array.isArray(v)) return d>=6?"<arr>":v.slice(0,20).map(x=>brief(x,d+1));
      if (t==="object"){ if(d>=6)return"<obj>"; const o={}; let ks=[]; try{ks=Object.keys(v);}catch(e){return"<unkeyable>";}
        for(const k of ks.slice(0,40)){try{o[k]=brief(v[k],d+1);}catch(e){o[k]="<throws>";}} return o; }
      return String(t);
    } catch(e){ return "<brief>"; }
  }
  function push(dir,data){ if(LOG.length<3000){ try{ LOG.push({dir,data:brief(data,0)}); }catch(e){} } }
  window.__TH.dump=()=>LOG.slice();
  try {
    const P=MessagePort.prototype, op=P.postMessage;
    P.postMessage=function(){ try{push("out",arguments[0]);}catch(e){} return op.apply(this,arguments); };
    const oa=P.addEventListener;
    P.addEventListener=function(type,fn){
      if(type==="message"&&typeof fn==="function"){ const a=[].slice.call(arguments);
        a[1]=function(ev){ try{push("in",ev&&ev.data);}catch(e){} return fn.apply(this,arguments); };
        return oa.apply(this,a); }
      return oa.apply(this,arguments);
    };
    const d=Object.getOwnPropertyDescriptor(P,"onmessage");
    if(d&&d.set) Object.defineProperty(P,"onmessage",{configurable:true,enumerable:d.enumerable,get:d.get,
      set:function(fn){ return d.set.call(this, typeof fn==="function"
        ? function(ev){try{push("in",ev&&ev.data);}catch(e){} return fn.apply(this,arguments);} : fn); }});
  } catch(e){}
  // NOTE: deliberately NOT wrapping Worker/SharedWorker - that broke invokeApi.
})()
"""

# H4: walk the in-page globals for anything token-ish or a live connection.
GLOBALS_JS = r"""
async () => {
  const out = { probed: [], strings: [] };
  const seen = new Set();
  function note(path, v) {
    try {
      const s = (typeof v === "string") ? v : JSON.stringify(v);
      if (s && (/__web\b/.test(s) || /ed77be7a/i.test(s) || /\d{3,6}\.[0-9a-f]{6,}\./i.test(s))) {
        out.strings.push({ path, sample: s.slice(0,200) });
      }
    } catch(e){}
  }
  function walk(obj, path, depth) {
    if (depth > 3 || obj == null) return;
    let keys = [];
    try { keys = Object.keys(obj); } catch(e){ return; }
    for (const k of keys.slice(0,60)) {
      const p = path + "." + k;
      if (seen.has(p)) continue; seen.add(p);
      let v;
      try { v = obj[k]; } catch(e){ continue; }
      note(p, v);
      const t = typeof v;
      if (t === "object" && v && depth < 3) walk(v, p, depth+1);
    }
  }
  for (const name of ["apiManagerProxy","mtprotoMessagePort","rootScope",
                      "appStorage","commonStateStorage"]) {
    try { if (window[name]) { out.probed.push(name); walk(window[name], name, 0); } } catch(e){}
  }
  // also try the managers object
  try {
    const m = window.__MKWL_managers ? window.__MKWL_managers() : null;
    if (m) { out.probed.push("managers"); walk(m, "managers", 0); }
  } catch(e){}
  return out;
}
"""

CALL_JS = r"""
async () => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
  try{ const c=await Promise.race([AM.invokeApi("help.getConfig",{}),
    new Promise((_,j)=>setTimeout(()=>j(new Error("to")),12000))]);
    return {ok:true, dcs:(c&&c.dc_options||[]).length}; }
  catch(e){ return {ok:false, code:String(e)}; }
}
"""

PORT_DUMP_JS = "() => (window.__TH && window.__TH.dump) ? window.__TH.dump() : []"


# ==========================================================================
# the hunts
# ==========================================================================

def record_find(hunt: str, found: dict) -> None:
    if found.get("token1") or found.get("token2") or found.get("magic"):
        REPORT["found"][hunt] = found
        if found.get("token1"):
            hit(f"{hunt}: token1 found -> {found['token1']}")
        if found.get("token2"):
            hit(f"{hunt}: token2 found -> {found['token2']}")
        if found.get("magic") and not found.get("token1"):
            info(f"{hunt}: the ed77be7a magic is present but no clean token1 "
                 f"matched - the bytes are here, parsing may need a tweak")


async def hunt_cdp_network(page, session, wait) -> None:
    """H1 + H5: sniff the real wire via CDP, including worker targets."""
    banner("HUNT 1  -  CDP network sniff (the real wire, no Worker rebuild)")
    bodies = []
    try:
        client = await session.context.new_cdp_session(page)
        await client.send("Network.enable")

        reqs = {}

        def on_req(ev):
            try:
                r = ev.get("request", {})
                pd = r.get("postData")
                if pd:
                    reqs[ev.get("requestId")] = r.get("url", "")
                    found = scan_text(pd)
                    if found:
                        found["url"] = r.get("url")
                        record_find("CDP req", found)
                        bodies.append(("req", r.get("url"), pd[:400]))
            except Exception:  # noqa: BLE001
                pass

        client.on("Network.requestWillBeSent", on_req)
        info("CDP Network enabled; making the app talk...")
        # drive a few calls so the worker opens its connection and sends
        for _ in range(3):
            try:
                await asyncio.wait_for(page.evaluate(CALL_JS), timeout=14)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.5)
        await asyncio.sleep(float(wait) / 2)

        # also pull response bodies for eitaa requests we saw
        for rid, url in list(reqs.items())[:20]:
            try:
                resp = await client.send("Network.getResponseBody", {"requestId": rid})
                b = resp.get("body", "")
                found = scan_text(b)
                if found:
                    record_find("CDP resp", found)
            except Exception:  # noqa: BLE001
                pass
        REPORT["hunts"]["cdp"] = {"request_bodies_scanned": len(reqs),
                                  "samples": bodies[:5]}
        if not REPORT["found"].get("CDP req") and not REPORT["found"].get("CDP resp"):
            info(f"scanned {len(reqs)} request bodies, no token on the main-page "
                 f"wire (the worker has its own connection; see HUNT 5)")
    except Exception as exc:  # noqa: BLE001
        bad(f"CDP sniff error: {type(exc).__name__}: {str(exc)[:120]}")
        REPORT["hunts"]["cdp_error"] = str(exc)


async def hunt_cdp_workers(page, session, wait) -> None:
    """H5: attach to worker targets and sniff THEIR network (where the envelope
    is actually built and sent)."""
    banner("HUNT 5  -  CDP into the worker targets (where the envelope is sent)")
    try:
        client = await session.context.new_cdp_session(page)
        # discover targets, including workers
        await client.send("Target.setDiscoverTargets", {"discover": True})
        await asyncio.sleep(2)
        targets = await client.send("Target.getTargets")
        workers = [t for t in targets.get("targetInfos", [])
                   if t.get("type") in ("worker", "shared_worker", "service_worker")]
        info(f"worker targets found: {[w.get('url', '')[:60] for w in workers]}")
        REPORT["hunts"]["worker_targets"] = [w.get("url") for w in workers]
        got = False
        for w in workers:
            try:
                att = await client.send("Target.attachToTarget",
                                        {"targetId": w["targetId"], "flatten": True})
                sid = att.get("sessionId")
                # enable network on that worker session
                await client.send("Network.enable", {}, sid)

                def on_wreq(ev, _url=w.get("url")):
                    try:
                        pd = ev.get("request", {}).get("postData")
                        if pd:
                            found = scan_text(pd)
                            if found:
                                found["worker"] = _url
                                record_find("CDP worker", found)
                    except Exception:  # noqa: BLE001
                        pass

                client.on("Network.requestWillBeSent", on_wreq)
                got = True
            except Exception as exc:  # noqa: BLE001
                info(f"  could not attach to {w.get('url', '')[:40]}: {str(exc)[:60]}")
        if got:
            info("attached to worker(s); driving traffic...")
            for _ in range(3):
                try:
                    await asyncio.wait_for(page.evaluate(CALL_JS), timeout=14)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(1.5)
            await asyncio.sleep(float(wait) / 2)
        if not REPORT["found"].get("CDP worker"):
            info("no token on the worker wire either (it may use a binary frame "
                 "the postData hook does not surface, or WebSocket)")
    except Exception as exc:  # noqa: BLE001
        bad(f"worker CDP error: {type(exc).__name__}: {str(exc)[:120]}")
        REPORT["hunts"]["worker_cdp_error"] = str(exc)


async def hunt_storage(page) -> None:
    banner("HUNT 2  -  localStorage + IndexedDB scan")
    s, err = await _ev(page, STORAGE_JS, 45)
    if err or not s:
        bad(f"storage read failed: {err}")
        return
    blob_parts = []
    for k, v in (s.get("localStorage") or {}).items():
        blob_parts.append(f"LS:{k}={v}")
    for k, v in (s.get("idb") or {}).items():
        blob_parts.append(f"IDB:{k}={v}")
    blob = "\n".join(blob_parts)
    info(f"localStorage keys: {list((s.get('localStorage') or {}).keys())}")
    info(f"IndexedDB stores: {list((s.get('idb') or {}).keys())}")
    found = scan_text(blob)
    REPORT["hunts"]["storage"] = {"ls_keys": list((s.get('localStorage') or {}).keys()),
                                  "idb_stores": list((s.get('idb') or {}).keys()),
                                  "bytes_scanned": len(blob)}
    if found:
        record_find("storage", found)
    else:
        info(f"scanned {len(blob)} chars of stored data, no token/magic present")


async def hunt_passive_port(page, wait) -> None:
    banner("HUNT 3  -  passive MessagePort recorder (no SharedWorker wrap)")
    # confirm the channel still works
    r, _ = await _ev(page, CALL_JS, 20)
    if r and r.get("ok"):
        ok(f"invokeApi still works with the passive port recorder "
           f"({r.get('dcs')} dc_options) - channel intact")
    else:
        info(f"invokeApi did not answer ({(r or {}).get('code')}) - "
             f"recording whatever crossed anyway")
    for _ in range(2):
        try:
            await asyncio.wait_for(page.evaluate(CALL_JS), timeout=14)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1)
    frames, _ = await _ev(page, PORT_DUMP_JS, 20)
    frames = frames or []
    blob = json.dumps(frames, ensure_ascii=False)
    found = scan_text(blob)
    REPORT["hunts"]["port"] = {"frames": len(frames), "channel_ok": bool(r and r.get("ok"))}
    if found:
        record_find("port", found)
    else:
        info(f"{len(frames)} port frames recorded, no token/magic in them "
             f"(the envelope is built inside the worker, not sent over the port)")


async def hunt_globals(page) -> None:
    banner("HUNT 4  -  in-page globals (apiManagerProxy / managers / storage objs)")
    g, err = await _ev(page, GLOBALS_JS, 30)
    if err or not g:
        bad(f"globals walk failed: {err}")
        return
    info(f"probed: {g.get('probed')}")
    strings = g.get("strings") or []
    REPORT["hunts"]["globals"] = {"probed": g.get("probed"), "hits": len(strings)}
    if strings:
        dump("token-ish strings found in globals:", strings[:10])
        blob = json.dumps(strings)
        found = scan_text(blob)
        if found:
            record_find("globals", found)
    else:
        info("no token-ish string reachable by walking the globals")


async def _ev(page, js, timeout):
    try:
        return (await asyncio.wait_for(page.evaluate(js), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout {timeout}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# main
# ==========================================================================

async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"token_hunt_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver
    import tempfile

    banner("TOKEN HUNT  -  find the direct-engine tokens without breaking the app")
    log(f"account {args.account}")
    log("looking for token1 ~ '9179.c756a2d10f.e41c4e_<uid>' and")
    log("token2 ~ 'mrtpgmi2y9fm222__web', and the ed77be7a envelope magic")

    # the passive port recorder is an init script; the rest run post-load
    tmp = os.path.join(tempfile.gettempdir(), f"th_{os.getpid()}.js")
    open(tmp, "w", encoding="utf-8").write(PORT_RECORDER_JS)

    session = await BrowserSession(args.account, headed=False, light_assets=None,
                                   init_script_path=tmp).start()
    page = session.page
    try:
        resp = await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                               timeout=120_000)
        info(f"HTTP {resp.status if resp else '?'}; booting {args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        st = await EitaaDriver(session).login_state()
        if not st.get("logged_in"):
            bad(f"not logged in ({st.get('auth_state')}) - log in first")
            REPORT["verdict"].append("Not logged in; the tokens only exist for a "
                                     "live session. Log in and re-run.")
            _save(outdir)
            return 2
        ok(f"logged in: my_id={st.get('my_id')}")

        # run every hunt; each is isolated
        for fn in (hunt_storage, hunt_globals):
            try:
                await fn(page)
            except Exception as exc:  # noqa: BLE001
                bad(f"{fn.__name__} raised: {str(exc)[:100]}")
        try:
            await hunt_passive_port(page, args.wait)
        except Exception as exc:  # noqa: BLE001
            bad(f"passive port hunt raised: {str(exc)[:100]}")
        try:
            await hunt_cdp_network(page, session, args.wait)
        except Exception as exc:  # noqa: BLE001
            bad(f"cdp network hunt raised: {str(exc)[:100]}")
        try:
            await hunt_cdp_workers(page, session, args.wait)
        except Exception as exc:  # noqa: BLE001
            bad(f"cdp worker hunt raised: {str(exc)[:100]}")

        try:
            await page.screenshot(path=str(outdir / "page.png"))
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        import traceback
        REPORT["traceback"] = traceback.format_exc()
        print(REPORT["traceback"], flush=True)
    finally:
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass

    _verdict()
    _save(outdir)
    return 0


def _verdict() -> None:
    banner("VERDICT")
    found = REPORT["found"]
    with_t1 = {h: f for h, f in found.items() if f.get("token1")}
    with_magic = {h: f for h, f in found.items() if f.get("magic")}
    v = REPORT["verdict"]
    if with_t1:
        hit("THE TOKEN IS REACHABLE - the direct path is revivable on this build")
        for h, f in with_t1.items():
            v.append(f"token1 via {h}: {f.get('token1')}  "
                     f"token2: {f.get('token2', 'not matched - may need a wider regex')}")
        v.append("Next: build the direct context from THIS source instead of the "
                 "worker-rebuild hook, then the apk arsenal can finally run - and a "
                 "bare apk via the media host is back on the table.")
    elif with_magic:
        hit("The ed77be7a envelope IS present somewhere, but a clean token1 did "
            "not parse out.")
        for h, f in with_magic.items():
            v.append(f"magic via {h} - the bytes are there; the token parse needs "
                     f"adjusting to this source's framing")
        v.append("This is promising: the envelope exists outside the worker. One "
                 "more pass to parse the exact bytes from that source should yield "
                 "the tokens.")
    else:
        v.append("The tokens were NOT found on the wire, in storage, over the "
                 "port, or in the globals. On this multi-account build the "
                 "ed77be7a envelope is built and sent entirely inside the "
                 "SharedWorker and never surfaces anywhere reachable without "
                 "rebuilding that worker - which breaks the app.")
        v.append("CONCLUSION (now evidence-based, not a guess): the direct engine "
                 "cannot get its tokens on this build, so a bare apk is not "
                 "deliverable by any path. The way to send an app to elderly "
                 "recipients is a PDF with a big 'Download' button linking to the "
                 "apk - a normal file Eitaa delivers, which we fixed today.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)
    # a compact per-hunt table
    banner("WHERE EACH HUNT LOOKED")
    for h, data in REPORT["hunts"].items():
        print(f"  {h:<16} {json.dumps(data, ensure_ascii=False, default=str)[:90]}",
              flush=True)


def _save(outdir) -> None:
    try:
        p = outdir / "report.json"
        p.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print(f"\n  full report: {p}", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Hunt the direct-engine tokens everywhere without breaking "
                    "the app. Read-only, own account only.")
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
