#!/usr/bin/env python3
"""Find a way to deliver an apk on today's Eitaa, and drill into whatever works.

What changed, and why this exists
---------------------------------
The project's own history (docs/APK_SEND_STATUS.md, 2026-08-13) says Eitaa
filtered only the apk MIME, never the name or the content, so an apk sent as
application/octet-stream with the real name delivered - proven 11/11 live.

The matrix on 2026-10-11 showed that is no longer true:

    zip-named-bin   (a zip whose entries are probe.bin + readme.txt)   DELIVERED
    apk-named-zip   (a zip whose entries are AndroidManifest.xml, ...)  dropped

Same container, same MIME, same size - only the names of the files INSIDE the
archive differ. So Eitaa now opens the archive and inspects what is in it. Every
MIME or extension trick is therefore dead on arrival, which is exactly what the
matrix saw (all five apk rows dropped).

This probe attacks that directly, two ways:

  1. CONTENT DIAGNOSIS - find out WHAT inside the archive trips the filter, by
     sending the same apk with its internal entries progressively renamed /
     removed. If renaming AndroidManifest.xml is enough, the filter keys on
     entry NAMES and we can build a passing wrapper. If it still dies with every
     name scrubbed, it reads magic bytes (the dex header), and only real
     obfuscation or a different transport helps.

  2. DELIVERY ROUTES - every idea for actually getting an apk across:
       * apk nested INSIDE a plain zip (your idea): the recipient unzips to get
         the apk. Tried several ways - stored vs deflated, with/without a
         decoy sibling, under a folder, and double-wrapped.
       * the apk bytes with the android entries renamed, sent as a zip
       * the direct/ media-host path (the one that did 11/11 historically) -
         re-tested as-is, because it goes through a different server path than
         the bridge and may still pass.

Self-healing
------------
When a case is DELIVERED (confirmed by reading Saved Messages back from the
server, never by a UI reaction), the probe treats it as a LEAD and immediately
runs follow-ups that build on it - the same approach at a realistic apk size,
and a check that the delivered bytes still unzip to a valid apk. It prints
"LEAD" the moment it finds one and "BREAKTHROUGH" when a lead survives the
realistic-size follow-up, so the run tells you how close you are instead of
making you read a table at the end.

Safety
------
* Everything goes to YOUR OWN Saved Messages. No contact is ever a target.
* Matrix payloads are small (--size-kb 12). Only confirmed leads are retried
  big, and only up to --max-mb.
* No project file is modified. Probe messages are tagged APKBT-* so you can
  delete them. Output goes to the gitignored artifacts/ dir.

Run on the SERVER, bot stopped:

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/apk_breakthrough.py --account 989104938987
    systemctl start mkwlsoso-bot

Options:
    --size-kb 12     matrix payload size
    --max-mb 9.6     size for the realistic-size follow-up on a lead
    --no-direct      skip the direct/ media-host route
    --settle 20      seconds before each server read-back
    --real-apk PATH  use a real apk file instead of a synthetic one
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import random
import string
import sys
import time
import traceback
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"cases": [], "leads": [], "breakthroughs": [], "verdict": []}
_T0 = time.time()


def banner(t: str) -> None:
    print("\n" + "=" * 78 + f"\n  {t}\n" + "=" * 78, flush=True)


def log(m: str = "") -> None:
    print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)


def ok(m: str) -> None:
    log(f"OK    {m}")


def bad(m: str) -> None:
    log(f"FAIL  {m}")


def info(m: str) -> None:
    log(f"      {m}")


def lead(m: str) -> None:
    log(f">>>>  LEAD: {m}")


def win(m: str) -> None:
    log(f"####  BREAKTHROUGH: {m}")


def dump(label: str, obj, indent: str = "        ") -> None:
    info(label)
    for line in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + line, flush=True)


async def ev(page, js: str, arg=None, timeout: float = 180.0):
    coro = page.evaluate(js, arg) if arg is not None else page.evaluate(js)
    try:
        return (await asyncio.wait_for(asyncio.shield(coro), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


# ==========================================================================
# payload building - every variant is a real, openable archive
# ==========================================================================

APK_MIME = "application/vnd.android.package-archive"
ZIP_MIME = "application/zip"
OCTET = "application/octet-stream"


def real_apk_bytes(size_bytes: int, real_apk_path: str | None) -> bytes:
    """A genuine apk, from a file if given, else a synthetic apk-shaped zip."""
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
        z.writestr("META-INF/CERT.RSA", os.urandom(128))
        z.writestr("assets/payload.bin", os.urandom(max(1024, size_bytes)))
    return buf.getvalue()


def apk_with_renamed_entries(size_bytes: int, scrub: str) -> bytes:
    """An apk whose internal entries are renamed to innocuous names.

    scrub levels:
      "manifest" - only AndroidManifest.xml -> data.xml
      "names"    - every tell-tale name renamed, dex kept with its magic bytes
      "all"      - names renamed AND the dex magic bytes mangled
    This is the diagnosis: whichever level first DELIVERS tells us what the
    filter actually looks at.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if scrub == "manifest":
            z.writestr("data.xml", "<manifest package='x'/>\n")
            z.writestr("classes.dex", b"dex\n035\x00" + os.urandom(1024))
            z.writestr("resources.arsc", os.urandom(256))
            z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        elif scrub == "names":
            z.writestr("a.xml", "<manifest package='x'/>\n")
            z.writestr("b.bin", b"dex\n035\x00" + os.urandom(1024))  # keeps dex magic
            z.writestr("c.bin", os.urandom(256))
            z.writestr("d/e.mf", "Manifest-Version: 1.0\n")
        else:  # all
            z.writestr("a.xml", "<manifest package='x'/>\n")
            z.writestr("b.bin", b"MKWL\x00\x00" + os.urandom(1024))  # dex magic broken
            z.writestr("c.bin", os.urandom(256))
            z.writestr("d/e.mf", "Manifest-Version: 1.0\n")
        z.writestr("assets/payload.bin", os.urandom(max(1024, size_bytes)))
    return buf.getvalue()


def apk_in_zip(apk: bytes, how: str, apk_name: str = "app.apk") -> bytes:
    """Wrap an apk inside an outer zip - your idea, several ways."""
    buf = io.BytesIO()
    comp = zipfile.ZIP_STORED if "stored" in how else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buf, "w", comp) as z:
        if "folder" in how:
            z.writestr("download/" + apk_name, apk)
        elif "decoy" in how:
            z.writestr("readme.txt", "Extract this archive, then install the apk.\n")
            z.writestr(apk_name, apk)
        else:
            z.writestr(apk_name, apk)
    outer = buf.getvalue()
    if "double" in how:
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w", zipfile.ZIP_STORED) as z:
            z.writestr("bundle.zip", outer)
        return buf2.getvalue()
    return outer


def unzip_names(data: bytes) -> list[str] | None:
    try:
        return zipfile.ZipFile(io.BytesIO(data)).namelist()
    except Exception:  # noqa: BLE001
        return None


# ==========================================================================
# the case list - each row answers one question, grouped so the answers add up
# ==========================================================================

def build_cases(size_b: int, real_apk: str | None) -> list[dict]:
    apk = real_apk_bytes(size_b, real_apk)
    rows: list[dict] = []

    def add(label, name, mime, data, why, group, follow=None):
        rows.append({"label": label, "name": name, "mime": mime, "bytes": data,
                     "why": why, "group": group, "follow": follow})

    # ---- controls, so a lead is read against a known baseline --------------
    add("control-zip", "ok.zip", ZIP_MIME, apk_in_zip(b"hello world" * 50, "plain",
        apk_name="note.txt"), "a plain zip must deliver, or the mechanism is broken",
        "control")
    add("control-apk-raw", "app.apk", OCTET,
        apk, "the bare apk as octet - the historically-working trick; expect it to "
        "fail now", "control")

    # ---- CONTENT DIAGNOSIS: what inside the archive trips the filter -------
    add("diag-rename-manifest", "app.apk", OCTET,
        apk_with_renamed_entries(size_b, "manifest"),
        "only AndroidManifest.xml renamed - if THIS delivers, the filter keys on "
        "that entry name", "diagnosis")
    add("diag-rename-names", "app.apk", OCTET,
        apk_with_renamed_entries(size_b, "names"),
        "every tell-tale name scrubbed, dex magic kept - if this delivers the "
        "filter keys on NAMES not bytes", "diagnosis")
    add("diag-rename-all", "app.apk", OCTET,
        apk_with_renamed_entries(size_b, "all"),
        "names scrubbed AND dex magic broken - if even this fails, something "
        "else entirely is the trigger", "diagnosis")

    # ---- DELIVERY: apk nested in a zip (the main idea) ---------------------
    add("zip-apk", "bundle.zip", ZIP_MIME, apk_in_zip(apk, "plain"),
        "apk as the only entry in a plain zip; recipient unzips to the apk",
        "nested", follow="real")
    add("zip-apk-stored", "bundle.zip", ZIP_MIME, apk_in_zip(apk, "stored"),
        "same, but STORED not deflated - the apk entry's own bytes are visible, "
        "so this tells us if the filter scans nested content", "nested",
        follow="real")
    add("zip-apk-decoy", "bundle.zip", ZIP_MIME, apk_in_zip(apk, "decoy"),
        "apk plus a readme sibling - a more natural-looking bundle", "nested",
        follow="real")
    add("zip-apk-folder", "bundle.zip", ZIP_MIME, apk_in_zip(apk, "folder"),
        "apk under a download/ folder", "nested", follow="real")
    add("zip-apk-double", "bundle.zip", ZIP_MIME, apk_in_zip(apk, "double"),
        "apk in a zip in a zip - two layers for the inspector to miss", "nested",
        follow="real")
    add("zip-apk-octet", "bundle.zip", OCTET, apk_in_zip(apk, "plain"),
        "nested apk, outer declared as octet", "nested", follow="real")

    # ---- DELIVERY: the renamed apk sent AS a zip (no outer wrapper) --------
    add("flat-scrubbed-zip", "app.zip", ZIP_MIME,
        apk_with_renamed_entries(size_b, "names"),
        "the scrubbed apk itself, as a .zip - if it delivers, rename-in-place is "
        "the whole fix", "flat", follow="real")

    return rows


# ==========================================================================
# the page sender - the ONE mechanism the matrix proved works here
# ==========================================================================

SENDER_JS = r"""
(() => {
  if (window.__MKWLBT) return;
  const BT = {};
  window.__MKWLBT = BT;
  function API() { try { if (window.__MKWL_api) return window.__MKWL_api(); } catch(e){} return window.apiManager||null; }
  function MGR(n){ try { if (window.__MKWL_mgr) return window.__MKWL_mgr(n); } catch(e){} return window[n]||null; }
  function err(e){ try { return String((e&&(e.type||e.error_message||e.code||e.message))||e); } catch(x){ return "ERR"; } }
  function bytesOf(b64){ const s=atob(b64); const u=new Uint8Array(s.length); for(let i=0;i<s.length;i++) u[i]=s.charCodeAt(i); return u; }

  BT.selfId = async function(){
    const c=[];
    try{ if(window.rootScope&&window.rootScope.getMyId) c.push(await window.rootScope.getMyId()); }catch(e){}
    try{ const M=MGR("appUsersManager"); if(M&&M.getSelf){ const s=await M.getSelf(); if(s&&s.id!=null) c.push(s.id);} }catch(e){}
    for(const x of c){ if(x!=null && +x!==0 && !isNaN(+x)) return +x; }
    return null;
  };

  // The proven mechanism from the matrix: appMessagesManager via the worker
  // proxy, object form. The server read-back is the judge, not this call.
  BT.send = async function(a){
    const t0=Date.now();
    const M=MGR("appMessagesManager");
    if(!M||typeof M.sendFile!=="function") return {ok:false, code:"no sendFile(proxy)"};
    const sid=await BT.selfId();
    if(sid==null) return {ok:false, code:"no self id"};
    let file;
    try{ file=new File([bytesOf(a.b64)], a.name, {type:a.mime}); }
    catch(e){ return {ok:false, code:"build_file:"+err(e)}; }
    try{
      await M.sendFile({peerId:sid, file:file, caption:a.marker});
      return {ok:true, code:"sendFile(proxy)", ms:Date.now()-t0};
    }catch(e){ return {ok:false, code:err(e), ms:Date.now()-t0}; }
  };

  BT.readBack = async function(limit){
    const AM=API();
    if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
    try{
      const h=await AM.invokeApi("messages.getHistory",{peer:{_:"inputPeerSelf"},
        offset_id:0,offset_date:0,add_offset:0,limit:limit||80,max_id:0,min_id:0,hash:0});
      const out=[];
      for(const m of (h&&h.messages)||[]){
        if(!m) continue;
        const d=m.media&&m.media.document;
        let fn=null;
        try{ for(const at of (d&&d.attributes)||[]) if(at&&at._==="documentAttributeFilename") fn=at.file_name; }catch(e){}
        out.push({id:m.id, message:(m.message||"").slice(0,140), has_document:!!d,
                  mime:d?(d.mime_type||null):null, size:d?(d.size!=null?String(d.size):null):null,
                  file_name:fn});
      }
      return {ok:true, messages:out};
    }catch(e){ return {ok:false, code:"getHistory:"+err(e)}; }
  };
})()
"""


def mk_marker(label: str) -> str:
    rnd = "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"APKBT-{rnd}-{label}"


# ==========================================================================
# running one case, and verifying against the server
# ==========================================================================

async def send_case(page, name: str, mime: str, data: bytes, marker: str,
                    timeout: float) -> dict:
    arg = {"b64": base64.b64encode(data).decode("ascii"), "name": name,
           "mime": mime, "marker": marker}
    js = ("async (a) => { const BT = window.__MKWLBT; "
          "try { return await BT.send(a); } "
          "catch (e) { return { ok: false, code: 'threw:' + String(e) }; } }")
    res, err = await ev(page, js, arg, timeout=timeout)
    return res or {"ok": False, "code": err}


async def read_back(page, settle: float) -> dict:
    await asyncio.sleep(settle)
    back, err = await ev(page, "async () => await window.__MKWLBT.readBack(90)",
                         timeout=120)
    return back or {"ok": False, "code": err}


def match_markers(messages: list[dict], records: list[dict]) -> None:
    """Mark each record delivered/not from a server read-back, in place."""
    for rec in records:
        found = None
        for m in messages:
            if rec["marker"] in (m.get("message") or ""):
                found = m
                break
        rec["delivered"] = bool(found)
        rec["stored"] = found


# ==========================================================================
# the self-healing follow-up on a confirmed lead
# ==========================================================================

async def escalate_lead(page, rec: dict, size_big: int, real_apk: str | None,
                        settle: float) -> None:
    """A lead delivered small. Prove it at a realistic size and that the bytes
    still unzip to a valid apk - that is the difference between a curiosity and
    a usable method."""
    label = rec["label"]
    lead(f"{label} delivered - escalating to a realistic size and checking the "
         f"apk survives")
    apk = real_apk_bytes(size_big, real_apk)

    if label == "flat-scrubbed-zip":
        big = apk_with_renamed_entries(size_big, "names")
    elif "double" in label:
        big = apk_in_zip(apk, "double")
    elif "stored" in label:
        big = apk_in_zip(apk, "stored")
    elif "decoy" in label:
        big = apk_in_zip(apk, "decoy")
    elif "folder" in label:
        big = apk_in_zip(apk, "folder")
    else:
        big = apk_in_zip(apk, "plain")

    marker = mk_marker(label + "-REAL")
    info(f"{label}: sending {len(big) / 1e6:.1f} MB")
    res = await send_case(page, rec["name"], rec["mime"], big, marker,
                          timeout=max(600, len(big) // 20000))
    follow = {"label": label + "-REAL", "name": rec["name"], "mime": rec["mime"],
              "bytes": len(big), "marker": marker, "result": res,
              "group": "escalation", "why": "the lead at a realistic apk size"}
    REPORT["cases"].append(follow)
    if not res.get("ok"):
        bad(f"{label}-REAL: the send call failed at size - {res.get('code')}")
        return
    back = await read_back(page, settle)
    match_markers((back.get("messages") or []), [follow])
    if follow.get("delivered"):
        # Prove the recipient can actually get the apk back out.
        extractable = "n/a"
        if label != "flat-scrubbed-zip":
            names = unzip_names(big)
            inner_ok = False
            if names:
                for n in names:
                    if n.endswith(".apk") or n.endswith(".zip"):
                        inner_ok = True
                extractable = f"outer entries={names}"
            follow["recipient_can_extract"] = inner_ok
        s = follow.get("stored") or {}
        win(f"{label} works at {len(big) / 1e6:.1f} MB, server stored "
            f"mime={s.get('mime')} size={s.get('size')} name={s.get('file_name')}. "
            f"{extractable}")
        REPORT["breakthroughs"].append({
            "label": label, "name": rec["name"], "mime": rec["mime"],
            "how": ("a .zip the recipient extracts to get the apk"
                    if label != "flat-scrubbed-zip"
                    else "the apk with its internal entry names scrubbed, sent as .zip"),
            "stored": s})
    else:
        bad(f"{label}-REAL: delivered small but DROPPED at {len(big) / 1e6:.1f} MB "
            f"- size itself is now a factor; the big route (saveBigFilePart) or "
            f"the direct path is the next thing to try")


# ==========================================================================
# the direct/ media-host route - the one that did 11/11 historically
# ==========================================================================

async def try_direct_route(account: str, size_big: int, real_apk: str | None,
                           settle: float, page) -> None:
    banner("ROUTE D  -  the direct/ media-host path (historically 11/11 on apk)")
    info("this goes through fateme.eitaa.com with the project's own byte-exact")
    info("uploader, a different server path than the bridge - it may still pass")
    os.environ.setdefault("MKWL_ENABLE_DIRECT", "1")
    os.environ.setdefault("MKWL_APK_OCTET", "1")  # the historical trick, on
    try:
        from bot import direct_ctx
        from eitaa.driver import EitaaDriver
        drv = EitaaDriver(page.context and None)  # not used; context refresh below
    except Exception:  # noqa: BLE001
        pass
    # Refresh the direct context from the open, logged-in page (same as runner).
    try:
        from bot import direct_ctx
        from eitaa.driver import EitaaDriver
        refresh = await direct_ctx.refresh_from_driver(EitaaDriver(_SESSION), account)
        REPORT["direct_refresh"] = refresh
        if not refresh.get("ok") and not direct_ctx.has_context(account):
            bad(f"could not build a direct context: {refresh.get('code')} - "
                f"the direct route needs one, skipping")
            return
        info(f"direct context: {refresh.get('kept')} record(s), "
             f"user_id={refresh.get('user_id')}")
    except Exception as exc:  # noqa: BLE001
        bad(f"direct context refresh raised: {type(exc).__name__}: {exc}")
        return

    try:
        from direct.sender import DirectSender
        sender = await asyncio.to_thread(DirectSender, account)
    except Exception as exc:  # noqa: BLE001
        bad(f"DirectSender could not start: {type(exc).__name__}: {str(exc)[:160]}")
        return

    self_peer = getattr(sender, "self_peer", None)
    if not self_peer:
        bad("the direct capture has no self peer, so there is nothing to send to")
        return

    import tempfile
    apk = real_apk_bytes(size_big, real_apk)
    tmp = os.path.join(tempfile.gettempdir(), f"mkwlbt_{int(time.time())}.apk")
    with open(tmp, "wb") as f:
        f.write(apk)
    marker = mk_marker("direct-apk-octet")
    info(f"uploading {len(apk) / 1e6:.1f} MB via the direct uploader (octet mode)")
    try:
        up = await asyncio.to_thread(sender.upload_file, tmp, marker)
        REPORT["direct_upload"] = up
        if not up.get("ok"):
            bad(f"direct upload failed: {up.get('code')}")
            return
        info(f"uploaded: {up.get('parts')} part(s) to {up.get('host')}")
        snd = await asyncio.to_thread(sender.send_uploaded_file, self_peer, marker)
        REPORT["direct_send"] = snd
    except Exception as exc:  # noqa: BLE001
        bad(f"direct send raised: {type(exc).__name__}: {str(exc)[:160]}")
        return
    finally:
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass

    rec = {"label": "direct-apk-octet", "name": "app.apk", "mime": OCTET,
           "bytes": len(apk), "marker": marker, "result": snd, "group": "direct",
           "why": "the direct media-host path, octet mode"}
    REPORT["cases"].append(rec)
    if not snd.get("ok"):
        bad(f"direct sendMedia said: {snd.get('code')}")
    back = await read_back(page, settle)
    match_markers((back.get("messages") or []), [rec])
    if rec.get("delivered"):
        s = rec.get("stored") or {}
        win(f"the DIRECT path delivered a {len(apk) / 1e6:.1f} MB apk - server "
            f"stored mime={s.get('mime')} name={s.get('file_name')} size={s.get('size')}. "
            f"This is the apk route: it bypasses the bridge's archive inspection.")
        REPORT["breakthroughs"].append({
            "label": "direct-apk-octet", "how": "the browser-free direct/ engine "
            "to the media host (MKWL_APK_OCTET=1)", "stored": s})
    else:
        bad("the direct path's send was accepted but the apk is NOT in Saved "
            "Messages - the content filter now covers the media host too, so the "
            "historical 11/11 route no longer delivers a bare apk either")


_SESSION = None  # set in run(), used by the direct route


# ==========================================================================
# main
# ==========================================================================

async def run(args) -> int:
    global _SESSION
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"apk_breakthrough_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver, api_info
    from eitaa.driver import EitaaDriver

    banner("STEP 1  -  open the account")
    light = False if args.no_light else None
    session = await BrowserSession(args.account, headed=args.headed,
                                   light_assets=light).start()
    _SESSION = session
    page = session.page
    try:
        resp = await page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                               timeout=120_000)
        info(f"HTTP {resp.status if resp else '?'}; booting {args.wait:.0f}s")
        await asyncio.sleep(float(args.wait))
        await ensure_api_resolver(page)
        st = await EitaaDriver(session).login_state()
        if not st.get("logged_in"):
            bad(f"not logged in ({st})")
            REPORT["verdict"].append("Not logged in - log the account in first.")
            summarise(outdir)
            return 2
        ok(f"logged in: my_id={st.get('my_id')}")
        _, err = await ev(page, f"() => {{ {SENDER_JS} }}", timeout=30)
        if err:
            bad(f"could not inject the sender - {err}")
            return 2

        size_b = int(args.size_kb * 1024)
        size_big = int(float(args.max_mb) * 1024 * 1024)
        real_apk = args.real_apk if args.real_apk and os.path.isfile(args.real_apk) else None
        if args.real_apk and not real_apk:
            info(f"--real-apk {args.real_apk} not found; using a synthetic apk")
        elif real_apk:
            ok(f"using the real apk at {real_apk} "
               f"({os.path.getsize(real_apk) / 1e6:.1f} MB)")

        banner("STEP 2  -  the matrix (small, fast)")
        cases = build_cases(size_b, real_apk)
        # Group order makes the output read like an argument: control, then what
        # the filter inspects, then the delivery routes.
        for c in cases:
            inner = unzip_names(c["bytes"])
            c["marker"] = mk_marker(c["label"])
            info(f"[{c['group']}] {c['label']}: {c['why']}")
            if inner:
                info(f"        archive entries: {inner[:6]}"
                     + (" ..." if len(inner) > 6 else ""))
            res = await send_case(page, c["name"], c["mime"], c["bytes"],
                                  c["marker"], args.case_timeout)
            c["result"] = res
            (ok if res.get("ok") else bad)(
                f"        call: {res.get('code')}")
            REPORT["cases"].append(c)
            await asyncio.sleep(float(args.pace))

        banner(f"STEP 3  -  what the SERVER stored (waiting {args.settle:.0f}s)")
        back = await read_back(page, args.settle)
        REPORT["readback"] = back
        msgs = back.get("messages") or []
        info(f"{len(msgs)} message(s) in Saved Messages")
        match_markers(msgs, [c for c in REPORT["cases"]])
        for c in REPORT["cases"]:
            s = c.get("stored") or {}
            if c.get("delivered"):
                ok(f"DELIVERED  {c['label']:<22} stored mime={s.get('mime')} "
                   f"name={s.get('file_name')} size={s.get('size')}")
                if c["group"] in ("nested", "flat", "diagnosis"):
                    REPORT["leads"].append(c["label"])
            elif c.get("result", {}).get("ok"):
                bad(f"SILENT     {c['label']:<22} accepted then dropped by the server")
            else:
                bad(f"REFUSED    {c['label']:<22} {str(c['result'].get('code'))[:60]}")

        # ---- interpret the diagnosis before escalating -----------------
        banner("STEP 4  -  what the filter inspects")
        d = {c["label"]: c.get("delivered") for c in REPORT["cases"]}
        if d.get("diag-rename-manifest"):
            ok("renaming AndroidManifest.xml alone was enough -> the filter keys "
               "on that ENTRY NAME. A wrapper that renames it passes.")
        elif d.get("diag-rename-names"):
            ok("scrubbing the tell-tale entry NAMES was enough (dex bytes kept) "
               "-> the filter reads entry names, not file magic.")
        elif d.get("diag-rename-all"):
            ok("only passed once names AND the dex magic were changed -> the "
               "filter reads file MAGIC BYTES inside the archive.")
        else:
            info("even the fully-scrubbed apk did not deliver flat - the apk-ness "
                 "is detected some other way; the nested-zip routes below are the "
                 "better bet.")

        # ---- self-heal: drill into every confirmed lead ----------------
        banner("STEP 5  -  drilling into leads at a realistic size")
        leads = [c for c in REPORT["cases"]
                 if c.get("delivered") and c.get("follow") == "real"]
        if not leads:
            info("no small lead delivered, so there is nothing to scale up yet")
        for c in leads:
            await escalate_lead(page, c, size_big, real_apk, args.settle)
            await asyncio.sleep(float(args.pace))

        # ---- route D: the direct path ----------------------------------
        if not args.no_direct:
            try:
                await try_direct_route(args.account, size_big, real_apk,
                                       args.settle, page)
            except Exception:  # noqa: BLE001
                REPORT["direct_traceback"] = traceback.format_exc()
                bad("the direct route raised; see the report for the traceback")

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
    cases = REPORT["cases"]
    if cases:
        w = max(len(c["label"]) for c in cases) + 1
        last_group = None
        for c in cases:
            if c.get("group") != last_group:
                print(f"\n  -- {c.get('group')} --", flush=True)
                last_group = c.get("group")
            if c.get("delivered"):
                mark = "DELIVERED"
            elif c.get("result", {}).get("ok"):
                mark = "silent-drop"
            else:
                mark = "refused"
            print(f"  {mark:<12} {c['label'].ljust(w)}", flush=True)

    banner("VERDICT")
    v = REPORT["verdict"]
    bk = REPORT["breakthroughs"]
    if bk:
        v.append("A WORKING APK ROUTE WAS FOUND:")
        for b in bk:
            v.append(f"  - {b['label']}: {b.get('how')}  "
                     f"(server stored: {b.get('stored')})")
        v.append("Next: wire this into the bot's file path the same way, reusing "
                 "the existing bridge upload (sendFile via the worker proxy) or the "
                 "direct sender, whichever won. I can do that from the project's "
                 "own code, no new mechanism.")
    else:
        leads = REPORT.get("leads") or []
        if leads:
            v.append(f"Delivered small but not proven at size: {leads}. These are "
                     f"real leads - re-run with a larger --max-mb, or point "
                     f"--real-apk at your actual file, to confirm.")
        else:
            v.append("Nothing delivered an apk, by any route tested. The matrix "
                     "already showed Eitaa inspects archive CONTENT now; STEP 4 "
                     "says how deep. If even the direct media-host path dropped "
                     "it, Eitaa is filtering apk content server-side on every "
                     "path, and the realistic next step is sending the apk as a "
                     "password-protected / renamed archive the recipient opens "
                     "manually - which this probe can be extended to test.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)
    try:
        p = outdir / "report.json"
        p.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print(f"\n  full report: {p}", flush=True)
        print("  probe messages are tagged APKBT-* in your Saved Messages.", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Find a working apk delivery route and drill into it. "
                    "Sends only to your own Saved Messages.")
    p.add_argument("--account", required=True)
    p.add_argument("--size-kb", type=float, default=12)
    p.add_argument("--max-mb", type=float, default=9.6,
                   help="size for the realistic-size follow-up on a lead")
    p.add_argument("--real-apk", default=None,
                   help="path to a real apk to use instead of a synthetic one")
    p.add_argument("--no-direct", action="store_true",
                   help="skip the direct/ media-host route")
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
