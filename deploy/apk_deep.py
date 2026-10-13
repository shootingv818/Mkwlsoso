#!/usr/bin/env python3
"""Pinpoint Eitaa's apk filter exactly, and find a ONE-STEP way through it.

Where we are
------------
apk_breakthrough proved:
  * Eitaa opens an archive and inspects what is inside (a zip of probe.bin
    delivered, a zip of AndroidManifest.xml did not).
  * A single-layer zip of an apk is dropped; a DOUBLE zip delivers. So the
    inspector descends exactly ONE level and stops.

A double zip is a bad answer for a real user - they would have to extract twice.
This probe exists to do better, by answering two precise questions:

  Q1  WHAT exactly triggers the single-layer drop? apk_breakthrough's flat
      "scrub names + break dex magic" test went out as a .zip and delivered, but
      that was a whole-archive rewrite. Here each signal is removed ONE AT A TIME
      from a real apk-in-a-single-zip, so the exact trigger is named: the
      AndroidManifest.xml entry name, the classes.dex name, the dex magic bytes,
      the META-INF signature block, or the .apk extension of the inner entry.

  Q2  Is there a ONE-STEP delivery - a single file the recipient opens once (or
      not at all) and gets a working apk? Candidates, each a real artifact:
        * the apk with ONLY the minimum changed to pass, still a single .zip
          with one entry the user extracts
        * the apk sent as a .jar / .apks / .xapk / .obb (sibling formats that
          are also just zips, in case the filter is extension-gated after all)
        * a self-contained renamed apk: entries renamed so Android still installs
          it (manifest/dex names are fixed by the format, so this tests whether a
          RENAMED-BACK step is unavoidable)
        * the apk with bytes PREPENDED so the zip's central directory still
          parses but a naive one-level scan sees a different structure first
        * the apk gzip'd (.apk.gz) - one gunzip, no zip tool, many clients do it
          inline
        * the direct/ media-host path, this time with the worker capture wired
          so the context actually builds

Self-healing
------------
Every delivered case becomes a lead and is re-sent at a realistic size, and the
probe prints LEAD / BREAKTHROUGH live. A breakthrough that needs ZERO extraction
(apk installs directly) is marked DIRECT-WIN; one that needs a single extract is
ONE-STEP; the double-zip is only the fallback.

Safety
------
Everything goes to YOUR OWN Saved Messages. No contact is targeted. Payloads are
small until a lead is confirmed. No project file is modified. Tagged APKDEEP-*.

Run on the SERVER, bot stopped:

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/apk_deep.py --account 989104938987
    systemctl start mkwlsoso-bot

    # with your real apk, which is the honest test:
    .venv/bin/python deploy/apk_deep.py --account 989104938987 --real-apk /root/app.apk
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import io
import json
import os
import random
import string
import struct
import sys
import time
import traceback
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"cases": [], "leads": [], "breakthroughs": [], "verdict": []}
_T0 = time.time()
_SESSION = None


def banner(t): print("\n" + "=" * 78 + f"\n  {t}\n" + "=" * 78, flush=True)
def log(m=""): print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)
def ok(m): log(f"OK    {m}")
def bad(m): log(f"FAIL  {m}")
def info(m): log(f"      {m}")
def lead(m): log(f">>>>  LEAD: {m}")
def win(m): log(f"####  {m}")


def dump(label, obj, indent="        "):
    info(label)
    for line in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + line, flush=True)


async def ev(page, js, arg=None, timeout=180.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


ZIP_MIME = "application/zip"
OCTET = "application/octet-stream"


# ==========================================================================
# apk building blocks - a real apk, and controlled single-signal variants
# ==========================================================================

def base_apk(size_b: int, real_apk_path: str | None) -> bytes:
    if real_apk_path and os.path.isfile(real_apk_path):
        return open(real_apk_path, "rb").read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<manifest package="com.mkwl.probe" />\n')
        z.writestr("classes.dex", b"dex\n035\x00" + os.urandom(1024))
        z.writestr("resources.arsc", os.urandom(256))
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("META-INF/CERT.SF", "Signature-Version: 1.0\n")
        z.writestr("META-INF/CERT.RSA", os.urandom(128))
        z.writestr("assets/payload.bin", os.urandom(max(1024, size_b)))
    return buf.getvalue()


def repack(entries: list[tuple[str, bytes]], comp=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", comp) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def read_entries(apk: bytes) -> list[tuple[str, bytes]]:
    z = zipfile.ZipFile(io.BytesIO(apk))
    return [(n, z.read(n)) for n in z.namelist()]


def apk_with_one_signal_removed(apk: bytes, signal: str) -> bytes:
    """Return the apk as a zip with exactly ONE apk-signal removed.

    This is the scalpel: whichever single removal first delivers names the
    trigger, instead of a blanket rewrite that cannot say which part mattered.
    """
    ent = read_entries(apk)
    out = []
    for name, data in ent:
        n, d = name, data
        if signal == "manifest-name" and name == "AndroidManifest.xml":
            n = "AndroidManifest.dat"
        elif signal == "dex-name" and name == "classes.dex":
            n = "classes.bin"
        elif signal == "dex-magic" and name == "classes.dex":
            d = b"MKWL" + data[3:]  # keep length, break the "dex\n" magic
        elif signal == "meta-inf" and name.startswith("META-INF/"):
            continue  # drop the signature block entirely
        elif signal == "arsc-name" and name == "resources.arsc":
            n = "resources.bin"
        out.append((n, d))
    return repack(out)


def apk_single_zip(apk: bytes, inner_name="app.apk", comp=zipfile.ZIP_DEFLATED) -> bytes:
    return repack([(inner_name, apk)], comp=comp)


def gz_bytes(apk: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", filename="app.apk") as g:
        g.write(apk)
    return buf.getvalue()


def prepend_pad(apk: bytes, pad: int = 4096) -> bytes:
    """A valid zip stays valid with junk PREPENDED: the central directory at the
    end still points correctly, and zip tools seek from the end. A one-level
    scanner that reads from the FRONT sees padding first."""
    return os.urandom(pad) + apk


def build_cases(size_b: int, real_apk: str | None) -> list[dict]:
    apk = base_apk(size_b, real_apk)
    rows: list[dict] = []

    def add(label, name, mime, data, why, group, extract, follow=True):
        rows.append({"label": label, "name": name, "mime": mime, "bytes": data,
                     "why": why, "group": group, "extract": extract,
                     "follow": follow})

    # ---- controls ----------------------------------------------------------
    add("ctl-zip-plain", "ok.zip", ZIP_MIME, repack([("note.txt", b"hi" * 20)]),
        "a plain zip must deliver", "control", "n/a", follow=False)
    add("ctl-apk-single", "bundle.zip", ZIP_MIME, apk_single_zip(apk),
        "a single-zip apk - known to drop; the baseline the scalpel moves from",
        "control", "1x unzip", follow=False)

    # ---- Q1: remove ONE signal at a time, each in a single zip -------------
    for sig, note in [
        ("manifest-name", "AndroidManifest.xml renamed"),
        ("dex-name", "classes.dex renamed"),
        ("dex-magic", "classes.dex magic bytes broken"),
        ("meta-inf", "the META-INF signature block removed"),
        ("arsc-name", "resources.arsc renamed"),
    ]:
        variant = apk_with_one_signal_removed(apk, sig)
        add(f"q1-{sig}", "bundle.zip", ZIP_MIME, apk_single_zip(variant),
            f"single zip, only change: {note} - if THIS delivers, that is the "
            f"trigger", "which-signal", "1x unzip + rename back")

    # ---- Q2: one-step delivery candidates ----------------------------------
    # sibling archive extensions (all are really zips)
    for ext in ["jar", "apks", "xapk", "obb"]:
        add(f"ext-{ext}", f"app.{ext}", OCTET, apk,
            f"the raw apk under a .{ext} name - a sibling archive format, in case "
            f"the filter is extension-gated after the content peek", "ext",
            "rename to .apk")
    # the apk gzip'd - one gunzip, no zip tool
    add("gz", "app.apk.gz", "application/gzip", gz_bytes(apk),
        "gzip'd apk - a single gunzip yields the apk, many clients do it inline",
        "wrap", "1x gunzip")
    # bytes prepended so a front-reading scanner sees padding first
    add("prepend", "app.apk", OCTET, prepend_pad(apk),
        "random bytes prepended; the zip still parses from its end, a naive "
        "front scan does not see the apk structure", "mangle", "strip header",
        )
    add("prepend-zip", "bundle.zip", ZIP_MIME, apk_single_zip(prepend_pad(apk)),
        "the prepended apk inside one zip", "mangle", "1x unzip + strip")
    # STORED single zip, so the inner apk bytes are literally present uncompressed
    add("single-stored", "bundle.zip", ZIP_MIME,
        apk_single_zip(apk, comp=zipfile.ZIP_STORED),
        "single zip, STORED - confirms whether the scan decompresses or just "
        "reads names", "which-signal", "1x unzip")

    return rows


# ==========================================================================
# page sender - the proven mechanism (appMessagesManager via the worker proxy)
# ==========================================================================

SENDER_JS = r"""
(() => {
  if (window.__MKWLD) return;
  const D = {}; window.__MKWLD = D;
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  function MGR(n){ try{ if(window.__MKWL_mgr) return window.__MKWL_mgr(n); }catch(e){} return window[n]||null; }
  function er(e){ try{ return String((e&&(e.type||e.error_message||e.code||e.message))||e);}catch(x){return "ERR";} }
  function b2u(b64){ const s=atob(b64); const u=new Uint8Array(s.length); for(let i=0;i<s.length;i++) u[i]=s.charCodeAt(i); return u; }
  D.selfId = async function(){
    const c=[];
    try{ if(window.rootScope&&window.rootScope.getMyId) c.push(await window.rootScope.getMyId()); }catch(e){}
    try{ const M=MGR("appUsersManager"); if(M&&M.getSelf){const s=await M.getSelf(); if(s&&s.id!=null)c.push(s.id);} }catch(e){}
    for(const x of c){ if(x!=null&&+x!==0&&!isNaN(+x)) return +x; }
    return null;
  };
  D.send = async function(a){
    const t0=Date.now();
    const M=MGR("appMessagesManager");
    if(!M||typeof M.sendFile!=="function") return {ok:false, code:"no sendFile(proxy)"};
    const sid=await D.selfId();
    if(sid==null) return {ok:false, code:"no self id"};
    let f;
    try{ f=new File([b2u(a.b64)], a.name, {type:a.mime}); }catch(e){ return {ok:false, code:"build:"+er(e)}; }
    try{ await M.sendFile({peerId:sid, file:f, caption:a.marker}); return {ok:true, code:"sendFile", ms:Date.now()-t0}; }
    catch(e){ return {ok:false, code:er(e), ms:Date.now()-t0}; }
  };
  D.readBack = async function(limit){
    const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
    try{
      const h=await AM.invokeApi("messages.getHistory",{peer:{_:"inputPeerSelf"},offset_id:0,offset_date:0,add_offset:0,limit:limit||90,max_id:0,min_id:0,hash:0});
      const out=[];
      for(const m of (h&&h.messages)||[]){ if(!m) continue;
        const d=m.media&&m.media.document; let fn=null;
        try{ for(const at of (d&&d.attributes)||[]) if(at&&at._==="documentAttributeFilename") fn=at.file_name; }catch(e){}
        out.push({id:m.id, message:(m.message||"").slice(0,160), has_document:!!d,
                  mime:d?(d.mime_type||null):null, size:d?(d.size!=null?String(d.size):null):null, file_name:fn});
      }
      return {ok:true, messages:out};
    }catch(e){ return {ok:false, code:"getHistory:"+er(e)}; }
  };
})()
"""


def mk_marker(label):
    rnd = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"APKDEEP-{rnd}-{label}"


async def send_case(page, name, mime, data, marker, timeout):
    arg = {"b64": base64.b64encode(data).decode("ascii"), "name": name,
           "mime": mime, "marker": marker}
    js = ("async (a) => { const D = window.__MKWLD; try { return await D.send(a); } "
          "catch (e) { return { ok: false, code: 'threw:' + String(e) }; } }")
    res, err = await ev(page, js, arg, timeout=timeout)
    return res or {"ok": False, "code": err}


async def read_back(page, settle):
    await asyncio.sleep(settle)
    back, err = await ev(page, "async () => await window.__MKWLD.readBack(100)", timeout=120)
    return back or {"ok": False, "code": err}


def match(messages, records):
    for rec in records:
        found = next((m for m in messages if rec["marker"] in (m.get("message") or "")), None)
        rec["delivered"] = bool(found)
        rec["stored"] = found


async def escalate(page, rec, size_big, real_apk, settle):
    label = rec["label"]
    lead(f"{label} delivered small - re-sending at a realistic size")
    apk = base_apk(size_big, real_apk)
    # rebuild the SAME transformation at full size
    g = rec["group"]
    if label == "gz":
        big = gz_bytes(apk)
    elif label == "prepend":
        big = prepend_pad(apk)
    elif label == "prepend-zip":
        big = apk_single_zip(prepend_pad(apk))
    elif label == "single-stored":
        big = apk_single_zip(apk, comp=zipfile.ZIP_STORED)
    elif label.startswith("ext-"):
        big = apk  # raw apk, only the name differs
    elif label.startswith("q1-"):
        big = apk_single_zip(apk_with_one_signal_removed(apk, label[3:]))
    else:
        big = apk_single_zip(apk)
    marker = mk_marker(label + "-REAL")
    info(f"{label}: {len(big) / 1e6:.1f} MB")
    res = await send_case(page, rec["name"], rec["mime"], big, marker,
                          timeout=max(600, len(big) // 20000))
    follow = {"label": label + "-REAL", "name": rec["name"], "mime": rec["mime"],
              "marker": marker, "result": res, "group": "escalation",
              "extract": rec.get("extract"), "why": "the lead at realistic size"}
    REPORT["cases"].append(follow)
    if not res.get("ok"):
        bad(f"{label}-REAL: send failed at size - {res.get('code')}")
        return
    back = await read_back(page, settle)
    match((back.get("messages") or []), [follow])
    if follow.get("delivered"):
        s = follow.get("stored") or {}
        extract = rec.get("extract", "?")
        kind = ("DIRECT-WIN" if extract in ("n/a", "rename to .apk")
                else "ONE-STEP" if "1x" in str(extract) or extract in ("strip header",)
                else "WIN")
        win(f"{kind}: {label} works at {len(big) / 1e6:.1f} MB "
            f"(recipient: {extract}). server stored mime={s.get('mime')} "
            f"name={s.get('file_name')} size={s.get('size')}")
        REPORT["breakthroughs"].append({"label": label, "kind": kind,
                                        "recipient": extract, "name": rec["name"],
                                        "mime": rec["mime"], "stored": s})
    else:
        bad(f"{label}-REAL: delivered small, DROPPED at {len(big) / 1e6:.1f} MB")


# ==========================================================================
# the direct/ media-host route - wired with the worker capture this time
# ==========================================================================

async def try_direct(account, size_big, real_apk, settle, verify_page) -> None:
    banner("ROUTE D  -  the direct/ media-host path, worker capture wired")
    info("the previous run skipped this because the session had no worker")
    info("capture; here the browser is opened WITH eitaa/worker_capture.js so")
    info("the direct context can build, then we send a bare apk and read back")
    os.environ.setdefault("MKWL_ENABLE_DIRECT", "1")
    os.environ.setdefault("MKWL_APK_OCTET", "1")

    from capture.browser import BrowserSession
    from eitaa.driver import EitaaDriver
    from eitaa import worker_capture  # noqa: F401 - ensure the file exists
    from bot import direct_ctx
    import tempfile

    cap_path = os.path.join(_ROOT, "eitaa", "worker_capture.js")
    if not os.path.isfile(cap_path):
        bad("eitaa/worker_capture.js is missing - cannot build a direct context")
        return
    sess = None
    try:
        sess = await BrowserSession(account, init_script_path=cap_path).start()
        from config import config
        await sess.page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                             timeout=120_000)
        await asyncio.sleep(18)
        drv = EitaaDriver(sess)
        refresh = await direct_ctx.refresh_from_driver(drv, account)
        REPORT["direct_refresh"] = refresh
        if not refresh.get("ok") and not direct_ctx.has_context(account):
            bad(f"direct context still did not build: {refresh.get('code')}")
            return
        ok(f"direct context: {refresh.get('kept')} record(s), "
           f"user_id={refresh.get('user_id')}")
    except Exception as exc:  # noqa: BLE001
        bad(f"opening the capture session raised: {type(exc).__name__}: {exc}")
        REPORT["direct_open_traceback"] = traceback.format_exc()
        if sess:
            try:
                await sess.close()
            except Exception:  # noqa: BLE001
                pass
        return

    try:
        from direct.sender import DirectSender
        sender = await asyncio.to_thread(DirectSender, account)
        self_peer = getattr(sender, "self_peer", None)
        if not self_peer:
            bad("direct capture has no self peer")
            return
        apk = base_apk(size_big, real_apk)
        tmp = os.path.join(tempfile.gettempdir(), f"apkdeep_{int(time.time())}.apk")
        open(tmp, "wb").write(apk)
        marker = mk_marker("direct-raw-apk")
        info(f"direct upload of a bare {len(apk) / 1e6:.1f} MB apk (octet mode)")
        up = await asyncio.to_thread(sender.upload_file, tmp, marker)
        REPORT["direct_upload"] = up
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass
        if not up.get("ok"):
            bad(f"direct upload failed: {up.get('code')}")
            return
        info(f"uploaded {up.get('parts')} part(s) to {up.get('host')}")
        snd = await asyncio.to_thread(sender.send_uploaded_file, self_peer, marker)
        REPORT["direct_send"] = snd
        rec = {"label": "direct-raw-apk", "name": "app.apk", "mime": OCTET,
               "marker": marker, "result": snd, "group": "direct",
               "extract": "none - a bare apk", "why": "the media-host path"}
        REPORT["cases"].append(rec)
        back = await read_back(verify_page, settle)
        match((back.get("messages") or []), [rec])
        if rec.get("delivered"):
            s = rec.get("stored") or {}
            win(f"DIRECT-WIN: the direct path delivered a BARE "
                f"{len(apk) / 1e6:.1f} MB apk - recipient installs it directly. "
                f"server stored mime={s.get('mime')} name={s.get('file_name')} "
                f"size={s.get('size')}")
            REPORT["breakthroughs"].append({"label": "direct-raw-apk",
                                           "kind": "DIRECT-WIN",
                                           "recipient": "none - a bare apk",
                                           "stored": s})
        else:
            bad("direct path: accepted but the apk is NOT in Saved Messages - the "
                "content filter now covers the media host too")
    except Exception as exc:  # noqa: BLE001
        bad(f"direct send raised: {type(exc).__name__}: {str(exc)[:160]}")
        REPORT["direct_traceback"] = traceback.format_exc()
    finally:
        try:
            await sender.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await sess.close()
        except Exception:  # noqa: BLE001
            pass


# ==========================================================================
# main
# ==========================================================================

async def run(args) -> int:
    global _SESSION
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"apk_deep_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver

    banner("STEP 1  -  open the account")
    light = False if args.no_light else None
    session = await BrowserSession(args.account, headed=args.headed,
                                   light_assets=light).start()
    _SESSION = session
    page = session.page
    real_apk = args.real_apk if args.real_apk and os.path.isfile(args.real_apk) else None
    try:
        resp = await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                               timeout=120_000)
        info(f"HTTP {resp.status if resp else '?'}; booting {args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        st = await EitaaDriver(session).login_state()
        if not st.get("logged_in"):
            bad(f"not logged in ({st})")
            REPORT["verdict"].append("Not logged in - log in first.")
            summarise(outdir)
            return 2
        ok(f"logged in: my_id={st.get('my_id')}")
        if real_apk:
            ok(f"using real apk {real_apk} ({os.path.getsize(real_apk) / 1e6:.1f} MB)")
        else:
            info("using a synthetic apk; --real-apk gives the honest answer")
        _, err = await ev(page, f"() => {{ {SENDER_JS} }}", timeout=30)
        if err:
            bad(f"sender inject failed - {err}")
            return 2

        size_b = int(args.size_kb * 1024)
        size_big = int(float(args.max_mb) * 1024 * 1024)

        banner("STEP 2  -  the matrix (small, fast)")
        cases = build_cases(size_b, real_apk)
        for c in cases:
            c["marker"] = mk_marker(c["label"])
            info(f"[{c['group']}] {c['label']}: {c['why']}")
            res = await send_case(page, c["name"], c["mime"], c["bytes"],
                                  c["marker"], args.case_timeout)
            c["result"] = res
            (ok if res.get("ok") else bad)(f"        call: {res.get('code')}")
            REPORT["cases"].append(c)
            await asyncio.sleep(float(args.pace))

        banner(f"STEP 3  -  server read-back (waiting {args.settle:.0f}s)")
        back = await read_back(page, args.settle)
        match((back.get("messages") or []), REPORT["cases"])
        for c in REPORT["cases"]:
            s = c.get("stored") or {}
            if c.get("delivered"):
                ok(f"DELIVERED  {c['label']:<18} stored mime={s.get('mime')} "
                   f"name={s.get('file_name')} size={s.get('size')}  "
                   f"recipient: {c.get('extract')}")
            elif c.get("result", {}).get("ok"):
                bad(f"SILENT     {c['label']:<18} accepted then dropped")
            else:
                bad(f"REFUSED    {c['label']:<18} {str(c['result'].get('code'))[:50]}")

        banner("STEP 4  -  which single signal is the trigger")
        q1 = {c["label"][3:]: c.get("delivered") for c in REPORT["cases"]
              if c["label"].startswith("q1-")}
        hits = [k for k, v in q1.items() if v]
        REPORT["q1"] = q1
        if hits:
            ok(f"single-zip delivered once these were changed: {hits}")
            info("that/those is the trigger - changing only it lets a SINGLE-zip "
                 "apk through, so the recipient extracts just once")
        else:
            info("no single-signal removal let a one-level zip through, so the "
                 "filter uses several signals together or scans magic bytes - "
                 "the one-step wrappers (gz / prepend / ext) are then the hope")

        banner("STEP 5  -  drill into every lead at realistic size")
        leads = [c for c in REPORT["cases"] if c.get("delivered") and c.get("follow")]
        if not leads:
            info("no lead to scale up")
        for c in leads:
            await escalate(page, c, size_big, real_apk, args.settle)
            await asyncio.sleep(float(args.pace))

        if not args.no_direct:
            try:
                await try_direct(args.account, size_big, real_apk, args.settle, page)
            except Exception:  # noqa: BLE001
                REPORT["direct_outer_traceback"] = traceback.format_exc()
                bad("direct route raised; see report")
        try:
            await page.screenshot(path=str(outdir / "page.png"))
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
    banner("SUMMARY")
    last = None
    for c in REPORT["cases"]:
        if c.get("group") != last:
            print(f"\n  -- {c.get('group')} --", flush=True)
            last = c.get("group")
        mark = ("DELIVERED" if c.get("delivered")
                else "silent-drop" if c.get("result", {}).get("ok") else "refused")
        print(f"  {mark:<12} {c['label']:<22} recipient: {c.get('extract', '?')}",
              flush=True)

    banner("VERDICT")
    v = REPORT["verdict"]
    bk = REPORT["breakthroughs"]
    direct_wins = [b for b in bk if b.get("kind") == "DIRECT-WIN"]
    one_step = [b for b in bk if b.get("kind") == "ONE-STEP"]
    if direct_wins:
        v.append("BEST: a BARE apk delivers (no extraction by the recipient):")
        for b in direct_wins:
            v.append(f"  - {b['label']} ({b.get('recipient')}) -> {b.get('stored')}")
        v.append("This is the one to wire in: the recipient installs the apk "
                 "directly, nothing to unzip.")
    elif one_step:
        v.append("A ONE-STEP route works (recipient opens once):")
        for b in one_step:
            v.append(f"  - {b['label']}: recipient does '{b.get('recipient')}' "
                     f"-> {b.get('stored')}")
        v.append("Better than the double-zip. Wire the winner in; the recipient "
                 "note tells them the single step.")
    elif bk:
        v.append("Only multi-step routes delivered:")
        for b in bk:
            v.append(f"  - {b['label']}: {b.get('recipient')}")
    else:
        v.append("Nothing delivered an apk this round. STEP 4 says whether any "
                 "single signal matters; if not, Eitaa scans magic bytes on every "
                 "path and the realistic options narrow to: a password-protected "
                 "archive (contents unreadable to the scanner), or splitting the "
                 "apk into parts the recipient rejoins. Both can be added here.")
    q1 = REPORT.get("q1") or {}
    hits = [k for k, val in q1.items() if val]
    if hits and not direct_wins:
        v.append(f"Diagnosis: changing {hits} lets a single-zip apk through, so a "
                 f"one-extract wrapper that renames exactly that is viable.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)
    try:
        p = outdir / "report.json"
        p.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print(f"\n  full report: {p}", flush=True)
        print("  probe messages tagged APKDEEP-* in Saved Messages.", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Pinpoint Eitaa's apk filter and find a one-step route. "
                    "Sends only to your own Saved Messages.")
    p.add_argument("--account", required=True)
    p.add_argument("--size-kb", type=float, default=12)
    p.add_argument("--max-mb", type=float, default=9.6)
    p.add_argument("--real-apk", default=None, help="path to a real apk")
    p.add_argument("--no-direct", action="store_true")
    p.add_argument("--wait", type=float, default=20)
    p.add_argument("--pace", type=float, default=1.5)
    p.add_argument("--settle", type=float, default=20)
    p.add_argument("--case-timeout", type=float, default=120)
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
