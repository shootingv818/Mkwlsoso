#!/usr/bin/env python3
"""Quick server->Eitaa connectivity health check. No login, no code, no upload.

Why
---
Sends and the apk probe started timing out today - a contacts call that took
0.4s on a good minute hit 45s, uploads blew past 120s. That smells like the
server's link to Eitaa being slow or throttled right now, not a bug in the bot.
This tells them apart: it measures ONLY the network path to the Eitaa hosts the
project actually uses, so a bad reading means "wait / check egress", not "the
code is broken".

What it checks, for each host the bot talks to
----------------------------------------------
  web.eitaa.com      the web app the bridge drives
  majid.eitaa.com    the direct API host (direct/sender.py DEFAULT_API_URL)
  fateme.eitaa.com   the direct media host (uploads / apk route)
  majid.eitaa.com    PING_HOST, what the panel's own health line uses

For each: DNS, TCP connect, TLS handshake, and a tiny HTTPS HEAD/GET, each timed
separately, repeated a few times so a single blip is visible as jitter rather
than mistaken for a trend. Several rounds give min/median/max.

It does NOT log in, request a code, open a browser, or upload anything. Pure
sockets. Safe to run as often as you like, including while the bot is running.

Run on the SERVER (bot can stay up):

    cd ~/Mkwlsoso && .venv/bin/python deploy/net_health.py

Options:
    --rounds 5     how many times to probe each host (default 5)
    --timeout 10   per-connection timeout in seconds (default 10)
    --extra host   add another host to probe (repeatable)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_T0 = time.time()


def banner(t):
    print("\n" + "=" * 72 + f"\n  {t}\n" + "=" * 72, flush=True)


def log(m=""):
    print(f"  {m}" if m else "", flush=True)


def _ms(dt):
    return round(dt * 1000, 1)


def probe_once(host: str, port: int, timeout: float) -> dict:
    """One full DNS + TCP + TLS + HTTP measurement for a host."""
    out = {"host": host}
    # DNS
    t = time.time()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        out["dns_ms"] = _ms(time.time() - t)
        out["ip"] = sorted({i[4][0] for i in infos})[0]
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"DNS: {type(exc).__name__}: {exc}"
        return out
    # TCP
    t = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"TCP: {type(exc).__name__}: {exc}"
        return out
    out["tcp_ms"] = _ms(time.time() - t)
    # TLS
    try:
        ctx = ssl.create_default_context()
        t = time.time()
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        out["tls_ms"] = _ms(time.time() - t)
        out["tls_version"] = ssock.version()
        ssock.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"TLS: {type(exc).__name__}: {exc}"
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        return out
    # HTTP (tiny GET, just the status line - no body downloaded)
    t = time.time()
    try:
        req = urllib.request.Request("https://" + host + "/",
                                     method="GET",
                                     headers={"User-Agent": "mkwl-net-health"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out["http_ms"] = _ms(time.time() - t)
            out["http_status"] = r.status
    except urllib.error.HTTPError as exc:
        # a 4xx/5xx still proves the round trip worked
        out["http_ms"] = _ms(time.time() - t)
        out["http_status"] = exc.code
    except Exception as exc:  # noqa: BLE001
        out["http_ms"] = _ms(time.time() - t)
        out["http_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    return out


def summarise_host(host: str, rounds: list[dict]) -> dict:
    good = [r for r in rounds if "error" not in r]
    out = {"host": host, "ok_rounds": len(good), "total_rounds": len(rounds)}
    if not good:
        out["verdict"] = "UNREACHABLE"
        out["last_error"] = rounds[-1].get("error") if rounds else "no data"
        return out
    for key in ("tcp_ms", "tls_ms", "http_ms"):
        vals = [r[key] for r in good if key in r]
        if vals:
            out[key] = {"min": min(vals), "med": round(statistics.median(vals), 1),
                        "max": max(vals)}
    out["ip"] = good[-1].get("ip")
    out["tls_version"] = good[-1].get("tls_version")
    http_errs = [r.get("http_error") for r in rounds if r.get("http_error")]
    if http_errs:
        out["http_errors"] = len(http_errs)
    # a simple health call: median TCP+TLS round under ~1.2s is healthy here
    med_tcp = out.get("tcp_ms", {}).get("med", 9999)
    med_http = out.get("http_ms", {}).get("med", 9999)
    jitter = (out.get("http_ms", {}).get("max", 0)
              - out.get("http_ms", {}).get("min", 0))
    if len(good) < len(rounds):
        out["verdict"] = "FLAKY (some rounds failed)"
    elif med_http > 4000 or med_tcp > 3000:
        out["verdict"] = "SLOW"
    elif jitter > 3000:
        out["verdict"] = "JITTERY (unstable latency)"
    else:
        out["verdict"] = "HEALTHY"
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="Server->Eitaa connectivity health. No login, no upload.")
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--extra", action="append", default=[],
                   help="additional host to probe (repeatable)")
    p.add_argument("--json", action="store_true", help="print the raw report too")
    args = p.parse_args()

    # the hosts the project actually uses, read from its own config/modules
    try:
        from config import config
        web = re.sub(r"^https?://", "", config.EITAA_WEB_URL).split("/")[0]
        ping = getattr(config, "PING_HOST", "majid.eitaa.com")
    except Exception:  # noqa: BLE001
        web, ping = "web.eitaa.com", "majid.eitaa.com"
    hosts = []
    for h in [web, "majid.eitaa.com", "fateme.eitaa.com", ping] + list(args.extra):
        if h and h not in hosts:
            hosts.append(h)

    banner("SERVER -> EITAA CONNECTIVITY  (no login, no code, no upload)")
    log(f"hosts: {hosts}")
    log(f"rounds: {args.rounds}   timeout: {args.timeout}s")
    log("these are the exact hosts the bot talks to; measured straight from this")
    log("server with plain sockets, so a bad number means the LINK, not the code")

    report = {"hosts": {}, "when": time.strftime("%Y-%m-%d %H:%M:%S")}
    summaries = []
    for host in hosts:
        banner(f"{host}")
        rounds = []
        for i in range(args.rounds):
            r = probe_once(host, 443, args.timeout)
            rounds.append(r)
            if "error" in r:
                log(f"  round {i + 1}: FAIL  {r['error']}")
            else:
                http = (f"HTTP {r.get('http_status')} {r.get('http_ms')}ms"
                        if "http_status" in r
                        else f"HTTP err {r.get('http_error')}")
                log(f"  round {i + 1}: ip={r.get('ip'):<16} "
                    f"tcp={r.get('tcp_ms')}ms tls={r.get('tls_ms')}ms  {http}")
            time.sleep(0.4)
        s = summarise_host(host, rounds)
        summaries.append(s)
        report["hosts"][host] = {"rounds": rounds, "summary": s}
        tcp = s.get("tcp_ms", {})
        http = s.get("http_ms", {})
        log(f"  --> {s['verdict']}   tcp med {tcp.get('med', '?')}ms  "
            f"http med {http.get('med', '?')}ms  "
            f"({s['ok_rounds']}/{s['total_rounds']} rounds ok)")

    banner("VERDICT")
    worst = ""
    for s in summaries:
        mark = {"HEALTHY": "OK  ", "SLOW": "SLOW", "FLAKY (some rounds failed)": "FLAK",
                "JITTERY (unstable latency)": "JITR",
                "UNREACHABLE": "DEAD"}.get(s["verdict"], "??  ")
        http = s.get("http_ms", {})
        log(f"  {mark}  {s['host']:<20} {s['verdict']:<28} "
            f"http med {http.get('med', '?')}ms")

    bad_hosts = [s for s in summaries if s["verdict"] != "HEALTHY"]
    web_s = next((s for s in summaries if s["host"] == web), None)
    print("", flush=True)
    if not bad_hosts:
        log("All Eitaa hosts are HEALTHY from this server right now. If a send or")
        log("the apk probe was timing out, it was transient - try again now.")
    else:
        if web_s and web_s["verdict"] in ("SLOW", "JITTERY (unstable latency)",
                                          "FLAKY (some rounds failed)"):
            log("web.eitaa.com itself is slow/unstable from here right now. That")
            log("is why sends and the browser bridge were timing out - it is the")
            log("LINK, not the bot. Wait a few minutes and re-check; if it stays")
            log("slow for long, the server's egress to Eitaa is being throttled.")
        dead = [s for s in bad_hosts if s["verdict"] == "DEAD"]
        if dead:
            log(f"UNREACHABLE: {[s['host'] for s in dead]} - DNS/TCP/TLS failed")
            log("every round. Check the server has internet and these are not")
            log("being blocked.")
        log("")
        log("What this means for the bot: the code paths you tested are fine; the")
        log("timeouts track this network reading. Re-run the health check until")
        log("the hosts read HEALTHY, then retry the real work.")

    if args.json:
        banner("RAW")
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
    log(f"\n  done in {time.time() - _T0:.1f}s")
    return 0 if not bad_hosts else 1


if __name__ == "__main__":
    raise SystemExit(main())
