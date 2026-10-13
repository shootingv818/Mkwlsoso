# Mkwlsoso — Roadmap

**Canonical, forward-looking roadmap.** Updated 2026-08-13 against branch
`update-test` @ `caf8ce8` (133 tracked files, first commit 2026-07-20).

This file answers one question: *what is done, what is half-done, and what is
next.* It is the single place to update when priorities change.

The other documents are **records, not roadmaps** — they explain *how* something
works or *why* a decision was made, and should not be consulted for "what's left":

| Document | Scope |
|---|---|
| `PROJECT_STATUS.md` | Reverse-engineering log + the dated history of every change and live test |
| `docs/NETWORK_ARCHITECTURE.md` | Egress/hosting decision record (Iran vs foreign host, proxying) |
| `docs/APK_SEND_STATUS.md` | The `.apk` MIME investigation, root cause and fix |
| `bot/README.md` | Panel reference — what each button does |
| `direct/README.md` | Browser-free client reference — protocol, commands, artifacts |

---

## Status legend

| Mark | Meaning |
|---|---|
| ✅ | Shipped **and** verified live on the server |
| 🧪 | Built, offline-tested, **not yet live-verified** |
| 🧱 | Scaffold — code exists, is not wired into the product |
| ⛔ | Settled dead end — do not re-investigate |

---

## 1) Where the project stands

Mkwlsoso is an owner-only **Telegram control panel** (`bot/`, Telethon) that drives
**Eitaa** for the operator's own accounts: login, contact building, and bulk text/file
sending. It reaches Eitaa three ways behind one interface (`bot/transports.py`), and
everything experimental is isolated in a folder that can be deleted to revert it.

The core product — panel, login, contact cache, send loop with resume, error cards —
is **live and working** on a US server. The newest three subsystems (web login
**portal**, central **log group**, distributed **worker fleet**) are the frontier: the
first two are wired and off by default, the third is not wired at all.

**Subsystem status at a glance**

| Subsystem | Where | Status |
|---|---|---|
| Telegram panel, jobs, live cards | `bot/app.py`, `bot/runner.py`, `bot/cards.py` | ✅ |
| `bridge` engine (drives the web app) | `eitaa/driver.py` | ✅ default, the proven path |
| `hybrid` engine (HTTPS + page fallback) | `bot/transports.py` | ✅ |
| `direct` engine (pure browser-free) | `direct/` | ✅ text + file · gated behind `MKWL_ENABLE_DIRECT=1` |
| Contact cache + peer/`access_hash` reuse | `bot/contacts_store.py`, `direct/peers.py` | ✅ |
| Resume ledger / refused-recipient memory | `bot/progress_store.py`, `bot/blocked_store.py` | ✅ |
| Warm browser standby pool | `capture/pool.py` | ✅ (ceiling 1 by default) |
| APK send mode (octet MIME) | `direct/apk_mode.py` | ✅ proven live, toggle OFF by default |
| Multi-account parallel send | `bot/runner.py` `run_send_multi` | ✅ width 1–2 |
| Session check | `session_check/checker.py` | ✅ |
| Photo export (read-only → PDF) | `photo_export/` | 🧪 |
| Contact Boost (number probing) | `contacts_boost/` | 🧪 needs `MKWL_BOOST_PREFIX` |
| Warm Path (skip re-navigation) | `eitaa/warmpath.py` | 🧪 OFF by default |
| Web login portal + tunnel | `portal/` | 🧪 OFF by default, FastAPI layer untested |
| Central log group (logbus) | `bot/logbus.py` | 🧪 inert until a group id is set |
| Worker fleet — registry/selection/health | `worker/store.py`, `selection.py`, `health.py` | 🧪 tested core, **unwired** |
| Worker fleet — SSH transport + agent | `worker/transport.py`, `worker/agent.py` | 🧱 |
| CLI campaign broadcaster | `jobs/campaign.py` | ✅ CLI only — the panel ignores its pacing |

---

## 2) Done — shipped and proven live

- **Eitaa's transport reverse-engineered byte-exactly** — the `ed77be7a` envelope,
  token-based auth, bare TL layer 135, no AES/auth_key. Serializers reproduce real
  captured bytes in unit tests. Details: `PROJECT_STATUS.md` §2.
- **Three engines behind one interface** (`bot/transports.py`), selected in one place
  (`effective_engine()`, `bot/runner.py:158-174`), so a stale setting can never route
  work somewhere unexpected.
- **Browser-free text send** and **multi-part file send** — a 9,494,529 B zip went out
  as 19 parts, uploaded once, to the dedicated media host `fateme.eitaa.com`.
- **`.apk` sending fixed** — Eitaa filters the *MIME*, not the name or the bytes.
  Sending as `application/octet-stream` with the real name in
  `documentAttributeFilename` delivers. Proven live at 11/11 recipients, 2.71 msg/s.
- **Contacts cached once** instead of re-collected at the start of every run, which used
  to cost minutes per send.
- **`access_hash` reuse** — it was being discarded twice (in `contacts_bridge.js` and in
  peer resolution). Persisting it is what made browser-free sending to arbitrary
  contacts possible.
- **Refused recipients remembered** — measured 12 contacts → 6 delivered, 6 `PEER_FLOOD`,
  split per *recipient* and not time-based, so they are skipped on later runs. Timed
  `FLOOD_WAIT_n` is never treated this way.
- **The panel no longer blocks sending** — `LiveCard` stashes the newest text and a
  background painter delivers it, instead of awaiting a Telegram edit inside the loop.
- **Every run reports where its time went** — `⏱ RUN TIMING` splits total into
  sending / slow path / pacing / other, plus achieved msg/s.
- **Measured rate model:** `rate = concurrency / (RTT + delay)`. Consequence worth
  remembering: `conc=3, delay=3` is *slower* than `conc=1, delay=1` — the delay and the
  round trip add up, they do not overlap.

---

## 3) Built, offline-tested, awaiting a live pass

Each of these is **off or inert by default**; that default is the rollback.

1. **Web login portal** (`portal/`) — FastAPI + cloudflared tunnel, an Eitaa-branded
   page, per-attempt hmac ownership tokens, capacity/queue position, durable attempt
   stats. Drives the *same* warm-pool browser login as the panel.
   - Enable: `MKWL_PORTAL_ENABLED=1`, plus `pip install fastapi uvicorn httpx` and a
     `cloudflared` binary — **none of which are in `requirements.txt`** (see §6).
   - Untested layers: `portal/app.py`, `portal/net.py`, `portal/page.py`. The logic
     underneath them *is* tested (`test_portal.py`, `test_portal_login.py`).
   - Live checks to run: a real attempt end-to-end over the tunnel; the 350s TTL
     expiring cleanly; a refresh/re-click producing a genuinely fresh session; two
     attempts at once hitting the capacity queue.
2. **Central log group** (`bot/logbus.py`) — mirrors send completions and portal logins
   to a Telegram group. Wired at `bot/runner.py` (both send paths) and
   `portal/login_adapter.py`. Inert until `MKWL_LOG_GROUP_ID` is set; never raises.
   - Live check: set a group id, run one send and one portal login, confirm both arrive
     and that a *broken* group id still cannot break a job.
3. **Warm Path** (`eitaa/warmpath.py`, `MKWL_WARMPATH=0`) — reuse the booted page instead
   of re-navigating, and skip the ~98s `getDialogs` PV count. Its explicit test invariant
   is "OFF == the old bot".
   - Live check: measure a send with it ON vs OFF and confirm the saved time is real.
4. **Contact Boost** (`contacts_boost/`, `MKWL_BOOST=0`) — probe blocks of numbers via
   `contacts.importContacts`, per-account blocks so accounts never share numbers, random
   picking within a prefix, cursor persisted after every batch, waits out FLOOD instead
   of aborting. Cannot run until `MKWL_BOOST_PREFIX` is set even when toggled on.
5. **Photo export** (`photo_export/`) — read-only; renders one photo per PDF page via
   headless Chromium. Its tests encode the real paging traps (`getDialogs` returning 25
   then 100, `messages.search` with a null `count`).
6. **Live-tunable warm-browser ceiling** — `MKWL_POOL_MAX_OPEN` / panel toggle, so the
   parallelism can be raised on a bigger host without a restart. Only `set_max_open()`
   clamping and `status()` are tested; raising it on a real host is unverified.

---

## 4) Scaffolds — the worker fleet

The intent: move browser work onto additional servers, keeping one Telegram panel.

**Tested core** — `worker/store.py` (JSON registry + account→worker affinity map, tags,
capacity), `worker/selection.py` (affinity, least-loaded round-robin, failover),
`worker/health.py` (TCP + API probes, backoff to 10 min). Covered by `test_worker.py`.

**Scaffolds** — `worker/transport.py` (SSH local port-forward via `asyncssh`) and
`worker/agent.py` (worker-side FastAPI: `/ping`, `/health`, `/login/*`, `/send/*`, bearer
token, loopback bind). Self-labelled "SCAFFOLD / OFF BY DEFAULT", not unit-tested.

**The honest gap:** `worker/` is **not referenced anywhere outside its own tests** —
verified by grepping for `import worker` across the repo, which matches only
`bot/tests/test_worker.py`. There is no panel screen, no routing in `run_send`, and no
health loop. `MKWL_MASTER_AS_WORKER=1` (the default) means everything runs in-process
exactly as today.

Remaining work, in order: a second real server → prove `transport.py` opens a tunnel and
`agent.py` answers → route one job family (login is the smallest) through a worker →
a panel screen → a health loop task → then the rest of the job families.

---

## 5) Settled dead ends — do not re-investigate

Kept so the same days are not spent twice. Full evidence in `PROJECT_STATUS.md` §5.

- ⛔ **Browser-free `contacts.importContacts`** — Eitaa answers with a 4-byte,
  payload-less reply `cid=0xdc252379`, identical for both `+98…` and `98…`, so phone
  format was never the cause. This endpoint does not serve contact import off the
  browser path. Contact building hands over to the bridge automatically.
- ⛔ **`.apk` with its real MIME** — `application/vnd.android.package-archive` is
  filtered by Eitaa. Not our bug; the fix is the octet MIME (§2), not more probing.
- ⛔ **Cookie / sticky-session theory for uploads** — `direct-capture-cookies` returned
  **0 cookies**; Eitaa auth is token/localStorage. The real fix was the media host.
- ⛔ **Keep-alive to pin a balancer node** — the balancer routes per request, not per TCP.
- ⛔ **Running the bot from a foreign host to reach Eitaa** — measured from a bought
  foreign server: no TCP handshake to `web.eitaa.com:443` or `majid.eitaa.com:443`, 100%
  ICMP loss, silently blackholed. Superseded in practice: the project now runs on a US
  server where Eitaa *is* reachable (~0.3–0.8s RTT), so the "proxy Telegram only" plan in
  `docs/NETWORK_ARCHITECTURE.md` is on hold rather than pending.
- ⛔ **The `direct` engine harvesting its own peers** — `contacts.importedContacts` ends
  in a `Vector<User>` whose row constructor is Eitaa-specific and unknown, so
  `parse_import_result()` deliberately stops at the safe rows. Reversing that row (from a
  real capture) is the only thing that would remove the last reason to open a browser for
  contacts — see §6 "Later".

---

## 6) Next

### Now — verify what is already built

Nothing here needs new code; it needs a live pass and a decision.

1. Portal end-to-end over the tunnel (the checks in §3.1).
2. Log group: point it at a real group, confirm sends and portal logins mirror.
3. Warm Path ON vs OFF, measured — then either make it the default or drop it.
4. Contact Boost with a real prefix on one account, watching the cursor persist.
5. Photo export on one account with many photos, watching pacing and provenance.

### Next — close the housekeeping gaps

6. **`requirements.txt` is incomplete.** It declares only `playwright` and `telethon`.
   `portal/` needs `fastapi`, `uvicorn`, `httpx`; `worker/` needs `asyncssh`. Every such
   import is lazy/try-guarded, so the features silently stay off instead of crashing —
   which is safe but makes "why is the portal not up?" hard to answer. Declare them as
   optional extras.
7. **Config drift:** `MKWL_PORTAL_TTL` is `600` in `.env.example:234` but `350` in
   `config.py:194` (commit `caf8ce8` shortened it). Same value should appear in both.
8. **`bot/README.md` is stale** — it still says "the panel is bridge-only, there is no
   Engine button", but the engine switch is back in Settings and cycles
   `bridge → hybrid → direct`. It also documents none of Portal / Log group / Boost /
   Photos / Session check / Pool ceiling, and its `## Files` list omits `transports.py`,
   `logbus.py`, `direct_ctx.py`, `blocked_store.py`, `progress_store.py`.
9. **Some knobs bypass `config.py`** and are read straight from `os.environ` in their
   module (`MKWL_SESSION_POOL`, `MKWL_POOL_IDLE_TTL/MAX_USES/MAX_AGE`,
   `MKWL_CONTEXT_MAX_AGE_H`, `MKWL_LIGHT_ASSETS`, `MKWL_UI_*_TIMEOUT`,
   `MKWL_LOGIN_SETTLE_TIMEOUT`, `MKWL_LEDGER_FLUSH`, `MKWL_MODE`, `MKWL_WORKER_*`), so
   there is no single place to read the effective configuration.

### Then — the worker fleet

10. The ordered plan in §4. This is the largest remaining piece of work and the one that
    changes the deployment shape, so it should not start before §3 is verified.

### Later — genuinely not built

11. **Resume/checkpoint for panel send jobs.** `jobs/state.py` is restart-safe but wired
    only to the CLI campaign; the panel has the per-content ledger
    (`bot/progress_store.py`) but not the CLI's job-level resume.
12. **Recipient selection and dedupe in the panel** — currently a run targets the cached
    contact list, minus refusals.
13. **Scheduling** and a **periodic batch cooldown** in the panel (the CLI campaign has
    `SEND_BATCH_SIZE`/`SEND_BATCH_COOLDOWN`; the panel ignores them by design).
14. **2FA on the portal path** — the login adapter handles a password step, but this has
    not been exercised live.
15. **Groups/channels, and receiving messages, on the direct path** — send-only today.
16. **Token sourcing.** The direct engine takes token1/token2 from the newest capture;
    sourcing them from the session export (localStorage `token`) would remove the
    staleness window that `MKWL_CONTEXT_MAX_AGE_H` currently papers over.
17. **Optional: a direct MTProto login handshake**, so `direct` needs no browser capture
    at all. Only worth it after 16.
18. **Optional: reverse the Eitaa-specific `User` row** (§5) — the last thing forcing a
    browser pass for contacts.
19. **Optional: retry on `locate_failed`** during browser upload; occasionally needs a
    second try due to balancer node routing. Not required for apk delivery, which works.

---

## 7) Verifying a change

There is no CI. The offline suites are the gate, they stub Playwright at import time, and
they touch neither a browser nor the network. All **16 pass** as of 2026-08-13 on
`update-test` (checked on Python 3.9 and 3.11):

```bash
for t in bot_logic send_loop engines scenarios multi_parallel contacts_boost \
         photo_export warmpath portal portal_login worker logbus live_card \
         login_settle pool; do python -m bot.tests.test_$t || echo "FAIL $t"; done
python -m direct.tests.test_direct
```

**Not covered by any test**, so these need a live check after every change that touches
them: the FastAPI/uvicorn/cloudflared layers (`portal/app.py`, `net.py`, `page.py`),
`worker/transport.py`, `worker/agent.py`, real Chromium/pool session opening, `cli.py`,
`jobs/campaign.py`, and everything under `deploy/`.

**Rollback points:** tags `baseline-before-hybrid`, `baseline-before-optimizations`,
`baseline-before-apkbridge`; and each isolated feature folder (`direct/`,
`contacts_boost/`, `photo_export/`, `session_check/`, `portal/`, `worker/`) can be deleted
to revert that feature, because every import of them is lazy.
