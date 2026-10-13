#!/usr/bin/env python3
"""Upload SWEEP — pinpoint exactly which variable flips saveFilePart 200 -> 500.

A 32-byte part succeeded on hadi; a 300 KB part 500'd. This isolates the cause
by sweeping ONE variable at a time on the account's real host:

  1. SIZE sweep      - one saveFilePart at 8B..512KB via a fresh Connection:close
  2. CONNECTION test - same small part reused on ONE keep-alive connection x3
  3. TRANSPORT test  - a 300KB part via the project's HttpTransport (keep-alive)
  4. HOST compare    - the 300KB part against hadi / majid / fateme

Read-only DIAGNOSTIC: sends throwaway parts (never finalised with sendMedia, so
nothing is delivered). New standalone file; changes NO project code.

    cd ~/Mkwlsoso && .venv/bin/python deploy/upload_sweep.py --account 989132531349
"""
from __future__ import annotations

import argparse
import http.client
import os
import ssl
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
from cli import _newest_capture, _load_cookies

LOGFILE = ""


def log(msg: str = "") -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}" if msg else ""
    print(line, flush=True)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def account_host(cap) -> str:
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


def classify(status, data: bytes) -> str:
    if data[:4].hex() == "b5757299":
        return "boolTrue ✅"
    low = data[:200].lower()
    if b"<html" in low or b"nginx" in low:
        return "nginx/html ❌"
    if not data:
        return "EMPTY body ❌"
    if data[:4].hex() == "ed77be7a":
        try:
            data = unwrap_eitaa(data)["body"]
            if data[:4].hex() == "b5757299":
                return "boolTrue (wrapped) ✅"
        except Exception:  # noqa: BLE001
            pass
    return f"other head={data[:8].hex()}"


def part_for(peer, ctx, size: int) -> bytes:
    data = (b"PK\x03\x04" + b"D" * size)[:size] if size >= 4 else b"D" * size
    plan = E.build_file_send(peer, data, "s.bin", ctx["user_id"],
                             caption="", mime="application/octet-stream")
    return wrap_eitaa(ctx["token1"], ctx["token2"], plan["parts"][0])


def raw_send(host, body, cookies, keepalive_conn=None, timeout=20):
    """POST one part. If keepalive_conn given, reuse it; else fresh close conn."""
    close_after = keepalive_conn is None
    conn = keepalive_conn or http.client.HTTPSConnection(
        host, 443, timeout=timeout, context=ssl.create_default_context())
    h = {"Content-Type": "application/octet-stream",
         "Content-Length": str(len(body)),
         "Connection": "keep-alive" if not close_after else "close"}
    if cookies:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        conn.request("POST", "/eitaa/", body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        r = (resp.status, len(data), classify(resp.status, data), resp.getheader("Server"))
    finally:
        if close_after:
            conn.close()
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    args = ap.parse_args()
    global LOGFILE
    LOGFILE = os.path.join(_ROOT, f"upload_sweep_{int(time.time())}.log")
    log(f"UPLOAD SWEEP  account={args.account}  log={LOGFILE}")

    path, cap = _newest_capture(args.account)
    if not cap:
        log("✗ no capture"); return 1
    ctx = E.extract_context(cap)
    cookies = _load_cookies(args.account)
    peer = E.input_peer_self(ctx["user_id"], ctx.get("access_hash", 0))
    host = account_host(cap)
    log(f"host={host}  user_id={ctx.get('user_id')}  cookies={len(cookies)}")

    # 1) SIZE sweep, fresh Connection: close each time
    log("")
    log("===== 1) SIZE SWEEP (fresh Connection: close) =====")
    sizes = [8, 1024, 16 * 1024, 64 * 1024, 128 * 1024, 200 * 1024,
             256 * 1024, 300 * 1024, 512 * 1024]
    boundary = None
    for sz in sizes:
        try:
            body = part_for(peer, ctx, sz)
            st, ln, verdict, srv = raw_send(host, body, cookies)
            log(f"  part={sz:>7}B  wire={len(body):>7}B -> HTTP {st} ({ln}B)  {verdict}")
            if "✅" not in verdict and boundary is None:
                boundary = sz
        except Exception as exc:  # noqa: BLE001
            log(f"  part={sz:>7}B -> EXC {type(exc).__name__}: {exc}")
            if boundary is None:
                boundary = sz
    log(f"  >> first failing size: {boundary}")

    # 2) keep-alive: 3 small parts reused on ONE connection
    log("")
    log("===== 2) KEEP-ALIVE (3 small parts, one connection) =====")
    try:
        conn = http.client.HTTPSConnection(host, 443, timeout=20,
                                           context=ssl.create_default_context())
        for i in range(3):
            body = part_for(peer, ctx, 16 * 1024)
            st, ln, verdict, srv = raw_send(host, body, cookies, keepalive_conn=conn)
            log(f"  reuse #{i+1}: HTTP {st} ({ln}B)  {verdict}")
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log(f"  keep-alive EXC: {type(exc).__name__}: {exc}")

    # 3) project HttpTransport with a 300KB part (reproduces the failure?)
    log("")
    log("===== 3) HttpTransport (keep-alive) 300KB part =====")
    try:
        tx = HttpTransport(f"https://{host}/eitaa/", timeout=30.0, cookies=dict(cookies))
        body = part_for(peer, ctx, 300 * 1024)
        resp = tx.post(body)
        rb = resp
        try:
            if resp[:4] == bytes.fromhex("ed77be7a"):
                rb = unwrap_eitaa(resp)["body"]
        except Exception:  # noqa: BLE001
            pass
        log(f"  HttpTransport 300KB -> {len(resp)}B  {classify(200, rb)}")
        tx.close()
    except Exception as exc:  # noqa: BLE001
        log(f"  HttpTransport 300KB -> EXC {type(exc).__name__}: {exc}")

    # 4) host compare with a 300KB part
    log("")
    log("===== 4) HOST COMPARE (300KB, fresh close) =====")
    for h in (host, "majid.eitaa.com", "fateme.eitaa.com"):
        try:
            body = part_for(peer, ctx, 300 * 1024)
            st, ln, verdict, srv = raw_send(h, body, cookies)
            log(f"  {h:20} -> HTTP {st} ({ln}B)  {verdict}  server={srv}")
        except Exception as exc:  # noqa: BLE001
            log(f"  {h:20} -> EXC {type(exc).__name__}: {exc}")

    log("")
    log("================= READ ME =================")
    log("  compare section 1 (close) vs 3 (keep-alive) at 300KB, and the size")
    log("  boundary in section 1. That isolates size-limit vs keep-alive vs host.")
    log(f"log saved: {LOGFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
