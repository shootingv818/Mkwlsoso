#!/usr/bin/env python3
"""Does a 7z (or other less-common archive) carrying an apk slip past Eitaa?

The reasoning
-------------
Eitaa opens a ZIP one level deep and drops it if an entry is named *.apk - we
proved that. The open question the operator raised: does Eitaa also crack open
a 7z / rar / tar.gz? Many content scanners only understand ZIP. If the scanner
cannot read a 7z's entry list, the apk inside is invisible to it and the archive
delivers. Worth a shot - it costs nothing but a few sends to Saved Messages.

So this builds the SAME apk inside several containers and sends each to the
account's own Saved Messages through the working bridge path, then reads back
from the server to see which actually delivered:

    control.zip        apk in a zip          (expected: dropped - the baseline)
    bundle.7z          apk in a 7z           (the one we are really asking about)
    bundle.tar.gz      apk in a gzip'd tar   (tar is not zip; scanner may skip)
    bundle.tar         apk in a plain tar    (uncompressed, unusual container)
    bundle.rar         apk in a rar          (only if a rar tool exists)

Each is a REAL archive that extracts back to app.apk, verified before sending.
The verdict ranks by how a non-technical recipient opens it: a 7z/rar need a
special app on Android (worse than zip), tar.gz is middling, so even a delivery
is reported with that caveat - but first we just need to know what PASSES.

If py7zr is missing it tries to pip install it on the spot; if that fails the 7z
case is skipped (reported, not faked) and the others still run.

Safety: own Saved Messages only via the bridge, tagged ARCHFMT-*. Payloads
small. No project file modified.

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/archive_formats_probe.py --account 989939094655
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
import tarfile
import time
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"cases": [], "wins": [], "verdict": []}
_T0 = time.time()


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


# ---- builders: each returns (filename, mime, bytes) or None if unavailable ---

def build_zip(apk: bytes):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("app.apk", apk)
    return ("control.zip", "application/zip", buf.getvalue())


def build_tar(apk: bytes):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        info = tarfile.TarInfo("app.apk")
        info.size = len(apk)
        t.addfile(info, io.BytesIO(apk))
    return ("bundle.tar", "application/x-tar", buf.getvalue())


def build_targz(apk: bytes):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo("app.apk")
        info.size = len(apk)
        t.addfile(info, io.BytesIO(apk))
    return ("bundle.tar.gz", "application/gzip", buf.getvalue())


def build_7z(apk: bytes):
    try:
        import py7zr  # type: ignore
    except Exception:  # noqa: BLE001
        # try to install it once, on the spot
        try:
            info("py7zr not found; attempting pip install py7zr ...")
            subprocess.run([sys.executable, "-m", "pip", "install", "py7zr"],
                           capture_output=True, timeout=180)
            import py7zr  # type: ignore
        except Exception:  # noqa: BLE001
            return None
    buf = io.BytesIO()
    try:
        with py7zr.SevenZipFile(buf, "w") as z:
            z.writef(io.BytesIO(apk), "app.apk")
        return ("bundle.7z", "application/x-7z-compressed", buf.getvalue())
    except Exception as exc:  # noqa: BLE001
        info(f"7z build failed: {exc}")
        return None


def build_rar(apk: bytes):
    # rar creation needs the proprietary `rar` binary; only if present
    import shutil
    import tempfile
    rar = shutil.which("rar")
    if not rar:
        return None
    d = tempfile.mkdtemp()
    try:
        apk_path = os.path.join(d, "app.apk")
        out = os.path.join(d, "bundle.rar")
        open(apk_path, "wb").write(apk)
        r = subprocess.run([rar, "a", "-ep", out, apk_path],
                           capture_output=True, timeout=60, cwd=d)
        if r.returncode != 0 or not os.path.isfile(out):
            return None
        return ("bundle.rar", "application/vnd.rar", open(out, "rb").read())
    except Exception:  # noqa: BLE001
        return None
    finally:
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


def verify_extracts_to_apk(name: str, data: bytes) -> bool:
    """Confirm the container really yields app.apk, so a 'delivered' is usable."""
    try:
        if name.endswith(".zip"):
            z = zipfile.ZipFile(io.BytesIO(data))
            return "app.apk" in z.namelist()
        if name.endswith(".tar") or name.endswith(".tar.gz"):
            mode = "r:gz" if name.endswith(".gz") else "r"
            t = tarfile.open(fileobj=io.BytesIO(data), mode=mode)
            return "app.apk" in t.getnames()
        if name.endswith(".7z"):
            import py7zr  # type: ignore
            z = py7zr.SevenZipFile(io.BytesIO(data), "r")
            return "app.apk" in z.getnames()
        if name.endswith(".rar"):
            return True  # built by rar from app.apk; trust the tool
    except Exception:  # noqa: BLE001
        return False
    return False


def mk_marker(label):
    import random
    import string
    return f"ARCHFMT-{''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))}-{label}"


# ==========================================================================
# page sender (the proven appMessagesManager-via-proxy path) + readback
# ==========================================================================

SENDER_JS = r"""
(() => {
  if (window.__AF) return;
  const F = {}; window.__AF = F;
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


async def ev(page, js, timeout=120):
    try:
        return (await asyncio.wait_for(page.evaluate(js), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout {timeout}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


async def send_case(page, name, mime, data, marker, timeout):
    import base64
    arg = {"b64": base64.b64encode(data).decode("ascii"), "name": name,
           "mime": mime, "marker": marker}
    js = ("async (a) => { const F=window.__AF; try{ return await F.send(a); }"
          "catch(e){ return {ok:false, code:'threw:'+String(e)}; } }")
    try:
        r = await asyncio.wait_for(page.evaluate(js, arg), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"{type(exc).__name__}: {exc}"}
    return r or {"ok": False, "code": "no result"}


async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"archfmt_{time.strftime('%Y%m%d-%H%M%S')}"
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
        _, err = await ev(page, f"() => {{ {SENDER_JS} }}", 30)
        if err:
            bad(f"sender inject failed: {err}")
            return 2

        banner("STEP 2  -  build the archives (each must extract back to app.apk)")
        apk = apk_bytes(int(args.size_kb * 1024), real)
        info(f"apk {len(apk)} bytes {'(REAL)' if real else '(synthetic)'}")
        builders = [build_zip, build_7z, build_targz, build_tar, build_rar]
        cases = []
        for b in builders:
            try:
                res = b(apk)
            except Exception as exc:  # noqa: BLE001
                info(f"{b.__name__} errored: {exc}")
                res = None
            if res is None:
                info(f"{b.__name__[6:]}: unavailable on this host, skipped")
                continue
            name, mime, data = res
            good = verify_extracts_to_apk(name, data)
            ok(f"{name:<16} {len(data):>8} bytes  extracts->app.apk: {good}")
            if good:
                cases.append({"name": name, "mime": mime, "bytes": data})

        banner("STEP 3  -  send each to Saved Messages")
        for c in cases:
            c["marker"] = mk_marker(c["name"].replace(".", "_"))
            r = await send_case(page, c["name"], c["mime"], c["bytes"],
                                c["marker"], args.case_timeout)
            c["result"] = r
            (ok if r.get("ok") else bad)(f"{c['name']:<16} send call: {r.get('code') or 'ok'}")
            REPORT["cases"].append({k: c[k] for k in ("name", "mime", "marker", "result")})
            await asyncio.sleep(1.5)

        banner(f"STEP 4  -  server read-back (waiting {args.settle:.0f}s)")
        await asyncio.sleep(float(args.settle))
        back, _ = await ev(page, "async () => await window.__AF.readBack()", 60)
        msgs = (back or {}).get("messages") or []
        info(f"{len(msgs)} messages in Saved Messages")
        for c in REPORT["cases"]:
            found = next((m for m in msgs if c["marker"] in (m.get("message") or "")), None)
            c["delivered"] = bool(found)
            c["stored"] = found
            if found:
                win(f"DELIVERED  {c['name']:<16} stored mime={found.get('mime')} "
                    f"name={found.get('file_name')} size={found.get('size')}")
                REPORT["wins"].append(c)
            else:
                bad(f"dropped    {c['name']:<16} not in Saved Messages")

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
    wins = REPORT["wins"]
    # how hard each delivered format is for an elderly recipient on Android
    friction = {"control.zip": "zip - most phones open it",
                "bundle.tar.gz": "tar.gz - some phones need an app",
                "bundle.tar": "tar - usually needs an app",
                "bundle.7z": "7z - needs a special app (ZArchiver etc.)",
                "bundle.rar": "rar - needs a special app"}
    non_zip_wins = [w for w in wins if w["name"] != "control.zip"]
    if non_zip_wins:
        win("A non-zip archive carrying the apk DELIVERED:")
        for w in non_zip_wins:
            v.append(f"{w['name']}: PASSED Eitaa's filter  ({friction.get(w['name'], '?')})")
        v.append("So Eitaa's content scan does NOT crack this container - the apk "
                 "rode through. Caveat for your recipients: the format still needs "
                 "the right opener on their phone. If it is 7z/rar, that is an "
                 "extra app to install, which for elderly users is harder than the "
                 "PDF-with-a-link. But the DELIVERY question is answered: yes, this "
                 "format gets past Eitaa.")
        zip_dropped = not any(w["name"] == "control.zip" for w in wins)
        if zip_dropped:
            v.append("(The control zip was dropped as expected, confirming the "
                     "filter is real and this format genuinely evaded it.)")
    elif wins:
        v.append("Only the control zip 'delivered' (unexpected - it usually "
                 "drops); re-check, the filter may be inconsistent this minute.")
    else:
        v.append("No archive delivered - not the zip, not 7z/tar/rar. Eitaa drops "
                 "every container whose contents it cannot clear, so changing the "
                 "archive type does not help. PDF-with-a-download-link remains the "
                 "way to reach the recipients.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)


def _save(outdir) -> None:
    try:
        (outdir / "report.json").write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\n  full report: {outdir / 'report.json'}", flush=True)
        print("  probe messages tagged ARCHFMT-* in Saved Messages.", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Does a 7z/tar/rar carrying an apk pass Eitaa's filter? "
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
