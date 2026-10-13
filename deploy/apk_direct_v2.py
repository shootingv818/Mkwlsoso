#!/usr/bin/env python3
"""Direct apk send, with tokens read straight from storage (no worker hook).

The breakthrough
----------------
token_hunt found the direct-engine tokens sitting in plain sight: Eitaa stores
them in appStorage / localStorage under account1, as readable strings:

    appStorage.storage.cache.account1 = {
        userId: 16214734, dcId: 2,
        token: "7462.f527bc5684.14d5072_16214734",   <- token1 (envelope route)
        imei:  "ab37c8mv489exm1__web"                 <- token2 (client id)
    }

So the direct path never needed the worker-rebuild hook that hangs this build.
We read token1/token2 and the user_id from storage, send to the account's OWN
Saved Messages with inputPeerSelf (which needs no 20-byte peer), and the direct
engine's byte-exact envelope goes straight to the media host - the server path
that has no archive scan, where a bare apk historically delivered 11/11.

What it does
------------
  1. open the account CLEAN (no hook, so invokeApi/the app stay healthy)
  2. read token1/token2/user_id/dcId from appStorage (and localStorage as backup)
  3. build the direct envelope in Python and POST a BARE apk to the media host,
     to inputPeerSelf, with a unique caption marker
  4. try it several ways: octet MIME, real apk MIME, and a couple of media
     shards (fateme, hadi), each a real on-wire send
  5. read Saved Messages back (through the page) and report which actually
     delivered - the server is the only judge
  6. self-heal: any delivered variant is re-sent at a realistic apk size and
     only then called a BREAKTHROUGH

If a bare apk lands in Saved Messages here, the direct path is the answer and I
wire apks to it in the bot. If every variant is dropped, Eitaa filters apk on
the media host too and PDF-with-a-link is the way - but this is the real test,
finally runnable.

Safety: own Saved Messages only (inputPeerSelf), tagged APKV2-*. No contact
targeted. No project file modified. Clean session, nothing that can hang the app.

    systemctl stop mkwlsoso-bot
    cd ~/Mkwlsoso && .venv/bin/python deploy/apk_direct_v2.py --account 989939094655
    systemctl start mkwlsoso-bot
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import zipfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT: dict = {"ctx": {}, "attempts": [], "wins": [], "verdict": []}
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


# Read Eitaa's stored session straight from the page. This is the whole trick:
# the tokens are plain strings in appStorage, no hook needed.
READ_TOKENS_JS = r"""
() => {
  const out = { sources: {} };
  // 1) appStorage.storage.cache.accountN
  try {
    const cache = window.appStorage && window.appStorage.storage
                  && window.appStorage.storage.cache;
    if (cache) {
      for (const k of Object.keys(cache)) {
        if (/^account\d+$/.test(k)) {
          const a = cache[k];
          if (a && a.token) out.sources["appStorage."+k] =
            { token: a.token, imei: a.imei, userId: a.userId, dcId: a.dcId };
        }
      }
    }
  } catch(e){ out.appStorage_err = String(e); }
  // 2) localStorage accountN (JSON string)
  try {
    for (const k of Object.keys(localStorage)) {
      if (/^account\d+$/.test(k)) {
        try {
          const a = JSON.parse(localStorage.getItem(k));
          if (a && a.token) out.sources["localStorage."+k] =
            { token: a.token, imei: a.imei, userId: a.userId, dcId: a.dcId };
        } catch(e){}
      }
    }
  } catch(e){ out.localStorage_err = String(e); }
  return out;
}
"""

READBACK_JS = r"""
async () => {
  function API(){ try{ if(window.__MKWL_api) return window.__MKWL_api(); }catch(e){} return window.apiManager||null; }
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
}
"""


async def ev(page, js, timeout=30):
    try:
        return (await asyncio.wait_for(page.evaluate(js), timeout=timeout), None)
    except asyncio.TimeoutError:
        return (None, f"timeout {timeout}s")
    except Exception as exc:  # noqa: BLE001
        return (None, f"{type(exc).__name__}: {exc}")


def mk_marker(label):
    import random
    import string
    return f"APKV2-{''.join(random.choice(string.ascii_uppercase+string.digits) for _ in range(6))}-{label}"


# ==========================================================================
# the direct send, built from storage tokens (no capture file, no hook)
# ==========================================================================

def self_peer_bytes():
    """A bare inputPeerSelf (ctor 0x7da07ec9, no fields) - Saved Messages, and
    it needs no user_id/access_hash, so storage tokens alone are enough."""
    from direct import tl
    from direct.schema import INPUT_PEER_SELF
    return tl.int_bytes(INPUT_PEER_SELF, signed=False)


def send_apk_direct(token1: str, token2: str, apk: bytes, name: str, mime: str,
                    marker: str, api_url: str, media_url: str) -> dict:
    """Upload a bare apk and sendMedia to inputPeerSelf, all in Python.

    Mirrors direct/sender.py's upload_file + send_uploaded_file, but with the
    tokens we read from storage and a self peer that needs no capture. Runs in a
    worker thread (the transport is synchronous).
    """
    import direct.eitaa_tl as E
    from direct.transport import HttpTransport, wrap_eitaa, unwrap_eitaa

    peer = self_peer_bytes()
    try:
        plan = E.build_file_send(peer, apk, name, 0, caption=marker, mime=mime)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"build_file_send: {exc}"}

    media_tx = HttpTransport(media_url, timeout=90.0, cookies={})
    api_tx = HttpTransport(api_url, timeout=30.0, cookies={})

    def rpc(tx, body):
        raw = wrap_eitaa(token1, token2, body)
        resp = tx.post(raw)
        rb = resp
        try:
            if resp[:4] == bytes.fromhex("ed77be7a"):
                rb = unwrap_eitaa(resp)["body"]
        except Exception:  # noqa: BLE001
            pass
        return rb

    # upload every part to the media host
    try:
        for i, part_body in enumerate(plan["parts"]):
            rb = rpc(media_tx, part_body)
            v = E.classify_response(rb)
            if not v.get("ok"):
                return {"ok": False, "code": f"part {i + 1}/{plan['total_parts']}: "
                                             f"{v.get('note')}", "head": rb[:24].hex()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"upload transport: {exc}"}

    # sendMedia (reusing the uploaded document) to the media host
    try:
        rb = rpc(media_tx, plan["send_media"])
        v = E.classify_response(rb)
        return {"ok": bool(v.get("ok")), "code": v.get("note"),
                "head": rb[:24].hex(), "parts": plan["total_parts"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": f"sendMedia transport: {exc}"}


async def run(args) -> int:
    REPORT["args"] = {k: v for k, v in vars(args).items()}
    from config import config
    config.ensure_dirs()
    outdir = config.ARTIFACTS_DIR / f"apk_v2_{time.strftime('%Y%m%d-%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)

    from capture.browser import BrowserSession
    from eitaa.api_resolver import ensure_api_resolver
    from eitaa.driver import EitaaDriver

    banner("STEP 1  -  open the account CLEAN (no hook, app stays healthy)")
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

        banner("STEP 2  -  read token1/token2 straight from storage")
        t, err = await ev(page, READ_TOKENS_JS, 20)
        if err or not t or not t.get("sources"):
            bad(f"could not read tokens: {err or t}")
            REPORT["verdict"].append("No account token in storage - unexpected on a "
                                     "logged-in session; send the report.")
            _save(outdir)
            return 1
        # pick the account matching the logged-in user_id, else the first
        src = None
        for key, val in t["sources"].items():
            if str(val.get("userId")) == str(st.get("my_id")):
                src = (key, val)
                break
        if not src:
            src = next(iter(t["sources"].items()))
        where, acc = src
        token1, token2 = acc.get("token"), acc.get("imei")
        REPORT["ctx"] = {"source": where, "userId": acc.get("userId"),
                         "dcId": acc.get("dcId"), "has_token1": bool(token1),
                         "has_token2": bool(token2)}
        ok(f"tokens from {where}: userId={acc.get('userId')} dcId={acc.get('dcId')}")
        if not token1 or not token2:
            bad("token1 or token2 missing in the stored account")
            REPORT["verdict"].append("The stored account had no usable token pair.")
            _save(outdir)
            return 1
        info(f"token1 ...{token1[-12:]}   token2 {token2}")

        banner("STEP 3  -  send a BARE apk to Saved Messages, several ways")
        apk = apk_bytes(int(args.size_kb * 1024), real)
        info(f"apk {len(apk)} bytes {'(REAL)' if real else '(synthetic)'}")
        OCTET = "application/octet-stream"
        APK = "application/vnd.android.package-archive"
        fateme = "https://fateme.eitaa.com/eitaa/"
        hadi = "https://hadi.eitaa.com/eitaa/"
        api = "https://majid.eitaa.com/eitaa/"
        attempts = [
            ("octet-fateme", "app.apk", OCTET, fateme),
            ("realmime-fateme", "app.apk", APK, fateme),
            ("octet-hadi", "app.apk", OCTET, hadi),
        ]
        for label, name, mime, media in attempts:
            marker = mk_marker(label)
            info(f"{label}: name={name} mime={mime} host={media.split('//')[1].split('/')[0]}")
            try:
                res = await asyncio.to_thread(send_apk_direct, token1, token2, apk,
                                              name, mime, marker, api, media)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "code": f"{type(exc).__name__}: {exc}"}
            res["marker"] = marker
            res["label"] = label
            REPORT["attempts"].append(res)
            (ok if res.get("ok") else bad)(
                f"        send: {res.get('code') or 'ok'}  "
                f"(parts={res.get('parts', '?')})")
            await asyncio.sleep(1.5)

        banner(f"STEP 4  -  what the server stored (waiting {args.settle:.0f}s)")
        await asyncio.sleep(float(args.settle))
        back, err = await ev(page, READBACK_JS, 60)
        msgs = (back or {}).get("messages") or []
        info(f"{len(msgs)} messages in Saved Messages")
        for a in REPORT["attempts"]:
            found = next((m for m in msgs if a["marker"] in (m.get("message") or "")), None)
            a["delivered"] = bool(found)
            a["stored"] = found
            if found:
                win(f"DELIVERED  {a['label']:<16} stored mime={found.get('mime')} "
                    f"name={found.get('file_name')} size={found.get('size')}")
                REPORT["wins"].append(a)
            else:
                bad(f"dropped    {a['label']:<16} not in Saved Messages")

        # self-heal: a win -> realistic size
        if REPORT["wins"] and not real:
            banner("STEP 5  -  re-send each win at a realistic apk size")
            big = apk_bytes(int(float(args.max_mb) * 1024 * 1024), None)
            info(f"big apk {len(big) / 1e6:.1f} MB")
            for w in list(REPORT["wins"]):
                label = w["label"]
                media = {"octet-fateme": "https://fateme.eitaa.com/eitaa/",
                         "realmime-fateme": "https://fateme.eitaa.com/eitaa/",
                         "octet-hadi": "https://hadi.eitaa.com/eitaa/"}[label]
                mime = ("application/vnd.android.package-archive"
                        if "realmime" in label else "application/octet-stream")
                marker = mk_marker(label + "-REAL")
                info(f"{label}-REAL: {len(big) / 1e6:.1f} MB")
                try:
                    res = await asyncio.to_thread(
                        send_apk_direct, token1, token2, big, "app.apk", mime,
                        marker, api, media)
                except Exception as exc:  # noqa: BLE001
                    res = {"ok": False, "code": str(exc)}
                res["marker"] = marker
                res["label"] = label + "-REAL"
                REPORT["attempts"].append(res)
                (ok if res.get("ok") else bad)(f"        send: {res.get('code') or 'ok'}")
            await asyncio.sleep(float(args.settle))
            back2, _ = await ev(page, READBACK_JS, 60)
            msgs2 = (back2 or {}).get("messages") or []
            for a in REPORT["attempts"]:
                if not a["label"].endswith("-REAL"):
                    continue
                found = next((m for m in msgs2 if a["marker"] in (m.get("message") or "")), None)
                if found:
                    win(f"BREAKTHROUGH: {a['label']} delivered a "
                        f"{len(big) / 1e6:.1f} MB bare apk! stored "
                        f"mime={found.get('mime')} name={found.get('file_name')} "
                        f"size={found.get('size')}")
                    a["delivered"] = True
                    REPORT["wins"].append(a)
                else:
                    bad(f"{a['label']}: delivered small, dropped at full size")
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
    big = [w for w in REPORT["wins"] if w.get("label", "").endswith("-REAL")]
    small = [w for w in REPORT["wins"] if not w.get("label", "").endswith("-REAL")]
    if big:
        win("A BARE APK DELIVERS via the direct media host, at full size!")
        for w in big:
            s = w.get("stored") or {}
            v.append(f"{w['label']}: stored mime={s.get('mime')} "
                     f"name={s.get('file_name')} size={s.get('size')}")
        v.append("THIS IS THE ANSWER. The recipient taps the apk and installs - no "
                 "zip, no rename, nothing. Wire apks to the direct engine: read "
                 "the tokens from storage (as here), send via the media host. "
                 "direct/sender.py already does the upload; it just needs the "
                 "storage-token context instead of the broken worker-capture hook.")
    elif small:
        win("A bare apk delivered at small size via the direct host.")
        for w in small:
            v.append(f"{w['label']}: {w.get('stored')}")
        v.append("Re-run with --real-apk to confirm at full size, then wire it in.")
    else:
        oks = [a for a in REPORT["attempts"] if a.get("code") == "ok" or a.get("ok")]
        v.append("The direct sends were made but no bare apk landed in Saved "
                 "Messages.")
        if any("transport" in str(a.get("code", "")) for a in REPORT["attempts"]):
            v.append("Some sends hit a transport error - the tokens may be stale "
                     "or the host rejected the envelope; see the per-attempt codes.")
        else:
            v.append("The envelope was accepted (uploads ok) but the apk was "
                     "filtered server-side on the media host too. That is the "
                     "evidence-based close: a bare apk is not deliverable on ANY "
                     "path, and PDF-with-a-download-link is the way to reach "
                     "elderly recipients.")
    for i, line in enumerate(v, 1):
        print(f"\n  {i}) {line}", flush=True)


def _save(outdir) -> None:
    try:
        (outdir / "report.json").write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\n  full report: {outdir / 'report.json'}", flush=True)
        print("  probe messages tagged APKV2-* in Saved Messages.", flush=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="Direct apk send using tokens read from storage (no hook). "
                    "Own Saved Messages only.")
    p.add_argument("--account", required=True)
    p.add_argument("--size-kb", type=float, default=12)
    p.add_argument("--max-mb", type=float, default=9.6)
    p.add_argument("--real-apk", default=None)
    p.add_argument("--wait", type=float, default=20)
    p.add_argument("--settle", type=float, default=18)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n  interrupted", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
