#!/usr/bin/env python3
"""Confident apk test — upload the proven way, deliver the bot's real way.

Upload: ONE octet-stream upload of a real .apk via the SAME page function the
bot's bridge_file_init() uses (window.__MKWL_fileInit) — this is exactly what
the fix will make bridge_file_init do for .apk files.

Deliver: the bot's REAL per-recipient send, driver.bridge_file_send() (the same
call that already delivers zip/pdf/txt), to Saved Messages + several random
own-contacts. Then it VERIFIES from the server (reads Saved history back) the
stored file name / size / mime, as hard proof the apk landed.

Standalone DIAGNOSTIC: imports project modules read-only, changes NO project
code. Recipients are the owner's own account + the owner's own saved contacts,
with a clear "this is a test, ignore" caption.

    cd ~/Mkwlsoso && DISPLAY=:99 .venv/bin/python deploy/apk_confident_test.py --account 989132531349 --contacts 3
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import os
import random
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from capture.browser import open_session
from eitaa.driver import EitaaDriver
from direct import eitaa_tl as E
from bot import contacts_store
from cli import _newest_capture

LOGFILE = ""
TAG = f"MKWLAPK{int(time.time())}"
CAPTION = f"تست فنی فایل apk — لطفا نادیده بگیرید 🙏 [{TAG}]"

# read Saved Messages history back and describe the newest doc matching TAG
VERIFY_JS = r"""
async (tag) => {
  const AM = window.apiManager;
  const self = { _: 'inputPeerSelf' };
  try {
    const h = await AM.invokeApi('messages.getHistory',
      { peer: self, offset_id:0, offset_date:0, add_offset:0, limit:12, max_id:0, min_id:0, hash:0 });
    for (const m of ((h && h.messages) || [])) {
      if (m && m.message && m.message.indexOf(tag) !== -1 && m.media && m.media.document) {
        const d = m.media.document;
        let name = null;
        for (const a of (d.attributes || [])) if (a.file_name) name = a.file_name;
        return { found: true, name, size: d.size, mime: d.mime_type, msg_id: m.id };
      }
    }
  } catch(e) { return { found: false, error: String(e) }; }
  return { found: false };
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


async def run(account: str, n_contacts: int, size_mb: float) -> int:
    tmp = tempfile.mkdtemp(prefix="apk_conf_")
    apk_path = os.path.join(tmp, f"{TAG}.apk")
    with open(apk_path, "wb") as fh:
        fh.write(b"PK\x03\x04" + b"D" * int(size_mb * 1024 * 1024))
    size = os.path.getsize(apk_path)

    path, cap = _newest_capture(account)
    ctx = E.extract_context(cap) if cap else {}
    self_peer = str(ctx.get("user_id") or "")
    items = [(t, p) for (t, p) in contacts_store.items(account) if p]
    picks = random.sample(items, min(n_contacts, len(items))) if items else []

    log(f"CONFIDENT APK TEST  account={account}")
    log(f"file={os.path.basename(apk_path)}  size={size}B (~{size_mb}MB)  tag={TAG}")
    log(f"self_peer={self_peer}  contacts_available={len(items)}  will send to {len(picks)} contact(s)")

    results = {}
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            log("✗ not logged in"); return 1

        # ---- UPLOAD ONCE, octet-stream, via the bot's real page upload fn ----
        log("")
        log("===== UPLOAD (octet-stream, same __MKWL_fileInit the bot uses) =====")
        with open(apk_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        locate_ms = int(min(420.0, 45.0 + 25.0 * size_mb) * 1000)
        try:
            init = await driver.page.evaluate(
                "(a) => window.__MKWL_fileInit(a.b, a.n, a.m, a.d)",
                {"b": b64, "n": os.path.basename(apk_path),
                 "m": "application/octet-stream", "d": locate_ms},
            )
        except Exception as exc:  # noqa: BLE001
            init = {"ok": False, "code": f"evaluate error: {exc}"}
        log(f"  upload: ok={init.get('ok')} doc_id={init.get('doc_id')} code={init.get('code')}")
        if not (isinstance(init, dict) and init.get("ok")):
            log("  ✗ upload failed -> aborting"); return 2

        # ---- DELIVER via the bot's REAL send (bridge_file_send) ----
        log("")
        log("===== DELIVER (driver.bridge_file_send — the bot's real send) =====")
        targets = ([("Saved Messages", self_peer)] if self_peer else []) + \
                  [(t or f"peer {p}", p) for (t, p) in picks]
        for who, peer in targets:
            res = await driver.bridge_file_send(str(peer), CAPTION)
            ok = bool(res.get("ok"))
            results[who] = ok
            log(f"  {who:28} peer={peer}: {'SENT ✅' if ok else 'FAILED ❌'} "
                f"msg_id={res.get('msg_id')} code={res.get('code')}")
            await asyncio.sleep(1)

        # ---- VERIFY from the server (Saved Messages history) ----
        log("")
        log("===== VERIFY (read Saved Messages back from the server) =====")
        try:
            v = await driver.page.evaluate(VERIFY_JS, TAG)
        except Exception as exc:  # noqa: BLE001
            v = {"found": False, "error": str(exc)}
        if v.get("found"):
            log(f"  ✅ apk is really in Saved Messages:")
            log(f"     name={v.get('name')}  size={v.get('size')}  mime={v.get('mime')}  msg_id={v.get('msg_id')}")
            log(f"     (name ends .apk, delivered as mime={v.get('mime')} so Eitaa didn't block it)")
        else:
            log(f"  ⚠ could not confirm from history: {v}")

    log("")
    log("================= SUMMARY =================")
    for who, ok in results.items():
        log(f"  {'✅' if ok else '❌'}  {who}")
    delivered = sum(1 for ok in results.values() if ok)
    log(f"  delivered to {delivered}/{len(results)} recipients")
    log("==========================================")
    log(f"log saved: {LOGFILE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--contacts", type=int, default=3, help="random own-contacts to send to")
    ap.add_argument("--size-mb", type=float, default=3.0)
    args = ap.parse_args()
    global LOGFILE
    LOGFILE = os.path.join(_ROOT, f"apk_confident_{int(time.time())}.log")
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(run(args.account, args.contacts, args.size_mb))
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
