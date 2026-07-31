#!/usr/bin/env python3
"""APK send diagnostic (browser-free / direct MTProto path).

Sends a generated dummy file to YOUR OWN Saved Messages using the project's own
direct engine, trying several MIME/size combinations, and reports exactly which
stage each one reaches: session context -> per-part upload -> sendMedia.

Nobody except your own Saved Messages receives anything. Read-only w.r.t. the
project: it only imports existing modules and writes dummy files under a temp
dir plus a log file. It does NOT touch settings, contacts or state.

Run on the SERVER (needs the logged-in capture + cookies + network to Eitaa):

    cd ~/Mkwlsoso && .venv/bin/python deploy/apk_diag.py --account 989132531349

Options:
    --account   the logged-in account (required)
    --size-mb   dummy file size for the main cases (default 9.6, matches a real apk)
    --to        target contact name (default: your own Saved Messages)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import tempfile

# repo root on sys.path so "direct"/"cli" import cleanly regardless of CWD
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# the direct engine is gated behind this flag in some builds; enable for the test
os.environ.setdefault("MKWL_ENABLE_DIRECT", "1")

from direct import eitaa_tl as E
from direct.transport import HttpTransport, wrap_eitaa, unwrap_eitaa
# reuse the exact helpers the real CLI send uses, so this mirrors production
from cli import _newest_capture, _load_cookies, _resolve_target_peer, _direct_rpc

SAVE_OK = "b5757299"  # upload.saveFilePart -> boolTrue on the wire


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def make_dummy(path: str, size_bytes: int) -> None:
    """A zip-shaped blob (apk == zip) of an exact size, deterministic-ish."""
    with open(path, "wb") as fh:
        fh.write(b"PK\x03\x04")                 # zip local-file-header magic
        remaining = size_bytes - 4
        chunk = (b"MKWLDIAG" * 128)             # 1 KiB filler
        while remaining > 0:
            take = min(len(chunk), remaining)
            fh.write(chunk[:take])
            remaining -= take


def run_case(label: str, ctx, cap, peer, tgt, cookies,
             data: bytes, file_name: str, mime: str) -> dict:
    """One upload+send attempt. Returns a result dict; never raises."""
    res = {"label": label, "name": file_name, "mime": mime,
           "size": len(data), "stage": "start", "ok": False,
           "parts_ok": 0, "parts_total": 0, "secs": 0.0, "detail": ""}
    t0 = time.time()
    log("")
    log(f"===== CASE: {label} =====")
    log(f"  file={file_name}  size={len(data)}B  mime={mime}")
    try:
        plan = E.build_file_send(peer, data, file_name, ctx["user_id"],
                                 caption="diag", mime=mime)
        res["parts_total"] = plan["total_parts"]
        endpoint = E.extract_media_url(cap) or "https://fateme.eitaa.com/eitaa/"
        log(f"  media endpoint={endpoint}  parts={plan['total_parts']}  cookies={len(cookies)}")
        tx = HttpTransport(endpoint, timeout=60.0, cookies=dict(cookies))
        res["stage"] = "upload"
        try:
            for i, part_body in enumerate(plan["parts"]):
                pt = time.time()
                raw = wrap_eitaa(ctx["token1"], ctx["token2"], part_body)
                resp = tx.post(raw)
                rb = resp
                try:
                    if resp[:4] == bytes.fromhex("ed77be7a"):
                        rb = unwrap_eitaa(resp)["body"]
                except Exception:  # noqa: BLE001
                    pass
                ok = rb[:4].hex() == SAVE_OK
                log(f"  part {i+1}/{plan['total_parts']}: "
                    f"{'OK' if ok else 'REJECT ' + rb[:16].hex()}  ({time.time()-pt:.2f}s)")
                if not ok:
                    res["detail"] = f"part {i+1} rejected: {rb[:48].hex()}"
                    res["secs"] = time.time() - t0
                    tx.close()
                    return res
                res["parts_ok"] = i + 1
            log("  all parts uploaded; sending media on the same connection...")
            res["stage"] = "sendMedia"
            rc = _direct_rpc(plan["send_media"], ctx, endpoint, "diag", tx=tx)
            res["ok"] = (rc == 0)
            res["stage"] = "done" if rc == 0 else "sendMedia"
            res["detail"] = f"_direct_rpc rc={rc}"
        finally:
            tx.close()
    except Exception as exc:  # noqa: BLE001
        res["detail"] = f"{type(exc).__name__}: {exc}"
        log(f"  ✗ EXCEPTION at stage '{res['stage']}': {res['detail']}")
    res["secs"] = time.time() - t0
    log(f"  -> stage={res['stage']} ok={res['ok']} in {res['secs']:.1f}s")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--size-mb", type=float, default=9.6)
    ap.add_argument("--to", default=None)
    args = ap.parse_args()

    global LOGFILE
    LOGFILE = os.path.join(_ROOT, f"apk_diag_{int(time.time())}.log")
    log(f"APK DIAGNOSTIC  account={args.account}  size={args.size_mb}MB  log={LOGFILE}")

    # --- session context + target (once) ---
    path, cap = _newest_capture(args.account)
    if not cap:
        log(f"✗ no capture for '{args.account}'. Log in / Update Contacts first.")
        return 1
    log(f"capture: {path}")
    try:
        ctx = E.extract_context(cap)
    except Exception as exc:  # noqa: BLE001
        log(f"✗ cannot extract session context: {exc}")
        return 2
    peer, tgt = _resolve_target_peer(args.account, ctx, args.to, "diag")
    if not peer:
        log("✗ could not resolve a target peer")
        return 2
    cookies = _load_cookies(args.account)
    log(f"target={tgt}  user_id={ctx.get('user_id')}  cookies={len(cookies)}")

    tmp = tempfile.mkdtemp(prefix="apk_diag_")
    big = os.path.join(tmp, "diag_big.apk")
    small = os.path.join(tmp, "diag_small.apk")
    make_dummy(big, int(args.size_mb * 1024 * 1024))
    make_dummy(small, 400 * 1024)
    big_bytes = open(big, "rb").read()
    small_bytes = open(small, "rb").read()

    cases = [
        # label, data, filename, mime
        ("small .apk / octet-stream (route+baseline)", small_bytes, "diag_small.apk",
         "application/octet-stream"),
        ("big .apk / real apk MIME (what our direct path sends now)", big_bytes,
         "diag_big.apk", E.guess_mime("x.apk")),
        ("big .apk / octet-stream (APK-mode / competitor style)", big_bytes,
         "diag_big.apk", "application/octet-stream"),
    ]
    results = [run_case(lbl, ctx, cap, peer, tgt, cookies, d, n, m)
               for (lbl, d, n, m) in cases]

    log("")
    log("================= SUMMARY =================")
    for r in results:
        verdict = "PASS ✅" if r["ok"] else "FAIL ❌"
        log(f"{verdict}  {r['label']}")
        log(f"        stage={r['stage']}  parts={r['parts_ok']}/{r['parts_total']}  "
            f"mime={r['mime']}  {r['secs']:.1f}s")
        if r["detail"]:
            log(f"        detail: {r['detail']}")
    log("==========================================")
    log(f"full log saved to: {LOGFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
