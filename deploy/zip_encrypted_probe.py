#!/usr/bin/env python3
"""An ENCRYPTED zip that hides the apk from Eitaa but a normal phone opens once.

The crux (measured today)
-------------------------
Eitaa opens a normal zip and reads the ENTRY NAMES one level deep; any *.apk
entry -> the whole zip is dropped. A 7z/tar slips past (it cannot read those),
but needs a special app on the phone - too hard for elderly recipients.

The target: a plain .zip the recipient opens once with the zip tool every phone
already has, that yields an installable apk, AND whose apk Eitaa cannot see.

Key fact that shapes everything: traditional ZipCrypto and standard AES-zip
encrypt file CONTENT but leave the entry NAMES in cleartext in the central
directory. So encryption ALONE does not hide "app.apk" from Eitaa - the name is
still readable. Two levers actually hide the name:

  1. rename the inner entry so it is not *.apk (e.g. app.bin / data), with or
     without encryption, and the recipient installs the extracted file anyway
     (Android installs by content, and many file managers let you tap-install a
     renamed apk; worst case they rename back).
  2. header/name encryption, where even the central directory names are
     encrypted - only some tools do this, and only some phone apps can open it.

We do not know which Eitaa drops or which a phone opens, so this tests the whole
matrix, server-judged, and ranks by how easy the result is for a non-technical
recipient:

    plain-apk-name        app.apk, no encryption            (control: dropped)
    zipcrypto-apk-name    app.apk, ZipCrypto content enc    (name still visible?)
    aes-apk-name          app.apk, AES-256 content enc      (name still visible?)
    zipcrypto-renamed     app.bin, ZipCrypto                (name hidden by rename)
    aes-renamed           app.bin, AES-256                  (name hidden by rename)
    plain-renamed         app.bin, no encryption            (does rename alone do it?)
    aes-renamed-data      data (no ext), AES-256            (most neutral name)

For each delivered case the report says exactly what the recipient must do:
"open with password X, tap app.apk" is ideal; "...then rename to .apk" is a
step worse. The password (if any) is a fixed simple word shown in the output so
you could tell recipients once.

Safety: own Saved Messages only, tagged ZENC-*. Small payloads. pyzipper is
pip-installed on the spot if missing. No project file modified.

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/zip_encrypted_probe.py --account 989939094655
    systemctl start mkwlsoso-bot
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import subprocess
import sys
import time
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"cases": [], "wins": [], "verdict": []}
_T0 = time.time()
PASSWORD = "123"  # a short, easy word recipients could be told once


def banner(t): print("\n" + "=" * 76 + f"\n  {t}\n" + "=" * 76, flush=True)
def log(m=""): print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)
def ok(m): log(f"OK    {m}")
def bad(m): log(f"FAIL  {m}")
def info(m): log(f"      {m}")
def win(m): log(f"####  {m}")


def apk_bytes(size_b: int, real: str | None) -> bytes:
    if real and os.path.isfile(real):
        return open(real, "rb").read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml",
                   '<?xml version="1.0"?><manifest package="com.mkwl.probe"/>\n')
        z.writestr("classes.dex", b"dex\n035\x00" + os.urandom(1024))
        z.writestr("resources.arsc", os.urandom(256))
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("assets/payload.bin", os.urandom(max(1024, size_b)))
    return buf.getvalue()


def _ensure_pyzipper():
    try:
        import pyzipper  # type: ignore
        return pyzipper
    except Exception:  # noqa: BLE001
        try:
            info("pyzipper not found; attempting pip install pyzipper ...")
            subprocess.run([sys.executable, "-m", "pip", "install", "pyzipper"],
                           capture_output=True, timeout=180)
            import pyzipper  # type: ignore
            return pyzipper
        except Exception:  # noqa: BLE001
            return None


# ---- builders: each returns (filename, mime, bytes, inner_name) or None ------

def b_plain(apk, inner):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(inner, apk)
    return buf.getvalue()


def b_zipcrypto(apk, inner):
    """Traditional ZipCrypto via the `zip` CLI (-P). Content encrypted, name
    cleartext."""
    import shutil
    import tempfile
    zbin = shutil.which("zip")
    if not zbin:
        return None
    d = tempfile.mkdtemp()
    try:
        ip = os.path.join(d, inner)
        op = os.path.join(d, "out.zip")
        open(ip, "wb").write(apk)
        r = subprocess.run([zbin, "-j", "-P", PASSWORD, op, ip],
                           capture_output=True, timeout=60, cwd=d)
        if r.returncode != 0 or not os.path.isfile(op):
            return None
        return open(op, "rb").read()
    except Exception:  # noqa: BLE001
        return None
    finally:
        import shutil as _s
        _s.rmtree(d, ignore_errors=True)


def b_aes(apk, inner, pz):
    """AES-256 via pyzipper. Content encrypted, name cleartext (standard AES
    zip)."""
    if pz is None:
        return None
    buf = io.BytesIO()
    try:
        with pz.AESZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED,
                           encryption=pz.WZ_AES) as z:
            z.setpassword(PASSWORD.encode())
            z.writestr(inner, apk)
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        info(f"aes build failed: {exc}")
        return None


def build_cases(apk, pz):
    """Each: label, filename, mime, bytes, recipient_steps."""
    ZIP = "application/zip"
    rows = []

    def add(label, data, recipient, inner):
        if data is None:
            info(f"{label}: tool unavailable, skipped")
            return
        rows.append({"label": label, "name": "app.zip", "mime": ZIP,
                     "bytes": data, "recipient": recipient, "inner": inner})

    add("plain-apk-name", b_plain(apk, "app.apk"),
        "open, tap app.apk (control - should drop)", "app.apk")
    add("zipcrypto-apk-name", b_zipcrypto(apk, "app.apk"),
        f"open w/ pass '{PASSWORD}', tap app.apk", "app.apk")
    add("aes-apk-name", b_aes(apk, "app.apk", pz),
        f"open w/ pass '{PASSWORD}', tap app.apk", "app.apk")
    add("plain-renamed", b_plain(apk, "app.bin"),
        "open, tap app.bin (rename to .apk if needed)", "app.bin")
    add("zipcrypto-renamed", b_zipcrypto(apk, "app.bin"),
        f"open w/ pass '{PASSWORD}', tap app.bin", "app.bin")
    add("aes-renamed", b_aes(apk, "app.bin", pz),
        f"open w/ pass '{PASSWORD}', tap app.bin", "app.bin")
    add("aes-renamed-data", b_aes(apk, "data", pz),
        f"open w/ pass '{PASSWORD}', tap data (rename to .apk)", "data")
    return rows


def names_visible(data: bytes) -> list:
    """What a plain central-directory read (what Eitaa does) sees. If 'app.apk'
    is here, Eitaa will drop it regardless of encryption."""
    try:
        return zipfile.ZipFile(io.BytesIO(data)).namelist()
    except Exception:  # noqa: BLE001
        return ["<unreadable - names may be header-encrypted>"]


def mk_marker(label):
    import random
    import string
    return f"ZENC-{''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))}-{label}"


# ==========================================================================
# page sender (proven appMessagesManager-via-proxy) + readback
# ==========================================================================

SENDER_JS = r"""
(() => {
  if (window.__ZE) return;
  const F = {}; window.__ZE = F;
  function MGR(n){ try{ if(window.__MKWL_mgr) return window.__MKWL_mgr(n); }catch(e){} return window[n]||null; }
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  function er(e){ try{ return String((e&&(e.type||e.error_message||e.code||e.message))||e);}catch(x){return "ERR";} }
  function b2u(b64){ const s=atob(b64); const u=new Uint8Array(s.length); for(let i=0;i<s.length;i++) u[i]=s.charCodeAt(i); return u; }
  F.selfId = async function(){
    const c=[];
    try{ if(window.rootScope&&window.rootScope.getMyId) c.push(await window.rootScope.getMyId()); }catch(e){}
    try{ const M=MGR("appUsersManager"); if(M&&M.getSelf){const s=await M.getSelf(); if(s&&s.id!=null)c.push(s.id);} }catch(e){}
    for(const x of c){ if(x!=null&&+x!==0&&!isNaN(+x)) return +x; }
    return null;
  };
  F.send = async function(a){
    const t0=Date.now();
    const M=MGR("appMessagesManager");
    if(!M||typeof M.sendFile!=="function") return {ok:false, code:"no sendFile"};
    const sid=await F.selfId();
    if(sid==null) return {ok:false, code:"no self id"};
    let f;
    try{ f=new File([b2u(a.b64)], a.name, {type:a.mime}); }catch(e){ return {ok:false, code:"build:"+er(e)}; }
    try{ await M.sendFile({peerId:sid, file:f, caption:a.marker}); return {ok:true, ms:Date.now()-t0}; }
    catch(e){ return {ok:false, code:er(e), ms:Date.now()-t0}; }
  };
  F.readBack = async function(){
    const AM=API(); if(!AM||!AM.invokeApi) return {ok:false, code:"no invokeApi"};
    try{
      const h=await AM.invokeApi("messages.getHistory",{peer:{_:"inputPeerSelf"},offset_id:0,offset_date:0,add_offset:0,limit:80,max_id:0,min_id:0,hash:0});
      const out=[];
      for(const m of (h&&h.messages)||[]){ if(!m) continue;
        const d=m.media&&m.media.document; let fn=null;
        try{ for(const at of (d&&d.attributes)||[]) if(at&&at._==="documentAttributeFilename") fn=at.file_name; }catch(e){}
        out.push({id:m.id, message:(m.message||"").slice(0,160), has_document:!!d,
                  mime:d?(d.mime_type||null):null, size:d?(d.size!=null?String(d.size):null):null, file_name:fn});
      }
      return {ok:true, messages:out};
    }catch(e){ return {ok:false, code:String(e)}; }
  };
})()
"""


async def ev(page, js, arg=None, timeout=120):
    try:
        if arg is not None:
            return (await asyncio.wait_for(page.evaluate(js, arg), timeout=timeout), None)
        return (await asyncio.wait_for(page.evaluate(js), timeout=timeout), None)
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


async def send_case(page, name, mime, data, marker, timeout):
    import base64
    arg = {"b64": base64.b64encode(data).decode("ascii"), "name": name,
           "mime": mime, "marker": marker}
    js = ("async (a) => { const F=window.__ZE; try{ return await F.send(a); }"
          "catch(e){ return {ok:false, code:'threw:'+String(e)}; } }")
    r, err = await ev(page, js, arg, timeout)
    return r or {"ok": False, "code": err}


async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"zenc_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver

    banner("STEP 1  -  open the account")
    session = await BrowserSession(args.account, headed=False, light_assets=None).start()
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
            bad(f"not logged in ({st.get('auth_state')})")
            REPORT["verdict"].append("Not logged in - log in and re-run.")
            _save(outdir)
            return 2
        ok(f"logged in: my_id={st.get('my_id')}")
        _, err = await ev(page, f"() => {{ {SENDER_JS} }}", None, 30)
        if err:
            bad(f"sender inject failed: {err}")
            return 2

        banner("STEP 2  -  build encrypted-zip variants")
        pz = _ensure_pyzipper()
        info(f"pyzipper: {'available' if pz else 'NOT available (AES cases skipped)'}  "
             f"password='{PASSWORD}'")
        apk = apk_bytes(int(args.size_kb * 1024), real)
        info(f"apk {len(apk)} bytes {'(REAL)' if real else '(synthetic)'}")
        cases = build_cases(apk, pz)
        for c in cases:
            vis = names_visible(c["bytes"])
            c["names_eitaa_sees"] = vis
            hidden = "app.apk" not in vis
            info(f"{c['label']:<20} {len(c['bytes']):>7}B  "
                 f"names Eitaa sees: {vis}  (apk name hidden: {hidden})")

        banner("STEP 3  -  send each to Saved Messages")
        for c in cases:
            c["marker"] = mk_marker(c["label"])
            r = await send_case(page, c["name"], c["mime"], c["bytes"],
                                c["marker"], args.case_timeout)
            c["result"] = r
            (ok if r.get("ok") else bad)(f"{c['label']:<20} send: {r.get('code') or 'ok'}")
            REPORT["cases"].append({k: c.get(k) for k in
                                    ("label", "marker", "result", "recipient",
                                     "inner", "names_eitaa_sees")})
            await asyncio.sleep(1.5)

        banner(f"STEP 4  -  server read-back (waiting {args.settle:.0f}s)")
        await asyncio.sleep(float(args.settle))
        back, _ = await ev(page, "async () => await window.__ZE.readBack()", None, 60)
        msgs = (back or {}).get("messages") or []
        info(f"{len(msgs)} messages in Saved Messages")
        for c in REPORT["cases"]:
            found = next((m for m in msgs if c["marker"] in (m.get("message") or "")), None)
            c["delivered"] = bool(found)
            c["stored"] = found
            if found:
                win(f"DELIVERED  {c['label']:<20} (recipient: {c['recipient']})")
                REPORT["wins"].append(c)
            else:
                bad(f"dropped    {c['label']:<20}")

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

    _verdict()
    _save(outdir)
    return 0


def _verdict() -> None:
    banner("VERDICT")
    v = REPORT["verdict"]
    wins = [w for w in REPORT["wins"] if w["label"] != "plain-apk-name"]
    # best = delivered AND recipient keeps app.apk (no rename), ideally no pass
    ideal = [w for w in wins if w.get("inner") == "app.apk"]
    renamed = [w for w in wins if w.get("inner") != "app.apk"]
    if ideal:
        win("BEST: an encrypted zip delivered AND keeps the name app.apk inside:")
        for w in ideal:
            v.append(f"{w['label']}: recipient does -> {w['recipient']}")
        v.append("This is the one to wire in: Eitaa could not see app.apk inside "
                 "the encrypted zip, but the recipient opens it once (with the "
                 f"password '{PASSWORD}') and taps app.apk. For elderly users, a "
                 "fixed password you tell them once is far easier than a rename.")
    elif renamed:
        win("An encrypted zip delivered, but the apk is under a NON-apk name:")
        for w in renamed:
            v.append(f"{w['label']}: recipient does -> {w['recipient']}")
        v.append("This passes Eitaa, but the recipient must either tap-install a "
                 "renamed file (many Android file managers allow it) or rename to "
                 ".apk first. Usable, with one extra step. If an app.apk-named "
                 "variant above had delivered it would be better; it did not this "
                 "run.")
    else:
        v.append("No encrypted zip delivered. Checking the 'names Eitaa sees' "
                 "column in STEP 2 explains why: if app.apk was still visible, "
                 "the encryption did not hide the NAME (ZipCrypto/standard AES "
                 "leave names cleartext) and Eitaa dropped it on the name; if the "
                 "name WAS hidden and it still dropped, Eitaa also flags "
                 "encrypted/unreadable zips. Either way the clean one-tap answer "
                 "is the PDF-with-a-download-link.")
    # always surface what Eitaa could see, it is the key diagnostic
    v.append("Name-visibility recap (what Eitaa's scanner reads):")
    for c in REPORT["cases"]:
        v.append(f"  {c['label']}: sees {c.get('names_eitaa_sees')} | "
                 f"delivered={c.get('delivered')}")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)


def _save(outdir) -> None:
    try:
        (outdir / "report.json").write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\n  full report: {outdir / 'report.json'}", flush=True)
        print("  probe messages tagged ZENC-* in Saved Messages.", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Encrypted-zip variants carrying an apk vs Eitaa's filter. "
                    "Own Saved Messages only.")
    p.add_argument("--account", required=True)
    p.add_argument("--size-kb", type=float, default=12)
    p.add_argument("--real-apk", default=None)
    p.add_argument("--wait", type=float, default=20)
    p.add_argument("--settle", type=float, default=18)
    p.add_argument("--case-timeout", type=float, default=120)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
