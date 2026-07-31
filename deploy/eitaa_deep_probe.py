#!/usr/bin/env python3
"""Eitaa DEEP probe - discover where/why the media (file-upload) path breaks.

The normal diagnostic proved the upload host `fateme` returns a GENERIC nginx
500, and that host was only a DEFAULT fallback (the capture had no upload
traffic to learn the real one from). This tool goes deep:

  A. CAPTURE FORENSICS  - every request the account actually made: host, path,
     decoded MTProto method name; flags any media call; lists distinct hosts.
  B. help.getConfig     - asks Eitaa itself for its DC/host list on the working
     API host, and surfaces every host/ip string inside the reply.
  C. UPLOAD HOST MATRIX - sends ONE real saveFilePart to every candidate host x
     path and classifies the reply: real-Eitaa vs nginx-html vs blocked. This
     empirically finds the host that actually accepts uploads.
  D. VERDICT            - the host(s) that accepted a part.

Read-only: sends only tiny throwaway parts (8-64 bytes) that are never
finalised with sendMedia, so nothing is delivered anywhere. Writes only a log.

Run on the SERVER:
    cd ~/Mkwlsoso && .venv/bin/python deploy/eitaa_deep_probe.py --account 989132531349
"""
from __future__ import annotations

import argparse
import http.client
import os
import ssl
import sys
import time
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("MKWL_ENABLE_DIRECT", "1")

from direct import eitaa_tl as E
from direct import schema as S
from direct import tl
from direct.transport import wrap_eitaa, unwrap_eitaa
from direct.sender import extract_api_url
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


def _method_name_map() -> dict:
    """id -> NAME, harvested from the project's own constants (auto-covers all)."""
    m = {}
    for mod in (E, S):
        for name, val in vars(mod).items():
            if isinstance(val, int) and name.isupper() and 0x1000 <= val <= 0xFFFFFFFF:
                m.setdefault(val, name)
    return m


def _printable_runs(data: bytes, minlen: int = 4):
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


def _classify(status: int, data: bytes) -> str:
    low = data[:400].lower()
    if b"<html" in low or b"nginx" in low:
        return "NGINX/HTML (wrong host or bad route)"
    if data[:4].hex() == "ed77be7a":
        return "EITAA ENVELOPE (REAL media host!)"
    # eitaa often replies bare TL: boolTrue/boolFalse/rpc_error
    head = data[:4].hex()
    if head in ("b5757299", "379779bc"):
        return f"EITAA bare TL {head} (REAL media host!)"
    if not data:
        return "EMPTY body"
    return f"OTHER (head={head})"


def raw_post(url: str, body: bytes, cookies: dict, timeout: float = 15.0) -> dict:
    u = urlparse(url)
    r = {"url": url, "status": None, "len": 0, "setcookie": None,
         "server": None, "klass": "", "snippet": "", "err": ""}
    try:
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout,
                                           context=ssl.create_default_context())
        h = {"Content-Type": "application/octet-stream",
             "Content-Length": str(len(body)), "Connection": "close"}
        if cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        conn.request("POST", u.path or "/", body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        r["status"] = resp.status
        r["len"] = len(data)
        r["setcookie"] = resp.getheader("Set-Cookie")
        r["server"] = resp.getheader("Server")
        r["klass"] = _classify(resp.status, data)
        try:
            r["snippet"] = data[:120].decode("utf-8").replace("\n", " ")
        except Exception:  # noqa: BLE001
            r["snippet"] = data[:60].hex()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        r["err"] = f"{type(exc).__name__}: {exc}"
    return r


def section_capture_forensics(cap, names) -> set:
    log("")
    log("========== A. CAPTURE FORENSICS ==========")
    hosts = set()
    media_hosts = set()
    total = 0
    try:
        for rec in E._iter_records(cap):
            if rec.get("kind") not in ("fetch", "xhr"):
                continue
            total += 1
            url = rec.get("url") or ""
            host = urlparse(url).hostname or "?"
            hosts.add(host)
            head = E._head_hex(rec)
            method = "?"
            if head.startswith("ed77be7a"):
                try:
                    body = unwrap_eitaa(bytes.fromhex(head))["body"]
                    mid = int.from_bytes(body[:4], "little", signed=False)
                    method = names.get(mid, f"0x{mid:08x}")
                    if mid in (E.SAVE_FILE_PART, E.SEND_MEDIA):
                        media_hosts.add(host)
                except Exception:  # noqa: BLE001
                    method = "(unwrap failed)"
            log(f"  {host:22} {urlparse(url).path:10} {method}")
        log(f"  ---- {total} request(s); distinct hosts: {sorted(hosts)}")
        if media_hosts:
            log(f"  ★ MEDIA host(s) seen in capture: {sorted(media_hosts)}")
        else:
            log("  ⚠ NO saveFilePart/sendMedia in capture -> real media host unknown")
    except Exception as exc:  # noqa: BLE001
        log(f"  forensics error: {exc}")
    return hosts | media_hosts


def section_getconfig(api_url, ctx, cookies, names) -> set:
    log("")
    log("========== B. help.getConfig (ask Eitaa for its hosts) ==========")
    found = set()
    try:
        body = S.help_get_config()
        raw = wrap_eitaa(ctx["token1"], ctx["token2"], body)
        r = raw_post(api_url, raw, cookies, timeout=20)
        log(f"  POST {api_url} -> HTTP {r['status']} ({r['len']}B) [{r['klass']}]")
        if r["err"]:
            log(f"  error: {r['err']}")
            return found
        # re-fetch bytes to parse (raw_post only kept a snippet)
        u = urlparse(api_url)
        conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=20,
                                           context=ssl.create_default_context())
        hh = {"Content-Type": "application/octet-stream",
              "Content-Length": str(len(raw)), "Connection": "close"}
        if cookies:
            hh["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        conn.request("POST", u.path or "/", body=raw, headers=hh)
        data = conn.getresponse().read()
        conn.close()
        inner = data
        try:
            inner = unwrap_eitaa(data)["body"]
        except Exception:  # noqa: BLE001
            pass
        strings = _printable_runs(inner, 4)
        hostish = [s for s in strings
                   if "eitaa" in s or ".ir" in s or ".com" in s
                   or s.count(".") == 3 or "http" in s]
        log(f"  decoded {len(strings)} ascii run(s); host/ip-like:")
        for s in sorted(set(hostish)):
            log(f"    • {s}")
            found.add(s)
        if not hostish:
            log(f"  (no host strings; raw head: {inner[:80].hex()})")
    except Exception as exc:  # noqa: BLE001
        log(f"  getConfig error: {type(exc).__name__}: {exc}")
    return found


def section_matrix(ctx, cookies, extra_hosts) -> list:
    log("")
    log("========== C. UPLOAD HOST MATRIX (one saveFilePart each) ==========")
    peer = E.input_peer_self(ctx["user_id"], 0)
    plan = E.build_file_send(peer, b"MKWLDIAG" * 4, "probe.bin",
                             ctx["user_id"], caption="", mime="application/octet-stream")
    part = plan["parts"][0]
    body = wrap_eitaa(ctx["token1"], ctx["token2"], part)

    base = ["majid.eitaa.com", "fateme.eitaa.com", "hadi.eitaa.com", "vahid.eitaa.com",
            "x.eitaa.com", "majid.eitaa.ir", "hadi.eitaa.ir", "vahid.eitaa.ir",
            "ghasem.eitaa.ir", "hossein.eitaa.ir", "bagher.eitaa.ir"]
    for h in extra_hosts:
        if h and h not in base and "eitaa" in h:
            base.append(h)

    hits = []
    for host in base:
        for path in ("/eitaa/", "/"):
            url = f"https://{host}{path}"
            r = raw_post(url, body, cookies, timeout=12)
            tag = r["klass"] or r["err"]
            log(f"  {host:20}{path:8} -> HTTP {r['status']} {str(r['len'])+'B':>7}  {tag}")
            if r["setcookie"]:
                log(f"        Set-Cookie: {r['setcookie'][:80]}")
            if "REAL media host" in (r["klass"] or ""):
                hits.append((host, path, r))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    args = ap.parse_args()

    global LOGFILE
    LOGFILE = os.path.join(_ROOT, f"eitaa_deep_{int(time.time())}.log")
    log(f"EITAA DEEP PROBE  account={args.account}  log={LOGFILE}")

    path, cap = _newest_capture(args.account)
    if not cap:
        log("✗ no capture; log in first")
        return 1
    log(f"capture: {path}")
    ctx = E.extract_context(cap)
    cookies = _load_cookies(args.account)
    api_url = extract_api_url(cap) or "https://majid.eitaa.com/eitaa/"
    log(f"user_id={ctx.get('user_id')}  cookies={len(cookies)}  api_url={api_url}")

    names = _method_name_map()
    seen_hosts = section_capture_forensics(cap, names)
    cfg_hosts = section_getconfig(api_url, ctx, cookies, names)
    extra = {h for h in seen_hosts if h}
    for s in cfg_hosts:
        s = s.strip().strip("/")
        if "eitaa" in s and "/" not in s and " " not in s:
            extra.add(s)
    hits = section_matrix(ctx, cookies, extra)

    log("")
    log("================= D. VERDICT =================")
    if hits:
        log("  ✅ host(s) that ACCEPTED a saveFilePart (real media host):")
        for host, p, r in hits:
            log(f"     https://{host}{p}   (HTTP {r['status']}, {r['klass']})")
        log("  -> set MKWL_DIRECT_MEDIA_URL to that URL, or teach the sender to use it.")
    else:
        log("  ❌ NO host accepted the upload. Either the token is API-host-only,")
        log("     or uploads need a browser-established sticky session first.")
        log("     Next: run a real browser file-send WITH capture, then re-read")
        log("     extract_media_url from that fresh capture.")
    log("=============================================")
    log(f"log saved: {LOGFILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
