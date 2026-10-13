#!/usr/bin/env python3
"""APK-focused rejection probe — find WHERE and WHY Eitaa refuses an apk.

Uploads IDENTICAL bytes to YOUR OWN Saved Messages several times, changing ONLY
the filename / MIME / caption, over ONE keep-alive connection to the account's
REAL host. The only variable is "apk-ness", so wherever the apk case diverges
from the zip case is exactly what Eitaa filters on. Prints the FULL server reply
(and any rpc_error text) for each case.

This is a standalone DIAGNOSTIC. It imports existing modules read-only and does
NOT modify any project code. It delivers only to your own Saved Messages.

Run on the SERVER:
    cd ~/Mkwlsoso && .venv/bin/python deploy/apk_reject_probe.py --account 989132531349
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("MKWL_ENABLE_DIRECT", "1")

from direct import eitaa_tl as E
from direct.transport import HttpTransport, wrap_eitaa, unwrap_eitaa
from direct.sender import extract_api_url
from cli import _newest_capture, _load_cookies, _resolve_target_peer

LOGFILE = ""
SAVE_OK = "b5757299"  # boolTrue -> saveFilePart accepted


def log(msg: str = "") -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}" if msg else ""
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _printable(data: bytes, minlen: int = 3):
    out, cur = [], []
    for b in data:
        if 32 <= b < 127:
            cur.append(chr(b))
        else:
            if len(cur) >= minlen:
                out.append("".join(cur))
            cur = []
    if len(cur) >= minlen:
        out.append("".join(cur))
    return out


def account_host(cap) -> str:
    """The host this account actually uses (most frequent in its own traffic)."""
    c = Counter()
    try:
        for rec in E._iter_records(cap):
            if rec.get("kind") in ("fetch", "xhr"):
                h = urlparse(rec.get("url") or "").hostname
                if h and "eitaa" in h:
                    c[h] += 1
    except Exception:  # noqa: BLE001
        pass
    return c.most_common(1)[0][0] if c else "hadi.eitaa.com"


def run_case(label, endpoint, ctx, cookies, peer, data, file_name, mime, caption):
    log("")
    log(f"===== CASE: {label} =====")
    log(f"  name={file_name}  mime={mime}  caption={caption!r}  bytes={len(data)}")
    res = {"label": label, "ok": False, "stage": "build", "detail": ""}
    tx = HttpTransport(endpoint, timeout=60.0, cookies=dict(cookies))
    try:
        plan = E.build_file_send(peer, data, file_name, ctx["user_id"],
                                 caption=caption, mime=mime)
        log(f"  parts={plan['total_parts']}  endpoint={endpoint}")
        # ---- upload every part on THIS one connection ----
        res["stage"] = "upload"
        for i, part in enumerate(plan["parts"]):
            resp = tx.post(wrap_eitaa(ctx["token1"], ctx["token2"], part))
            rb = resp
            try:
                if resp[:4] == bytes.fromhex("ed77be7a"):
                    rb = unwrap_eitaa(resp)["body"]
            except Exception:  # noqa: BLE001
                pass
            ok = rb[:4].hex() == SAVE_OK
            if not ok:
                res["detail"] = f"saveFilePart {i+1} rejected: {rb[:64].hex()}"
                log(f"  ✗ part {i+1}/{plan['total_parts']} REJECTED: {rb[:64].hex()}")
                for s in _printable(rb):
                    log(f"      text: {s}")
                return res
        log(f"  ✓ all {plan['total_parts']} parts accepted (boolTrue)")
        # ---- sendMedia on the SAME connection ----
        res["stage"] = "sendMedia"
        resp = tx.post(wrap_eitaa(ctx["token1"], ctx["token2"], plan["send_media"]))
        rb = resp
        try:
            if resp[:4] == bytes.fromhex("ed77be7a"):
                rb = unwrap_eitaa(resp)["body"]
        except Exception:  # noqa: BLE001
            pass
        log(f"  sendMedia reply {len(rb)}B  head={rb[:48].hex()}")
        try:
            verdict = E.classify_response(rb)
        except Exception as exc:  # noqa: BLE001
            verdict = {"ok": False, "note": f"classify failed: {exc}"}
        log(f"  verdict: ok={verdict.get('ok')}  note={verdict.get('note')}")
        # surface ANY human-readable text in the reply (rpc_error message etc.)
        texts = [s for s in _printable(rb) if len(s) >= 4]
        if texts:
            log(f"  reply text runs: {texts}")
        res["ok"] = bool(verdict.get("ok"))
        res["detail"] = verdict.get("note", "")
        res["stage"] = "done" if res["ok"] else "sendMedia"
    except Exception as exc:  # noqa: BLE001
        res["detail"] = f"{type(exc).__name__}: {exc}"
        log(f"  ✗ EXCEPTION at {res['stage']}: {res['detail']}")
    finally:
        tx.close()
    log(f"  -> {label}: {'DELIVERED ✅' if res['ok'] else 'FAILED ❌'} (stage={res['stage']})")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--kb", type=int, default=300, help="identical payload size in KB")
    args = ap.parse_args()

    global LOGFILE
    LOGFILE = os.path.join(_ROOT, f"apk_reject_{int(time.time())}.log")
    log(f"APK REJECTION PROBE  account={args.account}  log={LOGFILE}")

    path, cap = _newest_capture(args.account)
    if not cap:
        log("✗ no capture; log in first")
        return 1
    ctx = E.extract_context(cap)
    cookies = _load_cookies(args.account)
    peer, tgt = _resolve_target_peer(args.account, ctx, None, "apkrej")
    host = account_host(cap)
    endpoint = f"https://{host}/eitaa/"
    log(f"capture={path}")
    log(f"user_id={ctx.get('user_id')}  target={tgt}  host={host}  cookies={len(cookies)}")

    # ONE identical blob (zip-shaped, since apk == zip) reused by every case
    blob = b"PK\x03\x04" + (b"MKWLDIAG" * ((args.kb * 1024) // 8))

    cases = [
        # label, filename, mime, caption
        ("A. zip / zip-mime (reference, should deliver)", "diag.zip",
         "application/zip", "diag zip"),
        ("B. apk / real apk MIME (what breaks?)", "diag.apk",
         "application/vnd.android.package-archive", "diag apk"),
        ("C. apk / octet-stream (competitor style)", "diag.apk",
         "application/octet-stream", "diag apk octet"),
        ("D. zip-name + '.apk' only in caption / octet (ext vs content)", "diag.zip",
         "application/octet-stream", "here is diag.apk"),
    ]
    results = [run_case(lbl, endpoint, ctx, cookies, peer, blob, name, mime, cap_)
               for (lbl, name, mime, cap_) in cases]

    log("")
    log("================= SUMMARY =================")
    for r in results:
        log(f"  {'DELIVERED ✅' if r['ok'] else 'FAILED ❌'}  {r['label']}")
        log(f"        stage={r['stage']}  {r['detail']}")
    log("------------------------------------------")
    a = next((r for r in results if r["label"].startswith("A")), None)
    b = next((r for r in results if r["label"].startswith("B")), None)
    c = next((r for r in results if r["label"].startswith("C")), None)
    if a and a["ok"] and b and not b["ok"] and c and c["ok"]:
        log("  CONCLUSION: upload is fine; Eitaa blocks the apk MIME; octet-stream")
        log("  fixes it -> the APK-mode toggle is the correct fix.")
    elif a and a["ok"] and b and not b["ok"] and c and not c["ok"]:
        log("  CONCLUSION: Eitaa blocks apk beyond MIME (filename/content). Read the")
        log("  reply text above for the exact rule.")
    elif a and not a["ok"]:
        log("  CONCLUSION: even zip failed here -> host/connection issue, not apk.")
    log("==========================================")
    log(f"log saved: {LOGFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
