#!/usr/bin/env python3
"""REAL apk send test — the bot's ACTUAL bridge path, to Saved Messages + one
random own-contact, comparing the current behaviour vs the octet-stream fix.

Bismillah. Confidence run: sends a throwaway .apk with a clear "this is a test,
please ignore" caption. Two ways, both over the SAME machinery the bot uses:

  CASE A - exactly what the bot does now: bridge_file_init() derives the MIME
           from the OS mime database. On Debian/Ubuntu that yields the real apk
           MIME, which Eitaa blocks -> reproduces the failure.
  CASE B - the fix: upload the SAME .apk but force application/octet-stream via
           the same page upload function -> should deliver to Saved Messages AND
           the random contact.

Standalone DIAGNOSTIC: embeds nothing, imports project modules read-only, does
NOT change any project code. Recipients are the owner's own account + one of the
owner's own saved contacts, with an explanatory caption.

    cd ~/Mkwlsoso && DISPLAY=:99 .venv/bin/python deploy/apk_realsend_test.py --account 989132531349
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import mimetypes
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
CAPTION = "تست فنی فایل — لطفا نادیده بگیرید 🙏 (this is a test, please ignore)"


def log(msg: str = "") -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}" if msg else ""
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


async def deliver(driver, peer_id, who) -> bool:
    res = await driver.bridge_file_send(str(peer_id), CAPTION)
    ok = bool(res.get("ok"))
    log(f"    -> {who} (peer={peer_id}): {'DELIVERED ✅' if ok else 'FAILED ❌'} "
        f"method={res.get('method')} msg_id={res.get('msg_id')} code={res.get('code')}")
    return ok


async def run(account: str) -> int:
    # dummy apk (~1MB), zip-shaped bytes; content is irrelevant to Eitaa's filter
    tmp = tempfile.mkdtemp(prefix="apk_real_")
    apk_path = os.path.join(tmp, f"test_{int(time.time())}.apk")
    with open(apk_path, "wb") as fh:
        fh.write(b"PK\x03\x04" + b"D" * (1024 * 1024))

    server_mime = mimetypes.guess_type(apk_path)[0]
    log(f"REAL APK SEND TEST  account={account}")
    log(f"★ THIS SERVER's mimetypes for .apk = {server_mime!r}")
    log("  (if it is the vnd.android apk mime, THAT is why the bot's send is blocked)")

    # self peer id + a random own-contact
    path, cap = _newest_capture(account)
    ctx = E.extract_context(cap) if cap else {}
    self_peer = str(ctx.get("user_id") or "")
    items = [(t, p) for (t, p) in contacts_store.items(account) if p]
    contact = random.choice(items) if items else None
    log(f"self_peer={self_peer}  saved_contacts_with_peer={len(items)}")
    if contact:
        log(f"random contact picked: title={contact[0]!r} peer={contact[1]}")
    else:
        log("⚠ no saved contact with a peer_id -> will only test Saved Messages")

    results = {}
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            log("✗ not logged in"); return 1

        # ---------- CASE A: exactly what the bot does now ----------
        log("")
        log("===== CASE A: bot's current path (bridge_file_init, OS mime) =====")
        initA = await driver.bridge_file_init(apk_path, CAPTION)
        log(f"  upload: ok={initA.get('ok')} doc_id={initA.get('doc_id')} code={initA.get('code')}")
        if initA.get("ok"):
            if self_peer:
                results["A_saved"] = await deliver(driver, self_peer, "Saved Messages")
            if contact:
                results["A_contact"] = await deliver(driver, contact[1], f"contact {contact[0]!r}")
        else:
            log("  upload failed -> nothing to deliver for case A")

        await asyncio.sleep(2)

        # ---------- CASE B: the fix — force octet-stream on the SAME page fn ----------
        log("")
        log("===== CASE B: fix — same .apk uploaded as octet-stream =====")
        with open(apk_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        try:
            initB = await driver.page.evaluate(
                "(a) => window.__MKWL_fileInit(a.b, a.n, a.m, a.d)",
                {"b": b64, "n": os.path.basename(apk_path),
                 "m": "application/octet-stream", "d": 240000},
            )
        except Exception as exc:  # noqa: BLE001
            initB = {"ok": False, "code": f"evaluate error: {exc}"}
        log(f"  upload: ok={initB.get('ok')} doc_id={initB.get('doc_id')} code={initB.get('code')}")
        if isinstance(initB, dict) and initB.get("ok"):
            if self_peer:
                results["B_saved"] = await deliver(driver, self_peer, "Saved Messages")
            if contact:
                results["B_contact"] = await deliver(driver, contact[1], f"contact {contact[0]!r}")
        else:
            log("  upload failed -> nothing to deliver for case B")

    log("")
    log("================= SUMMARY =================")
    log(f"  server .apk mime : {server_mime!r}")
    for k in ("A_saved", "A_contact", "B_saved", "B_contact"):
        if k in results:
            log(f"  {k:10}: {'DELIVERED ✅' if results[k] else 'FAILED ❌'}")
    log("------------------------------------------")
    a_ok = results.get("A_saved") or results.get("A_contact")
    b_ok = results.get("B_saved") or results.get("B_contact")
    if not a_ok and b_ok:
        log("  CONFIRMED: current path fails for apk; octet-stream fix delivers to")
        log("  BOTH Saved Messages and a real contact. Fix = force octet for .apk")
        log("  in bridge_file_init (gated by the APK-mode toggle).")
    elif a_ok and b_ok:
        log("  Both delivered here -> on THIS server mimetypes did not return the")
        log(f"  apk mime (got {server_mime!r}); the block only bites when it does.")
    elif not a_ok and not b_ok:
        log("  Neither delivered -> not a mime issue on this run; check the upload")
        log("  'code' lines above (session/upload problem).")
    log("==========================================")
    log(f"log saved: {LOGFILE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    args = ap.parse_args()
    global LOGFILE
    LOGFILE = os.path.join(_ROOT, f"apk_realsend_{int(time.time())}.log")
    return asyncio.get_event_loop().run_until_complete(run(args.account))


if __name__ == "__main__":
    sys.exit(main())
