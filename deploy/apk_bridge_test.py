#!/usr/bin/env python3
"""APK bridge test — isolate WHAT Eitaa filters on, via the REAL send path.

The bot sends files through the browser (bridge) page engine, which is what
works for zip/pdf/txt. This test drives that SAME page engine and sends the
SAME bytes several times, changing ONLY the filename / MIME, all to the owner's
own Saved Messages. Wherever the apk case diverges from the zip case is exactly
Eitaa's filter — and whichever variant DOES go through is the competitor's
bypass.

Matrix (identical bytes each time):
  1. control.zip      / auto            -> reference, must go through
  2. app.apk          / apk MIME        -> what the bot sends now
  3. app.apk          / octet-stream    -> APK-mode idea, on the real path
  4. app_renamed.zip  / zip MIME        -> apk bytes but .zip name (ext bypass?)
  5. app.bin          / octet-stream    -> neutral extension

Standalone DIAGNOSTIC. Embeds its OWN page script; imports project modules
read-only; changes NO project code; delivers only to Saved Messages.

    cd ~/Mkwlsoso && DISPLAY=:99 .venv/bin/python deploy/apk_bridge_test.py --account 989132531349
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from capture.browser import open_session
from eitaa.driver import EitaaDriver

LOGFILE = ""

# Runs in the page: build a File with the GIVEN name+mime, sendFile to self,
# then poll getHistory to confirm the file actually landed. Returns a step log
# with any server/page error code. Nothing but Saved Messages is touched.
SEND_JS = r"""
async (arg) => {
  const { b64, filename, mime, caption } = arg;
  const out = { sent: false, located: false, doc_mime: null, error: null, steps: [] };
  const log = (n, ok, i) => out.steps.push({ n, ok, i: (typeof i === 'string' ? i.slice(0,200) : i) });
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const errCode = e => { try { return String((e && (e.type||e.error_message||e.code||e.message))||e); } catch(x){ return 'ERR'; } };
  const peerSelf = { _: 'inputPeerSelf' };
  const AM = window.apiManager, AMM = window.appMessagesManager;
  if (!AM || !AMM || !AMM.sendFile) { out.error = 'no apiManager/sendFile'; return out; }
  let selfNum = null;
  try { selfNum = window.appImManager && window.appImManager.myId; } catch(e){}
  try { if (selfNum == null && window.appPeersManager) selfNum = window.appPeersManager.peerId; } catch(e){}

  let file;
  try {
    const bin = atob(b64); const u = new Uint8Array(bin.length);
    for (let i=0;i<bin.length;i++) u[i]=bin.charCodeAt(i);
    file = new File([u], filename, { type: mime || 'application/octet-stream' });
    log('build', true, filename + ' ' + file.size + 'B type=' + (mime||''));
  } catch(e){ out.error = 'build:'+errCode(e); return out; }

  try { await AMM.sendFile({ peerId: selfNum, file, caption }); out.sent = true; log('sendFile', true, ''); }
  catch(e){ try { await AMM.sendFile(selfNum, file, { caption }); out.sent = true; log('sendFile+', true, ''); }
            catch(e2){ out.error = 'sendFile:'+errCode(e2); log('sendFile', false, errCode(e2)); return out; } }

  for (let a=0; a<30 && !out.located; a++) {
    await sleep(1000);
    try {
      const h = await AM.invokeApi('messages.getHistory',
        { peer: peerSelf, offset_id:0, offset_date:0, add_offset:0, limit:8, max_id:0, min_id:0, hash:0 });
      for (const m of ((h && h.messages) || [])) {
        if (m && m.message && m.message.indexOf(caption) !== -1) {
          out.located = !!(m.media && m.media.document);
          if (m.media && m.media.document) out.doc_mime = m.media.document.mime_type;
          break;
        }
      }
    } catch(e){ out.error = 'getHistory:'+errCode(e); break; }
  }
  return out;
}
"""


def log(msg: str = "") -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}" if msg else ""
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


async def run(account: str) -> int:
    blob = b"PK\x03\x04" + b"D" * (1024 * 1024)  # ~1MB, identical every case
    b64 = base64.b64encode(blob).decode("ascii")

    cases = [
        ("1. control.zip / auto",      "control.zip",      "application/zip"),
        ("2. app.apk / apk-mime",      "app.apk",          "application/vnd.android.package-archive"),
        ("3. app.apk / octet-stream",  "app.apk",          "application/octet-stream"),
        ("4. apk-bytes named .zip",    "app_renamed.zip",  "application/zip"),
        ("5. app.bin / octet-stream",  "app.bin",          "application/octet-stream"),
    ]

    log(f"APK BRIDGE TEST  account={account}")
    results = []
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            log("✗ not logged in for this account"); return 1
        try:
            await driver.open_saved_messages()
        except Exception:  # noqa: BLE001
            pass

        for label, name, mime in cases:
            cap = f"MKWLAPK{int(time.time())}_{name.replace('.', '_')}"
            log("")
            log(f"===== {label} =====")
            log(f"  name={name}  mime={mime}")
            try:
                res = await driver.page.evaluate(
                    SEND_JS, {"b64": b64, "filename": name, "mime": mime, "caption": cap})
            except Exception as exc:  # noqa: BLE001
                res = {"error": f"evaluate failed: {exc}"}
            sent = bool(res.get("sent"))
            located = bool(res.get("located"))
            ok = sent and located
            log(f"  sent={sent}  located={located}  doc_mime={res.get('doc_mime')}  error={res.get('error')}")
            for s in res.get("steps", []):
                log(f"    {'✅' if s.get('ok') else '❌'} {s.get('n')}: {s.get('i')}")
            log(f"  -> {'DELIVERED ✅' if ok else 'FAILED ❌'}")
            results.append((label, ok, res.get("error")))
            await asyncio.sleep(2)

    log("")
    log("================= SUMMARY =================")
    for label, ok, err in results:
        log(f"  {'✅' if ok else '❌'}  {label}" + (f"   ({err})" if err else ""))
    log("------------------------------------------")
    zip_ok = results[0][1] if results else False
    apk_mime = results[1][1] if len(results) > 1 else False
    apk_octet = results[2][1] if len(results) > 2 else False
    renamed = results[3][1] if len(results) > 3 else False
    if zip_ok and not apk_mime and apk_octet:
        log("  CONCLUSION: Eitaa filters the apk MIME; octet-stream bypasses it")
        log("  -> fix belongs in the BRIDGE send (set File type=octet for .apk).")
    elif zip_ok and not apk_mime and not apk_octet and renamed:
        log("  CONCLUSION: Eitaa filters the .apk FILENAME; sending apk bytes under")
        log("  a .zip name goes through -> bypass = rename on send.")
    elif zip_ok and not any([apk_mime, apk_octet, renamed]):
        log("  CONCLUSION: Eitaa rejects apk by CONTENT/signature, not name/mime.")
        log("  -> need the competitor's exact trick; paste their file's headers.")
    elif not zip_ok:
        log("  CONCLUSION: even the control zip failed -> session/page issue, retry.")
    log("==========================================")
    log(f"log saved: {LOGFILE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    args = ap.parse_args()
    global LOGFILE
    LOGFILE = os.path.join(_ROOT, f"apk_bridge_{int(time.time())}.log")
    return asyncio.get_event_loop().run_until_complete(run(args.account))


if __name__ == "__main__":
    sys.exit(main())
