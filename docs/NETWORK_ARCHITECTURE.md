# Network architecture: reaching Eitaa from a foreign host

Eitaa does not answer foreign IPs. The bot needs Telegram (blocked from Iran) and
Eitaa (blocked from abroad) at the same time. This document records the decision.

## The three egress points

The whole codebase touches the network in exactly three places, and all three
support a proxy natively. No rewrite is needed to route traffic.

| # | Location | Library | Proxy hook |
|---|---|---|---|
| 1 | `direct/transport.py:130` `_new_conn()` | `http.client` (stdlib) | `conn.set_tunnel(host, port)` (HTTP CONNECT) |
| 2 | `capture/browser.py:192` `_launch()` | Playwright | `proxy={"server": ...}` |
| 3 | `bot/app.py:165` `TelegramClient(...)` | Telethon | `proxy=(socks.SOCKS5, host, port, True, user, pass)` |

## Decision: run everything on the Iranian host, proxy only Telegram

**Server A (Iran)** runs the entire bot. **Server B (abroad)** is a plain
SOCKS5/MTProxy with no code on it.

Rationale:

1. **A's closed inbound ports are irrelevant.** Telethon dials *out* to Telegram
   DCs; nothing ever connects to A. So no reverse tunnel is required, which
   removes the tunnel config, the tunnel watchdog, and the MSS clamp entirely.
2. **Only Telegram must leave Iran.** Eitaa traffic goes out of A directly with
   the Iranian IP: zero added latency on the hot path. This matters — measured
   Eitaa RTT is already ~1.9s and ~6.2s per message.
3. **The control channel is tiny.** Telegram API traffic is a few KB. Tunnelling
   that is cheap; tunnelling the automation is not.
4. **Chromium stays on A.** If headless ran abroad, the entire Eitaa web app
   download would cross the tunnel. On 1 core with 30-89% steal that is fatal.
5. **State stays in one place.** All state is local files — `profiles/`,
   `artifacts/sessions/*.json`, `data/contacts_*.json`, `data/peers_*.json`,
   `blocked_store`, `progress_store`, `settings.json`. Splitting logic across
   A and B would need a new RPC layer, and every `LiveCard` edit would cross it.

### Rejected alternative: logic on A, thin Telegram controller on B

Correct on paper, expensive here. B would need an RPC to A for every panel tap,
plus auth, queueing and replay — a new subsystem rather than a setting.

| | Controller on B | Everything on A |
|---|---|---|
| Reverse tunnel | required | **not needed** |
| Code on B | controller + RPC | **none** |
| MTU / MSS clamp | required | **not applicable** |
| Tunnel watchdog | required | **not needed** |
| Eitaa path latency | + tunnel | direct |
| State | split / RPC | single host |
| Code change | new subsystem | 3 arguments |

If SOCKS5 on 443 is filtered, fall back to **MTProxy**, which Telethon supports
directly (`ConnectionTcpMTProxyRandomizedIntermediate`). Still no reverse tunnel.

## Planned shape inside the bot

`bot/net.py` — one module that resolves a proxy policy per destination:

```
telegram -> proxy (B)      the only thing that gets proxied
eitaa    -> direct from A  must keep the Iranian IP
```

All three egress points read from it. `data/net.json` holds the proxy list and
the last known good entry, with automatic failover: Telethon drops -> next proxy
-> reconnect. The session file stays valid, so the login is not lost.

Panel section `NETWORK` on Home:

| Row | Shows |
|---|---|
| Telegram path | active proxy, RTT, last failover |
| Eitaa path | RTT of a **real API call**, not a TCP connect |
| Egress IP | as Eitaa sees it; warns when it is not Iranian |
| Buttons | `Test Now`, `Switch Proxy` |

## Health checking

`bot/app.py:141` `_ping_blocking()` currently only opens a TCP socket to
`PING_HOST` (`majid.eitaa.com`). That stays green when the envelope token has
expired or the load balancer node changed, so it is process liveness, not
service health.

Real probes:

- **Eitaa** — a `direct-replay`-style call. It returns Eitaa's real DC config and
  therefore proves token, envelope wrapping and host routing together.
- **Telegram** — `bot.get_me()`.

The bot already runs under systemd (`mkwlsoso-bot`), so the health loop belongs
in the existing async event loop with self-healing. No separate watchdog service.

## Queue / concurrency for headless — already built

`capture/pool.py` is the standby session pool. On a 1 GB host keep:

```
MKWL_SESSION_POOL=1
MKWL_POOL_MAX_OPEN=1        do not raise this on 961 MB
MKWL_POOL_IDLE_TTL=240
MKWL_POOL_MAX_USES=25
MKWL_POOL_MAX_AGE=1800
SEND_CONCURRENCY=1
```

`bot/cards.py:707` reports `saved_launches`. With Chromium taking 158-203s to
start, `MAX_OPEN=1` is already the queue.

## Fallback only: reverse tunnel

Use this only if the bot cannot run on A at all.

Pick **rathole**:

| Tool | Why not |
|---|---|
| WireGuard | UDP, commonly throttled in Iran; full L3 tunnel widens the attack surface and still needs an MSS clamp |
| `ssh -R` | no real reconnect (needs autossh), head-of-line blocking on one connection, slowest |
| frp | Go, larger RSS, features not needed here |
| **rathole** | Rust, ~2-3 MB RSS (matters on 961 MB), native TLS on 443, built-in backoff |

Even then, do not build an RPC: tunnel a local SOCKS5 from A to B and set the
three proxy arguments on B. In that case the MSS clamp *is* required —
path MTU 1378, so clamp to 1338 — otherwise large upload responses are lost
silently.

## Measured: the foreign host is blackholed

Probed from the newly bought foreign server, 2026-07-30:

| Target | Result |
|---|---|
| `web.eitaa.com:443` | no TCP handshake, 12s timeout (`time_connect` 0.000s) |
| `majid.eitaa.com:443` | no TCP handshake, 12s timeout |
| ICMP to `majid.eitaa.com` | 100% packet loss |
| Egress IP | `217.60.249.84` — United States, CGI GLOBAL LIMITED |

Silently dropped, not refused: there is no connection to retry or tune. Eitaa
traffic must originate from the Iranian host. This confirms the plan above.

## Status

Diagnosis confirmed. Plan not yet implemented; next step is to verify from the
Iranian host that Eitaa is reachable, that Telegram is not, and that outbound
443 to the foreign host works.
