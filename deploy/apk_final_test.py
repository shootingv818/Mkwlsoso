#!/usr/bin/env python3
"""FINAL apk test — one octet upload, deliver to N (default 10) own-contacts +
Saved Messages, with full telemetry: per-recipient result, exact failure reason,
send rate, throughput, timings, and an upload-stage breakdown.

Upload: the bot's REAL bridge_file_init(), with the .apk MIME forced to
application/octet-stream for THIS test process only (mirrors the pending fix; no
project code changed). Deliver: the bot's REAL bridge_file_send() per recipient
(the same call that sends zip/pdf/txt today).

Standalone DIAGNOSTIC — imports project modules read-only. Recipients are the
owner's own saved contacts + own Saved Messages, with a clear test caption.

    cd ~/Mkwlsoso && DISPLAY=:99 .venv/bin/python deploy/apk_final_test.py --account 989132531349 --contacts 10
"""
from __future__ import annotations

import argparse
import asyncio
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


def log(msg: str = "") -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}" if msg else ""
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


async def run(account: str, n: int, size_mb: float) -> int:
    tmp = tempfile.mkdtemp(prefix="apk_final_")
    apk_path = os.path.join(tmp, f"{TAG}.apk")
    with open(apk_path, "wb") as fh:
        fh.write(b"PK\x03\x04" + b"D" * int(size_mb * 1024 * 1024))
    size = os.path.getsize(apk_path)

    path, cap = _newest_capture(account)
    ctx = E.extract_context(cap) if cap else {}
    self_peer = str(ctx.get("user_id") or "")
    items = [(t, p) for (t, p) in contacts_store.items(account) if p]
    picks = random.sample(items, min(n, len(items))) if items else []

    log(f"FINAL APK TEST  account={account}")
    log(f"file={TAG}.apk  size={size}B (~{size_mb}MB)")
    log(f"self_peer={self_peer}  contacts_available={len(items)}  targets={len(picks)}+Saved")

    rows = []          # (who, peer, ok, code, secs)
    async with open_session(account) as session:
        driver = EitaaDriver(session)
        await driver.open()
        if not await driver.is_logged_in():
            log("✗ not logged in"); return 1

        # ---------- UPLOAD (real path, .apk MIME -> octet for this process) ----------
        log("")
        log("===== UPLOAD (bridge_file_init, .apk MIME forced octet) =====")
        import mimetypes as _mt
        _orig = _mt.guess_type
        _mt.guess_type = lambda p, *a, **k: (
            ("application/octet-stream", None) if str(p).lower().endswith(".apk")
            else _orig(p, *a, **k))
        t_up = time.time()
        try:
            init = await driver.bridge_file_init(apk_path, CAPTION)
        except Exception as exc:  # noqa: BLE001
            init = {"ok": False, "code": f"bridge_file_init error: {exc}"}
        finally:
            _mt.guess_type = _orig
        up_secs = time.time() - t_up
        log(f"  upload ok={init.get('ok')} doc_id={init.get('doc_id')} code={init.get('code')}  "
            f"time={up_secs:.1f}s  speed={size/1024/1024/max(up_secs,0.01):.2f} MB/s")
        if not (isinstance(init, dict) and init.get("ok")):
            log("  ✗ upload failed -> cannot deliver. Fix upload first."); return 2

        # ---------- DELIVER to Saved + N contacts, timed per recipient ----------
        log("")
        log(f"===== DELIVER to {len(picks)}+1 recipients (bridge_file_send) =====")
        targets = ([("Saved Messages", self_peer)] if self_peer else []) + \
                  [(t or f"peer {p}", p) for (t, p) in picks]
        t_send0 = time.time()
        for i, (who, peer) in enumerate(targets, 1):
            t0 = time.time()
            try:
                res = await driver.bridge_file_send(str(peer), CAPTION)
                ok = bool(res.get("ok"))
                code = res.get("code") or ("limit=" + str(res.get("limit")) if res.get("limit") else None)
                msg_id = res.get("msg_id")
            except Exception as exc:  # noqa: BLE001
                ok, code, msg_id = False, f"{type(exc).__name__}: {exc}", None
            dt = time.time() - t0
            rows.append((who, peer, ok, code, dt))
            log(f"  [{i:>2}/{len(targets)}] {who[:24]:24} peer={str(peer):>10}  "
                f"{'SENT ✅' if ok else 'FAIL ❌'}  msg_id={msg_id}  {dt:.2f}s"
                + (f"  reason={code}" if not ok else ""))
        send_secs = time.time() - t_send0

    # ---------- TELEMETRY ----------
    ok_n = sum(1 for r in rows if r[2])
    fail_n = len(rows) - ok_n
    times = [r[4] for r in rows]
    avg = sum(times) / len(times) if times else 0
    rate = ok_n / send_secs if send_secs > 0 else 0

    log("")
    log("================= TELEMETRY =================")
    log(f"  upload        : {up_secs:.1f}s for {size/1024/1024:.1f}MB "
        f"({size/1024/1024/max(up_secs,0.01):.2f} MB/s)")
    log(f"  delivered     : {ok_n}/{len(rows)}   failed: {fail_n}")
    log(f"  send window   : {send_secs:.1f}s   avg/recipient: {avg:.2f}s")
    log(f"  delivery rate : {rate:.2f} msg/s  (~{rate*60:.0f}/min sustained)")
    if fail_n:
        log("  ---- failures (why) ----")
        by = {}
        for who, peer, ok, code, dt in rows:
            if not ok:
                by.setdefault(str(code), []).append(f"{who}({peer})")
        for code, whos in by.items():
            log(f"    {code}: {len(whos)}  e.g. {', '.join(whos[:3])}")
        log("  note: PEER_ID_INVALID = stale/deleted contact peer, NOT an apk problem")
    log("=============================================")
    log("")
    log("================= SUMMARY =================")
    for who, peer, ok, code, dt in rows:
        log(f"  {'✅' if ok else '❌'}  {who[:28]:28} {dt:.2f}s" + (f"  ({code})" if not ok else ""))
    log("==========================================")
    log(f"log saved: {LOGFILE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--contacts", type=int, default=10)
    ap.add_argument("--size-mb", type=float, default=3.0)
    args = ap.parse_args()
    global LOGFILE
    LOGFILE = os.path.join(_ROOT, f"apk_final_{int(time.time())}.log")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(run(args.account, args.contacts, args.size_mb))
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
