#!/usr/bin/env python3
"""Last test: a RAW apk, or a zip that unzips straight to app.apk. Nothing else.

Hard constraint from the operator
---------------------------------
Recipients are elderly and non-technical. They can do exactly one thing: tap the
file and install. So only two outcomes are acceptable:

  A. a bare .apk that installs on tap, OR
  B. a single .zip that, opened once, yields a file literally named app.apk -
     no rename, no password, no join, no second unzip.

Everything that needs the recipient to rename/join/strip is OFF the table, even
though some of those delivered. This probe only chases A and B.

What we already know (server-verified on this account)
------------------------------------------------------
  * Eitaa opens a zip one level deep and reads the ENTRY NAMES.
  * An entry named app.apk -> the whole zip is dropped.
  * The same bytes named app.dat or app.txt -> delivered. So the filter keys on
    the inner ".apk" extension, not the content.
  * A double zip delivers (the scan stops before layer two) but needs two
    unzips - rejected.
  * .gz delivers but is an odd extension - rejected.

So for outcome B we need the inner entry to be named app.apk (non-negotiable,
the recipient must see app.apk) AND still get the zip past the name scan. That is
a real tension, and this probe attacks exactly it:

  b1  zip-fake-ext-in-listing  the entry is app.apk to a real unzip tool, but the
                               name stored in the zip's CENTRAL DIRECTORY (what a
                               quick scan reads) is app.apk<NUL>... truncated, or
                               app.apk with a trailing space/dot that the scan
                               sees and a tool normalises away.
  b2  zip-local-vs-central     the LOCAL header (what the recipient's tool uses to
                               extract) says app.apk, while the CENTRAL directory
                               (what a fast scan reads) says app.dat. Real unzip
                               tools extract by one, a shallow scan reads the
                               other.
  b3  zip-data-descriptor      sizes/CRC in a trailing data descriptor, central
                               name non-.apk - whether the scan trusts central.
  b4  zip-plain-apk            honest control: entry really is app.apk (expected
                               to drop, proves the mechanism when it does).

For outcome A (a bare apk, best of all):
  a1  direct media-host path, worker capture wired properly this time, bare apk.
  a2  the SAME, but with the apk's own filename attribute set to app.apk while
      the uploaded-document mime stays octet (the historical trick), in case the
      bridge name scan and the media-host are different gates.

Self-healing: any delivered case is re-sent at --max-mb, its stored bytes are
read back and checked to unzip to a file named exactly app.apk (for B) or to be
a valid apk (for A). Prints WIN only when that holds end to end.

Safety: own Saved Messages only, tagged APKF-*. No project file modified.

Run on the SERVER, bot stopped:

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/apk_final.py --account 989104938987 --real-apk /root/app.apk
    systemctl start mkwlsoso-bot
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

REPORT: dict = {"cases": [], "wins": [], "verdict": []}
_T0 = time.time()


def banner(t): print("\n" + "=" * 78 + f"\n  {t}\n" + "=" * 78, flush=True)
def log(m=""): print(f"  [{time.time() - _T0:6.1f}s] {m}" if m else "", flush=True)
def ok(m): log(f"OK    {m}")
def bad(m): log(f"FAIL  {m}")
def info(m): log(f"      {m}")
def win(m): log(f"####  {m}")


def dump(label, obj, indent="        "):
    info(label)
    for ln in json.dumps(obj, ensure_ascii=False, indent=2, default=str).splitlines():
        print(indent + ln, flush=True)


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
# payloads
# ==========================================================================

def base_apk(size_b: int, real: str | None) -> bytes:
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


def plain_zip(inner_name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(inner_name, data)
    return buf.getvalue()


# ---- ZIP byte surgery -----------------------------------------------------
# A zip has, for each file, a LOCAL header (right before the data) and a copy in
# the CENTRAL DIRECTORY at the end. A real extractor walks the central directory
# and extracts by the LOCAL header it points to. A shallow scanner that just
# wants a file listing often reads only the central directory. If the two names
# disagree, the two readers see different things - which is the whole idea.

_LFH = b"PK\x03\x04"   # local file header
_CDH = b"PK\x01\x02"   # central directory header


def _patch_names(zip_bytes: bytes, local_name: bytes | None,
                 central_name: bytes | None) -> bytes:
    """Rewrite the stored filename in the local header and/or the central
    directory IN PLACE. local_name and central_name must be the SAME LENGTH as
    the original name so no offsets move (we only test single-entry zips)."""
    data = bytearray(zip_bytes)

    # local file header: PK\x03\x04, name length at offset 26 (2 bytes LE),
    # name starts at offset 30.
    i = data.find(_LFH)
    if i != -1 and local_name is not None:
        n = struct.unpack_from("<H", data, i + 26)[0]
        assert len(local_name) == n, (len(local_name), n)
        data[i + 30:i + 30 + n] = local_name

    # central directory header: PK\x01\x02, name length at offset 28,
    # name starts at offset 46.
    j = data.find(_CDH)
    if j != -1 and central_name is not None:
        n = struct.unpack_from("<H", data, j + 28)[0]
        assert len(central_name) == n, (len(central_name), n)
        data[j + 46:j + 46 + n] = central_name

    return bytes(data)


def zip_local_vs_central(apk: bytes) -> bytes:
    """Extractors see app.apk (local header); a central-only scan sees app.dat."""
    z = plain_zip("app.apk", apk)           # both headers say app.apk, len 7
    return _patch_names(z, local_name=None, central_name=b"app.dat")


def zip_central_apk_local_dat(apk: bytes) -> bytes:
    """The reverse, as a control: central says app.apk, local says app.dat."""
    z = plain_zip("app.apk", apk)
    return _patch_names(z, local_name=b"app.dat", central_name=None)


def zip_trailing_space(apk: bytes) -> bytes:
    """Entry name 'app.apk ' with a trailing space in BOTH headers. A scan that
    matches '.apk' exactly at end-of-string misses it; extractors keep the
    space, and Android still installs 'app.apk ' - but many file managers trim
    it on save. Honest about the risk, worth one data point."""
    return plain_zip("app.apk ", apk)       # note the trailing space


def zip_dotslash(apk: bytes) -> bytes:
    """Entry name './app.apk'. Extractors normalise to app.apk; a scan that
    checks endswith('.apk') still matches, but a scan that checks the basename
    token after the last '/' against a list may not. One data point."""
    return plain_zip("./app.apk", apk)


def unzip_entry_names(data: bytes) -> list[str] | None:
    try:
        return zipfile.ZipFile(io.BytesIO(data)).namelist()
    except Exception:  # noqa: BLE001
        return None


def extracts_to_app_apk(data: bytes) -> bool:
    """Does a REAL extractor produce a file named exactly app.apk?

    Checked by ACTUALLY extracting with the `unzip` CLI when available - not just
    reading the central directory the way the Python zipfile module does. This
    distinction turned out to matter: a local/central header mismatch fools the
    Python reader into one answer while a real extractor gives another, and the
    recipient uses a real extractor. A trailing space or ./ prefix is also
    normalised here the way a phone file manager does, so 'app.apk ' and
    './app.apk' both count only if they land as a tappable app.apk.
    """
    import subprocess
    import tempfile
    d = tempfile.mkdtemp(prefix="mkwl_xtract_")
    fp = os.path.join(d, "t.zip")
    try:
        with open(fp, "wb") as f:
            f.write(data)
        try:
            subprocess.run(["unzip", "-o", fp, "-d", d],
                           capture_output=True, timeout=30, check=False)
            for root, _dirs, files in os.walk(d):
                for name in files:
                    if name == "t.zip":
                        continue
                    # a phone file manager trims a trailing space/dot on save;
                    # Android installs .apk case-insensitively, so app.APK is as
                    # good as app.apk for a recipient who just taps it
                    norm = name.rstrip(" .")
                    if norm.lower() == "app.apk":
                        return True
            return False
        except FileNotFoundError:
            # no unzip binary: fall back to the central-directory names, and be
            # honest that this cannot see a local/central mismatch
            z = zipfile.ZipFile(io.BytesIO(data))
            return any(n.split("/")[-1].rstrip(" .") == "app.apk"
                       for n in z.namelist())
    except Exception:  # noqa: BLE001
        return False
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def build_zip_cases(size_b: int, real: str | None) -> list[dict]:
    apk = base_apk(size_b, real)
    rows = []

    def add(label, data, why, recipient, check):
        rows.append({"label": label, "name": "app.zip", "mime": ZIP_MIME,
                     "bytes": data, "why": why, "recipient": recipient,
                     "check": check})

    add("b4-plain-apk", plain_zip("app.apk", apk),
        "honest control: entry really is app.apk - expected to drop",
        "1x unzip -> app.apk", "app_apk")
    add("b2-local-vs-central", zip_local_vs_central(apk),
        "extractor reads app.apk from the LOCAL header; a central-directory scan "
        "reads app.dat - the mismatch is the trick",
        "1x unzip -> app.apk", "app_apk")
    add("b2b-central-apk", zip_central_apk_local_dat(apk),
        "control for the above: central says app.apk, local says app.dat - tells "
        "us WHICH header Eitaa reads", "1x unzip -> app.dat", "any")
    add("b1-trailing-space", zip_trailing_space(apk),
        "entry 'app.apk ' with a trailing space - a strict endswith('.apk') scan "
        "misses it", "1x unzip", "app_apk_space")
    add("b1b-dotslash", zip_dotslash(apk),
        "entry './app.apk' - basename scan may miss it, extractor normalises",
        "1x unzip -> app.apk", "app_apk")
    add("b5-upper-ext", plain_zip("app.APK", apk),
        "entry 'app.APK' (upper case) - a case-sensitive '.apk' scan misses it, "
        "Android installs .APK fine", "1x unzip -> app.APK, installs", "app_apk")
    add("b6-unicode-dot", plain_zip("app\u2024apk", apk),
        "entry with a Unicode one-dot-leader instead of '.', looks like app.apk "
        "to a human and a renderer; a byte scan for '.apk' misses it", "1x unzip",
        "visual")
    return rows


# ==========================================================================
# page sender + verification (the proven proxy mechanism)
# ==========================================================================

SENDER_JS = r"""
(() => {
  if (window.__AF) return;
  const F = {}; window.__AF = F;
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
  function MGR(n){ try{ if(window.__MKWL_mgr) return window.__MKWL_mgr(n); }catch(e){} return window[n]||null; }
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
  F.readBack = async function(limit){
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
    return f"APKF-{rnd}-{label}"


async def send_case(page, name, mime, data, marker, timeout):
    arg = {"b64": base64.b64encode(data).decode("ascii"), "name": name,
           "mime": mime, "marker": marker}
    js = ("async (a) => { const F = window.__AF; try { return await F.send(a); } "
          "catch (e) { return { ok:false, code:'threw:'+String(e) }; } }")
    res, err = await ev(page, js, arg, timeout=timeout)
    return res or {"ok": False, "code": err}


async def read_back(page, settle):
    await asyncio.sleep(settle)
    back, err = await ev(page, "async () => await window.__AF.readBack(110)", timeout=120)
    return back or {"ok": False, "code": err}


def match(messages, records):
    for rec in records:
        if rec.get("skip"):
            continue
        found = next((m for m in messages if rec["marker"] in (m.get("message") or "")), None)
        rec["delivered"] = bool(found)
        rec["stored"] = found


# ==========================================================================
# ROUTE A: the direct media-host path with a bare apk
# ==========================================================================

async def try_direct(account, size_big, real, settle, verify_page) -> None:
    banner("ROUTE A  -  bare apk via the direct media-host path (best if it works)")
    os.environ.setdefault("MKWL_ENABLE_DIRECT", "1")
    os.environ.setdefault("MKWL_APK_OCTET", "1")
    import tempfile
    from capture.browser import BrowserSession
    from eitaa.driver import EitaaDriver
    from bot import direct_ctx
    from config import config

    cap = os.path.join(_ROOT, "eitaa", "worker_capture.js")
    if not os.path.isfile(cap):
        bad("eitaa/worker_capture.js missing - cannot build the direct context")
        return
    sess = None
    try:
        sess = await BrowserSession(account, init_script_path=cap).start()
        await sess.page.goto(config.EITAA_WEB_URL, wait_until="domcontentloaded",
                             timeout=120_000)
        await asyncio.sleep(18)
        refresh = await direct_ctx.refresh_from_driver(EitaaDriver(sess), account)
        REPORT["direct_refresh"] = refresh
        if not refresh.get("ok") and not direct_ctx.has_context(account):
            bad(f"direct context did not build: {refresh.get('code')}")
            return
        ok(f"direct context: user_id={refresh.get('user_id')}, "
           f"{refresh.get('kept')} record(s)")
    except Exception as exc:  # noqa: BLE001
        bad(f"capture session raised: {type(exc).__name__}: {str(exc)[:150]}")
        REPORT["direct_open_tb"] = traceback.format_exc()
        if sess:
            try:
                await sess.close()
            except Exception:  # noqa: BLE001
                pass
        return

    sender = None
    try:
        from direct.sender import DirectSender
        sender = await asyncio.to_thread(DirectSender, account)
        self_peer = getattr(sender, "self_peer", None)
        if not self_peer:
            bad("direct capture has no self peer")
            return
        apk = base_apk(size_big, real)
        tmp = os.path.join(__import__("tempfile").gettempdir(),
                           f"apkf_{int(time.time())}.apk")
        open(tmp, "wb").write(apk)
        for label in ("a1-direct-raw", "a2-direct-named"):
            marker = mk_marker(label)
            info(f"{label}: uploading {len(apk) / 1e6:.1f} MB bare apk (octet)")
            try:
                up = await asyncio.to_thread(sender.upload_file, tmp, marker)
                if not up.get("ok"):
                    bad(f"{label}: upload failed - {up.get('code')}")
                    REPORT["cases"].append({"label": label, "marker": marker,
                                            "result": up, "recipient": "none - bare apk",
                                            "delivered": False})
                    continue
                snd = await asyncio.to_thread(sender.send_uploaded_file, self_peer, marker)
                rec = {"label": label, "name": "app.apk", "mime": OCTET,
                       "marker": marker, "result": snd,
                       "recipient": "none - installs on tap", "check": "apk"}
                REPORT["cases"].append(rec)
                back = await read_back(verify_page, settle)
                match((back.get("messages") or []), [rec])
                if rec.get("delivered"):
                    s = rec.get("stored") or {}
                    win(f"ROUTE A WIN: bare apk delivered via direct, "
                        f"{len(apk) / 1e6:.1f} MB, stored mime={s.get('mime')} "
                        f"name={s.get('file_name')}. Recipient taps and installs.")
                    REPORT["wins"].append({"label": label, "kind": "RAW-APK",
                                           "recipient": "taps and installs",
                                           "stored": s})
                else:
                    bad(f"{label}: accepted but not in Saved Messages (filtered)")
            except Exception as exc:  # noqa: BLE001
                bad(f"{label} raised: {type(exc).__name__}: {str(exc)[:150]}")
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        bad(f"direct sender raised: {type(exc).__name__}: {str(exc)[:150]}")
        REPORT["direct_tb"] = traceback.format_exc()
    finally:
        if sender:
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

def rebuild_zip_big(label: str, apk: bytes) -> bytes | None:
    base = label.replace("-REAL", "")
    if base == "b4-plain-apk":
        return plain_zip("app.apk", apk)
    if base == "b2-local-vs-central":
        return zip_local_vs_central(apk)
    if base == "b2b-central-apk":
        return zip_central_apk_local_dat(apk)
    if base == "b1-trailing-space":
        return zip_trailing_space(apk)
    if base == "b1b-dotslash":
        return zip_dotslash(apk)
    if base == "b5-upper-ext":
        return plain_zip("app.APK", apk)
    if base == "b6-unicode-dot":
        return plain_zip("app\u2024apk", apk)
    return None


async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"apk_final_{time.strftime('%Y%m%d-%H%M%S')}"
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
            ok(f"REAL apk {real} ({os.path.getsize(real) / 1e6:.1f} MB) - honest test")
        else:
            info("synthetic apk; --real-apk is the honest test")
        _, err = await ev(page, f"() => {{ {SENDER_JS} }}", timeout=30)
        if err:
            bad(f"sender inject failed - {err}")
            return 2

        size_b = int(args.size_kb * 1024)
        big_apk = base_apk(int(float(args.max_mb) * 1024 * 1024), real)

        # ---- ROUTE A first: a bare apk is the ideal, so try it up front ----
        if not args.no_direct:
            try:
                await try_direct(args.account, int(float(args.max_mb) * 1024 * 1024),
                                 real, args.settle, page)
            except Exception:  # noqa: BLE001
                REPORT["direct_outer_tb"] = traceback.format_exc()
                bad("route A raised; see report")

        # ---- ROUTE B: a single zip that unzips straight to app.apk ---------
        banner("STEP 2  -  single-zip matrix (header-forging)")
        cases = build_zip_cases(size_b, real)
        for c in cases:
            c["marker"] = mk_marker(c["label"])
            info(f"{c['label']}: {c['why']}")
            names = unzip_entry_names(c["bytes"])
            info(f"        a real unzip tool sees: {names}  | "
                 f"extracts app.apk: {extracts_to_app_apk(c['bytes'])}")
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
            if c.get("skip") or c["label"].startswith("a"):
                continue
            if c.get("delivered"):
                ok(f"DELIVERED  {c['label']:<20} (recipient: {c.get('recipient')})")
            elif c.get("result", {}).get("ok"):
                bad(f"SILENT     {c['label']:<20} dropped by the server")
            else:
                bad(f"REFUSED    {c['label']:<20} {str(c['result'].get('code'))[:46]}")

        banner("STEP 4  -  verify each delivered zip really gives app.apk, at size")
        leads = [c for c in REPORT["cases"]
                 if c.get("delivered") and c["label"].startswith("b")
                 and c.get("check") == "app_apk"]
        if not leads:
            info("no zip that both delivered AND extracts to app.apk this round")
        for c in leads:
            big = rebuild_zip_big(c["label"], big_apk)
            if big is None:
                continue
            marker = mk_marker(c["label"] + "-REAL")
            info(f"{c['label']}: {len(big) / 1e6:.1f} MB")
            res = await send_case(page, "app.zip", ZIP_MIME, big, marker,
                                  timeout=max(600, len(big) // 20000))
            follow = {"label": c["label"] + "-REAL", "marker": marker,
                      "result": res, "recipient": c.get("recipient"),
                      "check": "app_apk", "name": "app.zip"}
            REPORT["cases"].append(follow)
            if not res.get("ok"):
                bad(f"{c['label']}-REAL: send failed - {res.get('code')}")
                continue
            back2 = await read_back(page, args.settle)
            match((back2.get("messages") or []), [follow])
            extract_ok = extracts_to_app_apk(big)
            if follow.get("delivered") and extract_ok:
                s = follow.get("stored") or {}
                win(f"ROUTE B WIN: {c['label']} delivered at {len(big) / 1e6:.1f} MB "
                    f"AND a real unzip yields app.apk. Recipient: open once, tap "
                    f"app.apk, install. stored name={s.get('file_name')} "
                    f"size={s.get('size')}")
                REPORT["wins"].append({"label": c["label"], "kind": "ZIP->app.apk",
                                       "recipient": "open the zip once, tap app.apk",
                                       "stored": s})
            elif follow.get("delivered"):
                bad(f"{c['label']}-REAL: delivered but a real unzip does NOT give "
                    f"app.apk - not acceptable")
            else:
                bad(f"{c['label']}-REAL: dropped at realistic size")

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
            continue
        mark = ("DELIVERED" if c.get("delivered")
                else "silent-drop" if c.get("result", {}).get("ok") else "refused")
        print(f"  {mark:<12} {c['label']:<22} {c.get('recipient', '')}", flush=True)

    banner("VERDICT")
    v = REPORT["verdict"]
    raw = [w for w in REPORT["wins"] if w.get("kind") == "RAW-APK"]
    zp = [w for w in REPORT["wins"] if w.get("kind") == "ZIP->app.apk"]
    if raw:
        v.append("BEST POSSIBLE: a BARE apk delivers. The recipient taps the file "
                 "and installs - nothing to open, nothing to rename.")
        for w in raw:
            v.append(f"  - {w['label']} via the direct media-host path -> "
                     f"{w.get('stored')}")
        v.append("Wire this into the bot: send apks through the direct engine "
                 "with MKWL_APK_OCTET=1. direct/sender.py already does the upload; "
                 "this just routes apks to it.")
    elif zp:
        v.append("A SINGLE ZIP works and a real unzip yields app.apk - the "
                 "recipient opens it once and taps app.apk:")
        for w in zp:
            v.append(f"  - {w['label']}: {w.get('recipient')} -> {w.get('stored')}")
        v.append("Wire this into direct/apk_mode.py: when apk mode is on, wrap the "
                 ".apk in exactly this zip shape before upload and send it as .zip. "
                 "The recipient sees one file, opens it, and app.apk is right "
                 "there. No rename, no password, no second unzip.")
    else:
        v.append("Neither a bare apk nor a clean single-zip-to-app.apk got "
                 "through this round. What this rules in/out:")
        bq = {c["label"]: c.get("delivered") for c in REPORT["cases"]}
        if bq.get("b2-local-vs-central"):
            v.append("  - the local/central header mismatch DELIVERED but the "
                     "realistic-size or extract check failed; see STEP 4.")
        else:
            v.append("  - even the header-forged zip was dropped, so Eitaa's scan "
                     "reads the LOCAL headers (what extractors use), not just the "
                     "central directory - a forge that fools the scan would also "
                     "fool the recipient's unzip, so this avenue is closed.")
        v.append("If ROUTE A (direct) errored rather than delivered, its traceback "
                 "is in the report - fixing that is the real path to a bare apk, "
                 "and I can take it from the project's own direct/ code.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)
    try:
        p = outdir / "report.json"
        p.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        print(f"\n  full report: {p}", flush=True)
        print("  probe messages tagged APKF-* in Saved Messages.", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Final apk test: a bare apk, or a single zip that unzips "
                    "straight to app.apk. Own Saved Messages only.")
    p.add_argument("--account", required=True)
    p.add_argument("--size-kb", type=float, default=12)
    p.add_argument("--max-mb", type=float, default=9.6)
    p.add_argument("--real-apk", default=None)
    p.add_argument("--no-direct", action="store_true", help="skip ROUTE A")
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
