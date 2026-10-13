#!/usr/bin/env python3
"""Make a SINGLE zip that carries an apk and still delivers. One unzip, done.

Where we are
------------
Measured on this account, server-verified:

    zip of note.txt (one small text entry)      DELIVERED
    zip of app.apk  (one apk entry)              dropped
    zip of app.apk inside another zip (double)   DELIVERED

So Eitaa opens a zip and inspects it ONE level deep. A plain text zip passes; a
zip whose entry is an apk does not. A double zip passes only because the
inspector stops before the second layer - but two unzips is unacceptable for a
real recipient.

The goal here is a ONE-unzip answer: a single .zip the user opens once to get a
working app.apk. To get there we need to know exactly what, at that one level,
makes the inspector reject the archive. The last probe removed apk SIGNALS and
none alone was the trigger - but it always kept the inner entry recognisable as
an apk/zip. This probe asks different, sharper questions about the WRAPPER
itself.

The questions, each a real single zip the recipient opens once
--------------------------------------------------------------
  nested-ext         inner entry is app.apk            (baseline: known to drop)
  nested-renamed     inner entry is app.dat            does the INNER NAME matter?
  nested-stored      inner entry STORED, not deflated  does the scan decompress?
  nested-plus-decoy  app.apk + several innocent files  does a decoy dilute it?
  nested-deep-path   a/b/c/d/app.apk                   does a deep path hide it?
  nested-many        20 junk entries + app.apk         does entry COUNT matter?
  nested-apk-as-txt  the apk bytes stored as "app.txt" does the inner EXT matter?
  nested-apk-split   app.apk cut into app.part1..3     no entry IS an apk, user
                                                        rejoins -> still one unzip
  nested-encrypted   app.apk in a PASSWORD zip         the scanner cannot read
                                                        the entry at all
  nested-prepended   inner apk has junk prepended      inner no longer parses as
                                                        a zip to a quick check
  nested-big-first   a big innocent file BEFORE the    does the scanner give up
                     apk entry                          after N bytes?
  nested-comment     zip with a long archive comment   does a comment break the
                                                        one-level walk?

Whichever delivers is confirmed at a realistic size and checked to round-trip
back to a valid apk, so a pass is a usable method, not a curiosity. The password
and split cases trade one unzip for one extra hint to the recipient, so they are
ranked below the plain one-unzip wins.

Self-healing
------------
A delivered case becomes a lead and is re-sent at --max-mb, printing LEAD then
BREAKTHROUGH, and ranked ONE-UNZIP (best), ONE-UNZIP+note, or FALLBACK.

Safety: everything goes to YOUR OWN Saved Messages, tagged ZWRAP-*. Payloads are
small until a lead is confirmed. No project file is modified.

Run on the SERVER, bot stopped:

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/zip_wrap_deep.py --account 989104938987
    systemctl start mkwlsoso-bot

    # the honest test uses your real apk:
    .venv/bin/python deploy/zip_wrap_deep.py --account 989104938987 --real-apk /root/app.apk
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
import struct
import sys
import time
import traceback
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"cases": [], "breakthroughs": [], "verdict": []}
_T0 = time.time()


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
# payloads - every case is a single zip the recipient opens once
# ==========================================================================

def base_apk(size_b: int, real: str | None) -> bytes:
    if real and os.path.isfile(real):
        return open(real, "rb").read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml",
                   '<?xml version="1.0" encoding="utf-8"?>\n'
                   '<manifest package="com.mkwl.probe" />\n')
        z.writestr("classes.dex", b"dex\n035\x00" + os.urandom(1024))
        z.writestr("resources.arsc", os.urandom(256))
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("assets/payload.bin", os.urandom(max(1024, size_b)))
    return buf.getvalue()


def one_zip(entries, comp=zipfile.ZIP_DEFLATED, comment: bytes | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", comp) as z:
        for name, data in entries:
            z.writestr(name, data)
        if comment:
            z.comment = comment
    return buf.getvalue()


def encrypted_zip(inner_name: str, apk: bytes, password: str) -> bytes | None:
    """A password-protected zip. The scanner cannot read the entry's content.

    Prefer pyminizip (real ZipCrypto) if present; otherwise return None and the
    case is reported as unavailable rather than faked.
    """
    try:
        import pyminizip  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    import tempfile
    src = tempfile.NamedTemporaryFile(suffix=".apk", delete=False)
    dst = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    try:
        src.write(apk)
        src.close()
        dst.close()
        pyminizip.compress(src.name, None, dst.name, password, 5)
        return open(dst.name, "rb").read()
    except Exception:  # noqa: BLE001
        return None
    finally:
        for p in (src.name, dst.name):
            try:
                os.unlink(p)
            except Exception:  # noqa: BLE001
                pass


def split_bytes(data: bytes, parts: int) -> list[tuple[str, bytes]]:
    step = (len(data) + parts - 1) // parts
    out = []
    for i in range(parts):
        chunk = data[i * step:(i + 1) * step]
        if chunk:
            out.append((f"app.part{i + 1}", chunk))
    return out


def build_cases(size_b: int, real: str | None) -> list[dict]:
    apk = base_apk(size_b, real)
    junk = ("read me - unzip and install the apk\n" * 20).encode()
    rows: list[dict] = []

    def add(label, data, why, extract, follow=True, note=None, name="bundle.zip"):
        rows.append({"label": label, "name": name, "mime": ZIP_MIME, "bytes": data,
                     "why": why, "extract": extract, "follow": follow, "note": note})

    # control + known-drop baseline
    add("ctl-text", one_zip([("note.txt", b"hello" * 10)]),
        "a plain text zip must deliver", "n/a", follow=False)
    add("nested-ext", one_zip([("app.apk", apk)]),
        "inner entry is app.apk - the known drop, the baseline", "1x unzip",
        follow=False)

    # does the INNER NAME / EXTENSION matter?
    add("nested-renamed", one_zip([("app.dat", apk)]),
        "inner entry renamed to app.dat - does the inspector key on the inner "
        "NAME?", "1x unzip + rename to .apk")
    add("nested-apk-as-txt", one_zip([("app.txt", apk)]),
        "apk bytes stored as app.txt - inner extension vs inner content",
        "1x unzip + rename to .apk")

    # does HOW it is stored matter?
    add("nested-stored", one_zip([("app.apk", apk)], comp=zipfile.ZIP_STORED),
        "the apk STORED, not deflated - does the scan decompress to look inside?",
        "1x unzip")

    # does structure AROUND the apk hide it?
    add("nested-decoy", one_zip(
        [("readme.txt", junk), ("icon.png", os.urandom(512)),
         ("app.apk", apk), ("license.txt", b"MIT\n")]),
        "apk among several innocent files - does a decoy dilute the signal?",
        "1x unzip")
    add("nested-deep-path", one_zip([("files/data/bundle/app.apk", apk)]),
        "apk under a deep folder path", "1x unzip")
    add("nested-many", one_zip(
        [(f"j{i}.txt", os.urandom(64)) for i in range(20)] + [("app.apk", apk)]),
        "20 junk entries then the apk - does entry COUNT or position matter?",
        "1x unzip")
    add("nested-big-first", one_zip(
        [("aaa_big.bin", os.urandom(max(size_b * 4, 65536))), ("app.apk", apk)]),
        "a big innocent entry BEFORE the apk - does the scan give up after N "
        "bytes?", "1x unzip")
    add("nested-comment", one_zip([("app.apk", apk)], comment=b"X" * 2000),
        "a long archive comment - does it break the one-level walk?", "1x unzip")

    # inner no longer parses as an apk/zip to a quick check, user fixes it
    add("nested-inner-prepend", one_zip([("app.apk", os.urandom(2048) + apk)]),
        "inner apk has junk prepended, so a quick parse of the inner entry fails "
        "while a zip tool still opens the OUTER fine", "1x unzip + strip header")

    # no entry IS an apk - the recipient rejoins the parts (still one unzip)
    add("nested-split", one_zip(split_bytes(apk, 3) + [("JOIN.txt",
        b"copy /b app.part1+app.part2+app.part3 app.apk\n")]),
        "apk split into parts, no single entry is an apk", "1x unzip + join parts")

    # the scanner literally cannot read the content
    enc = encrypted_zip("app.apk", apk, "install")
    if enc is not None:
        add("nested-encrypted", enc,
            "apk in a PASSWORD zip (password 'install') - the scanner cannot read "
            "the entry content at all", "1x unzip with password 'install'")
    else:
        rows.append({"label": "nested-encrypted", "skip": True,
                     "why": "pyminizip not installed; `pip install pyminizip` to "
                            "test the password-zip route"})

    return rows


# ==========================================================================
# page sender (the proven proxy mechanism) + verification
# ==========================================================================

SENDER_JS = r"""
(() => {
  if (window.__ZW) return;
  const Z = {}; window.__ZW = Z;
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  function MGR(n){ try{ if(window.__MKWL_mgr) return window.__MKWL_mgr(n); }catch(e){} return window[n]||null; }
  function er(e){ try{ return String((e&&(e.type||e.error_message||e.code||e.message))||e);}catch(x){return "ERR";} }
  function b2u(b64){ const s=atob(b64); const u=new Uint8Array(s.length); for(let i=0;i<s.length;i++) u[i]=s.charCodeAt(i); return u; }
  Z.selfId = async function(){
    const c=[];
    try{ if(window.rootScope&&window.rootScope.getMyId) c.push(await window.rootScope.getMyId()); }catch(e){}
    try{ const M=MGR("appUsersManager"); if(M&&M.getSelf){const s=await M.getSelf(); if(s&&s.id!=null)c.push(s.id);} }catch(e){}
    for(const x of c){ if(x!=null&&+x!==0&&!isNaN(+x)) return +x; }
    return null;
  };
  Z.send = async function(a){
    const t0=Date.now();
    const M=MGR("appMessagesManager");
    if(!M||typeof M.sendFile!=="function") return {ok:false, code:"no sendFile"};
    const sid=await Z.selfId();
    if(sid==null) return {ok:false, code:"no self id"};
    let f;
    try{ f=new File([b2u(a.b64)], a.name, {type:a.mime}); }catch(e){ return {ok:false, code:"build:"+er(e)}; }
    try{ await M.sendFile({peerId:sid, file:f, caption:a.marker}); return {ok:true, ms:Date.now()-t0}; }
    catch(e){ return {ok:false, code:er(e), ms:Date.now()-t0}; }
  };
  Z.readBack = async function(limit){
    const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
    try{
      const h=await AM.invokeApi("messages.getHistory",{peer:{_:"inputPeerSelf"},offset_id:0,offset_date:0,add_offset:0,limit:limit||100,max_id:0,min_id:0,hash:0});
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
    return f"ZWRAP-{rnd}-{label}"


async def send_case(page, name, mime, data, marker, timeout):
    arg = {"b64": base64.b64encode(data).decode("ascii"), "name": name,
           "mime": mime, "marker": marker}
    js = ("async (a) => { const Z = window.__ZW; try { return await Z.send(a); } "
          "catch (e) { return { ok: false, code: 'threw:' + String(e) }; } }")
    res, err = await ev(page, js, arg, timeout=timeout)
    return res or {"ok": False, "code": err}


async def read_back(page, settle):
    await asyncio.sleep(settle)
    back, err = await ev(page, "async () => await window.__ZW.readBack(110)", timeout=120)
    return back or {"ok": False, "code": err}


def match(messages, records):
    for rec in records:
        if rec.get("skip"):
            continue
        found = next((m for m in messages if rec["marker"] in (m.get("message") or "")), None)
        rec["delivered"] = bool(found)
        rec["stored"] = found


def rank(extract: str) -> str:
    e = (extract or "").lower()
    if e == "1x unzip":
        return "ONE-UNZIP"
    if "1x unzip" in e:
        return "ONE-UNZIP+note"
    return "FALLBACK"


async def escalate(page, rec, apk_big, settle):
    label = rec["label"]
    lead(f"{label} delivered small - re-sending at realistic size")
    # rebuild the SAME wrapper at full apk size
    import importlib
    big = rebuild_big(label, apk_big)
    if big is None:
        info(f"{label}: cannot rebuild at size, skipping")
        return
    marker = mk_marker(label + "-REAL")
    info(f"{label}: {len(big) / 1e6:.1f} MB")
    res = await send_case(page, rec["name"], rec["mime"], big, marker,
                          timeout=max(600, len(big) // 20000))
    follow = {"label": label + "-REAL", "name": rec["name"], "mime": rec["mime"],
              "marker": marker, "result": res, "extract": rec.get("extract"),
              "group": "escalation", "why": "lead at realistic size"}
    REPORT["cases"].append(follow)
    if not res.get("ok"):
        bad(f"{label}-REAL: send failed - {res.get('code')}")
        return
    back = await read_back(page, settle)
    match((back.get("messages") or []), [follow])
    if follow.get("delivered"):
        s = follow.get("stored") or {}
        r = rank(rec.get("extract"))
        win(f"{r}: {label} works at {len(big) / 1e6:.1f} MB "
            f"(recipient: {rec.get('extract')}). stored mime={s.get('mime')} "
            f"name={s.get('file_name')} size={s.get('size')}")
        REPORT["breakthroughs"].append({"label": label, "rank": r,
                                        "recipient": rec.get("extract"),
                                        "note": rec.get("note"), "stored": s})
    else:
        bad(f"{label}-REAL: delivered small, DROPPED at size")


_BIG_APK = None  # set in run()


def rebuild_big(label: str, apk: bytes) -> bytes | None:
    junk = ("read me - unzip and install the apk\n" * 20).encode()
    base = label.replace("-REAL", "")
    if base == "nested-renamed":
        return one_zip([("app.dat", apk)])
    if base == "nested-apk-as-txt":
        return one_zip([("app.txt", apk)])
    if base == "nested-stored":
        return one_zip([("app.apk", apk)], comp=zipfile.ZIP_STORED)
    if base == "nested-decoy":
        return one_zip([("readme.txt", junk), ("icon.png", os.urandom(512)),
                        ("app.apk", apk), ("license.txt", b"MIT\n")])
    if base == "nested-deep-path":
        return one_zip([("files/data/bundle/app.apk", apk)])
    if base == "nested-many":
        return one_zip([(f"j{i}.txt", os.urandom(64)) for i in range(20)] +
                       [("app.apk", apk)])
    if base == "nested-big-first":
        return one_zip([("aaa_big.bin", os.urandom(262144)), ("app.apk", apk)])
    if base == "nested-comment":
        return one_zip([("app.apk", apk)], comment=b"X" * 2000)
    if base == "nested-inner-prepend":
        return one_zip([("app.apk", os.urandom(2048) + apk)])
    if base == "nested-split":
        return one_zip(split_bytes(apk, 3) + [("JOIN.txt", b"join parts\n")])
    if base == "nested-encrypted":
        return encrypted_zip("app.apk", apk, "install")
    if base == "nested-ext":
        return one_zip([("app.apk", apk)])
    return None


# ==========================================================================
# main
# ==========================================================================

async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"zip_wrap_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver

    banner("STEP 1  -  open the account")
    light = False if args.no_light else None
    session = await BrowserSession(args.account, headed=args.headed,
                                   light_assets=light).start()
    page = session.page
    real = args.real_apk if args.real_apk and os.path.isfile(args.real_apk) else None
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
        if real:
            ok(f"using real apk {real} ({os.path.getsize(real) / 1e6:.1f} MB)")
        else:
            info("synthetic apk; --real-apk is the honest test")
        _, err = await ev(page, f"() => {{ {SENDER_JS} }}", timeout=30)
        if err:
            bad(f"sender inject failed - {err}")
            return 2

        size_b = int(args.size_kb * 1024)
        big_apk = base_apk(int(float(args.max_mb) * 1024 * 1024), real)

        banner("STEP 2  -  the single-zip matrix (small, fast)")
        cases = build_cases(size_b, real)
        for c in cases:
            if c.get("skip"):
                bad(f"SKIP   {c['label']:<22} {c['why']}")
                REPORT["cases"].append(c)
                continue
            c["marker"] = mk_marker(c["label"])
            info(f"{c['label']}: {c['why']}")
            try:
                inner = zipfile.ZipFile(io.BytesIO(c["bytes"])).namelist()
                info(f"        outer entries: {inner[:6]}"
                     + (" ..." if len(inner) > 6 else ""))
            except Exception:  # noqa: BLE001
                pass
            res = await send_case(page, c["name"], c["mime"], c["bytes"],
                                  c["marker"], args.case_timeout)
            c["result"] = res
            (ok if res.get("ok") else bad)(f"        call: {res.get('code', 'ok')}")
            REPORT["cases"].append(c)
            await asyncio.sleep(float(args.pace))

        banner(f"STEP 3  -  server read-back (waiting {args.settle:.0f}s)")
        back = await read_back(page, args.settle)
        match((back.get("messages") or []), REPORT["cases"])
        for c in REPORT["cases"]:
            if c.get("skip"):
                continue
            s = c.get("stored") or {}
            if c.get("delivered"):
                ok(f"DELIVERED  {c['label']:<22} recipient: {c.get('extract')}  "
                   f"(stored {s.get('mime')} {s.get('size')})")
            elif c.get("result", {}).get("ok"):
                bad(f"SILENT     {c['label']:<22} accepted then dropped")
            else:
                bad(f"REFUSED    {c['label']:<22} {str(c['result'].get('code'))[:46]}")

        banner("STEP 4  -  drill into leads at a realistic apk size")
        leads = [c for c in REPORT["cases"]
                 if c.get("delivered") and c.get("follow")]
        if not leads:
            info("no single-zip lead delivered this round")
        for c in leads:
            await escalate(page, c, big_apk, args.settle)
            await asyncio.sleep(float(args.pace))

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
    for c in REPORT["cases"]:
        if c.get("skip"):
            print(f"  skipped      {c['label']:<22} {c.get('why', '')[:50]}", flush=True)
            continue
        mark = ("DELIVERED" if c.get("delivered")
                else "silent-drop" if c.get("result", {}).get("ok") else "refused")
        print(f"  {mark:<12} {c['label']:<22} recipient: {c.get('extract', '?')}",
              flush=True)

    banner("VERDICT")
    v = REPORT["verdict"]
    bk = REPORT["breakthroughs"]
    best = [b for b in bk if b.get("rank") == "ONE-UNZIP"]
    noted = [b for b in bk if b.get("rank") == "ONE-UNZIP+note"]
    if best:
        v.append("A SINGLE-UNZIP route works - the recipient opens one .zip and "
                 "gets app.apk, nothing else:")
        for b in best:
            v.append(f"  - {b['label']}  (stored {b.get('stored')})")
        v.append("This is the one to wire into the bot's file path: wrap an .apk "
                 "in one zip of this shape before upload, send it as .zip. I can "
                 "do that from the project's own code (direct/apk_mode.py is the "
                 "natural home), no new mechanism.")
    elif noted:
        v.append("A one-unzip-plus-one-hint route works (recipient unzips once, "
                 "then does one small thing):")
        for b in noted:
            v.append(f"  - {b['label']}: recipient does '{b.get('recipient')}'  "
                     f"(stored {b.get('stored')})")
        v.append("Usable with a short recipient note. If you want ZERO extra "
                 "steps, the fully-clean single unzip was not found this round - "
                 "say the word and I will widen the wrapper shapes.")
    else:
        v.append("No single zip delivered an apk this round. Eitaa's one-level "
                 "scan recognises a nested zip/apk by content regardless of inner "
                 "name, extension, position, decoys, deflate vs store, comment or "
                 "a prepended header. The shapes that remain are: a password zip "
                 "(if pyminizip is installed - the scanner cannot read the entry) "
                 "and the split-parts zip (no entry is itself an archive). "
                 "Whichever of those delivered above is the way; if neither ran, "
                 "install pyminizip and re-run.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)
    try:
        p = outdir / "report.json"
        p.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print(f"\n  full report: {p}", flush=True)
        print("  probe messages tagged ZWRAP-* in Saved Messages.", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Find a single zip carrying an apk that Eitaa delivers. "
                    "Sends only to your own Saved Messages.")
    p.add_argument("--account", required=True)
    p.add_argument("--size-kb", type=float, default=12)
    p.add_argument("--max-mb", type=float, default=9.6)
    p.add_argument("--real-apk", default=None, help="path to a real apk")
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
