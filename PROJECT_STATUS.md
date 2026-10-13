# Mkwlsoso — Project Status & Handoff

Comprehensive record of the browser-free Eitaa Direct Client + the Telegram bot
v2 update. Read this first when continuing on a new session/account.

> **This file is the history log, not the roadmap.** For what is done / half-done /
> next, read **`docs/ROADMAP.md`** — it is the canonical roadmap. Keep adding dated
> entries here; keep priorities there.

- Repo: `shootingv818/Mkwlsoso`
- Base branch: `feat/eitaa-web-capture`
- Working branch: `update-test` (current). Earlier working branches, newest first:
  `feat/apk-bridge-fix`, `feat/warmpath-engine`, `feat/hybrid-engine-live`,
  `feat/fast-send-multi-account` — each still on the remote as a rollback point.
- Server: `~/Mkwlsoso`, venv `.venv`, `DISPLAY=:99`, runs via systemd `mkwlsoso-bot`
- The assistant CANNOT run live (sandbox has no Playwright/Telethon/network) — it
  only compiles + runs offline tests (`python -m direct.tests.test_direct`). The
  user runs everything live on the server and pastes output back.
- Secrets (tokens/cookies/peers/session) live ONLY in gitignored
  `artifacts/sessions/*.json` on the server — never in the repo or notes.

---

## 1) The big goal
Talk to Eitaa **without a browser** (kill the Chromium RAM/CPU cost and the
server crashes under load), and wire it into the Telegram control bot. Everything
experimental is isolated in `direct/` — deleting that folder reverts the project.

---

## 2) The Eitaa protocol we reverse-engineered (all evidence-based, byte-exact)

### Transport envelope (every POST to a shard host)
```
ed77be7a                     4-byte constant MAGIC
len1 (1B) | token1           ASCII routing token "9179.c756a2d10f.e41c4e_<userid>"
len2 (1B) | token2           ASCII session id    "mrtpgmi2y9fm222__web"
bodyLen (4B, BIG-endian)     payload length
body                         BARE, UN-ENCRYPTED Telegram TL (layer 135)
0000008700000020000000       11-byte constant trailer (layer=135, 32)
```
Key finding: **no AES / auth_key / msg_id** — auth is the token; HTTP is 1:1 so no
MTProto message container is needed. (`direct/transport.py` `wrap_eitaa`/`unwrap_eitaa`.)

### Hosts (per operation — CONFIRMED via `direct-inspect-capture`)
- Regular API (sendMessage, importContacts, getState): `bagher.eitaa.ir` / `majid.eitaa.com`
- **Media (saveFilePart + sendMedia): a dedicated media host — `fateme.eitaa.com`**
- Contact-photo downloads: `hadi.eitaa.com`
Media MUST go to the media host or the upload part isn't found (see failed test #2).

### Confirmed TL constructors (little-endian on wire)
| method | id |
|---|---|
| messages.sendMessage | `0x520c3870` — flags, peer, message, random_id |
| messages.sendMedia | `0x3491eba9` — flags, peer, media, message, random_id, entities |
| contacts.importContacts | `0x2c800be5` — Vector\<inputPhoneContact\> |
| inputPhoneContact | `0xf392b7f4` — client_id, phone, first, last |
| contacts.importedContacts (resp) | `0x77d01c3b` — first vector = imported |
| upload.saveFilePart | `0xb304a621` + **Eitaa 24B trailer** (flag=3, uploadPeer `0x59511722`+self_id, size) |
| inputMediaUploadedDocument | `0x5b38c6c1` |
| inputFile | `0xf52ff27f` — name pattern `document.<mime-subtype>` |
| documentAttributeFilename | `0x15590068` |
| vector | `0x1cb5c415` |
| eitaa_error (resp) | `0xc4b9f9bb` — code:int, message:string |
| self / Saved-Messages peer | inputPeerUser ctor `0xdde8a54c` + user_id:long + access_hash:long |

---

## 3) What we BUILT

### direct/ (browser-free client, all unit-tested byte-exact)
- `transport.py` — HTTPS POST with ONE persistent keep-alive connection + a
  cookie jar (send + absorb Set-Cookie); `wrap_eitaa`/`unwrap_eitaa`.
- `eitaa_tl.py` — serializers (`send_message`, `import_contacts`, `save_file_part_eitaa`,
  `input_file`, `input_media_uploaded_document`, `send_media`, `input_peer_self`,
  `build_file_send`), `extract_context` (token/self-peer from a capture),
  `extract_media_url`, `find_message_peer`, `parse_import_result`, `classify_response`.
- `tl.py`, `aes.py`, `crypto.py`, `mtproto.py`, `session.py`, `service.py`, `schema.py`, `dc.py`.
- Tests: `python -m direct.tests.test_direct` → ALL PASS (reproduces real captured
  sendMessage/importContacts/saveFilePart/sendMedia bytes byte-for-byte).

### CLI commands (on the server)
Capture (need `DISPLAY=:99`, browser):
```
python cli.py direct-capture-all      --account <a>        # text+file+contact wire bytes, per op
python cli.py direct-capture-peer     --account <a> --to "<contact>"   # learn a contact's peer
python cli.py direct-capture-cookies  --account <a>        # export eitaa cookies (returned 0 — no cookies)
python cli.py direct-inspect-capture  --account <a>        # READ-ONLY: host + TL method per request
```
Browser-free actions (no DISPLAY):
```
python cli.py direct-replay    --account <a>                       # prove transport (config)
python cli.py direct-send      --account <a> [--to "<c>"] --text "hi"
python cli.py direct-send-file --account <a> [--to "<c>"] --file f.zip --caption "..."
python cli.py direct-import    --account <a> --phone "+98..." --first Name
```

### FINAL SHAPE: the panel is BRIDGE-ONLY (2026-07-25)
After the live tests below, the panel was moved fully onto the proven browser
(bridge) path and the engine switch was REMOVED from the UI:
- `store.engine` always reports `bridge` unless `MKWL_ENABLE_DIRECT=1` is set in
  `.env`. The stored choice is never overwritten, so the flag restores it.
- `direct/` is untouched and its CLI commands still work. It is kept because the
  browser-free FILE send is proven live (9.5 MB zip, 19 parts, uploaded once);
  only browser-free CONTACT IMPORT is unsupported by Eitaa (dead-end below).
- Contact building auto-hands-over to the bridge if the direct path is ever used.
- **Contacts are now cached** (`bot/contacts_store.py` -> `DATA_DIR/contacts_<acct>.json`,
  title + peer_id only). Collecting Eitaa's virtualized contact list took minutes
  and was being redone at the start of EVERY send; now it is done once via
  📥 Save Contacts and later sends start delivering immediately.
- **Multi Send is its own top-level section** on Home (bridge-based), showing the
  combined reach before starting and reporting into ONE live card.
- UI reworked: progress bars, ETA, per-account saved-contact counts in every list,
  10 per page, and honest titles (a run with failures is never a green success).

### Fast send + multi-account panel (branch `feat/fast-send-multi-account`)
Built entirely by WIRING UP existing proven code — no new protocol work.

New isolated modules (deleting `direct/` still reverts everything):
- `direct/peers.py` — the peer store, now shared by the CLI and the bot. Same
  `peers_<account>.json` format as before, plus an `id:<user_id>` alias.
  `save_users()` / `targets()` / `resolve()` / `count()` / `forget()`.
- `direct/sender.py` — the long-running form of `direct-send` / `direct-send-file`:
  `DirectSender(account).send_text(peer, text)`, `upload_file()` once +
  `send_uploaded_file(peer, caption)` per recipient, `import_contacts(batch)`.
  Keep-alive per host, hosts taken from the account's own capture. Returns the
  same dict shape as the browser bridge.

**The unlock: `access_hash` was being thrown away twice.**
A browser-free send needs `user_id` + `access_hash`. Both were already on the wire:
- `contacts.importContacts` returns the matched users WITH their access_hash —
  `contacts_bridge.js` reduced it to `has_hash: true/false`.
- `appPeersManager.getInputPeerById` returns a real inputPeer — `RESOLVE_PEERS_JS`
  only reported whether a hash existed.
Both now return the value and it is persisted. So: build/collect contacts once with
the bridge engine, then the direct engine can send to all of them with no browser.

Wired up:
- `bot/runner.py` `run_send()` now ROUTES on the engine (mirroring `run_contacts`):
  `direct` → new `_send_job_direct` (browser-free, blocking calls in
  `asyncio.to_thread`), `bridge` → the unchanged proven path, which now also
  harvests peers.
- `run_send_multi()` + `AggregateProgress`: several accounts send SIMULTANEOUSLY and
  report into ONE live card with the COMBINED sent/total plus a per-account
  breakdown; a supervisor posts one final summary.
- `_contacts_job_direct` no longer hardcodes `bagher.eitaa.ir` and no longer blocks
  the event loop — it uses `DirectSender.import_contacts` in a thread.

Contact-build bug (raced through, built nothing, no reason given):
- Root cause candidate confirmed to be diagnosable, not guessed: the server matches
  nobody and returns NO error when the phone format is wrong. The first batch is now
  probed in BOTH formats (`98…` and `+98…`), the raw counts are posted as a
  **🔬 IMPORT PROBE** card, the winning format is used for the rest of the job, and
  if neither matches anyone the job falls back to the proven one-by-one UI add flow
  instead of reporting a silent zero. Rate limits abort rather than fall back.

Panel changes (see `bot/README.md`):
- Adding an account no longer asks for a name — the phone digits ARE the account
  (`0930…`/`930…`/`+98930…` all map to `98…`); duplicates are refused.
- The Accounts list shows ONLY the number, 10 per page with `◀ 1/3 ▶`.
- 🗑 Delete Account (with confirm) removes the browser profile, saved peers and
  captured session.
- 🚀 Multi-Account Send: tick accounts (10 per page) → send from all at once.
- The account panel shows a **Peers** count and warns when the direct engine has no
  targets.

### bot/ (Telegram panel v2) — see `bot/README.md`
- Engine selection in Settings (bridge/direct); Home shows engine + bot + server ping.
- Accounts listed by phone; tap a number → per-account panel (Send / Build Contacts /
  Refresh / Stop). Add Account separate; on-add shows contacts + chats.
- Live in-place log cards for contact-build and send ("🔎 Discover…/📤 Send… — Live").
- Imported contact name = the account's own phone. Batch contact building.
- Removed: noVNC add, Help, standalone Stats button, debug prints.
- Richer error cards: Trace/Where/Phase/Engine/Account/Target/Code/Detail/Time.

---

## 4) Tests — PASSED (live on the server)
- ✅ Transport proven: `direct-replay` returned Eitaa's real DC config.
- ✅ Browser-free **send text to self** — `updateShortSentMessage`.
- ✅ Browser-free **send text to a contact** (learned "علی" peer) — `updateShortSentMessage`.
- ✅ Browser-free **import contact** — `contacts.importedContacts`.
- ✅ Browser-free **learn contact peer** (`direct-capture-peer`).
- ✅ Browser-free **send file txt** and **zip**, to self AND to a contact — media result `0x74ae4240`.
- ✅ **MULTI-PART upload verified LIVE** (2026-07-25): a 9,494,529 B zip went out as
  **19 parts, uploaded ONCE**, to `fateme.eitaa.com`, then sent via `sendMedia`
  (`[dsend] path=direct sent=1 failed=0 of 1 (file)`). This closes the old
  ">512 KiB untested" TODO — the browser-free FILE path works at real size.
- ✅ All offline unit tests (`python -m direct.tests.test_direct`).

## 5) Tests — FAILED / dead-ends (and why) — keep so we don't repeat them
- ❌ **File upload to `majid` (API host)** → `INTERNAL_SERVER_ERROR "part key: 0 ..._<ip>"`.
  Cause: media must go to the **media host** (`fateme.eitaa.com`), not the API host.
  FIX: `extract_media_url` routes file ops to the media host. → now works.
- ❌ **Keep-alive single connection** did NOT fix the above — the balancer routes
  per-request, not per-TCP. (Superseded by the media-host fix.)
- ❌ **Cookie/sticky-session theory** — `direct-capture-cookies` returned **0 cookies**
  (Eitaa auth is token/localStorage, not cookies). Cookie jar kept as harmless
  belt-and-suspenders, but it was NOT the fix. The media host was.
- ❌ **Negative `file_id`** corrupted the server temp path → INTERNAL_SERVER_ERROR.
  FIX: `file_id` is now always positive.
- ⛔ **apk files** → `error 400: PEER_ID_INVALID`. Confirmed by the user this is an
  **Eitaa platform limitation** (the web app can't send .apk either). NOT our bug.
  Nothing to fix. txt/zip/pdf/images work.
- ⛔ **`contacts.importContacts` on the browser-free path** → SETTLED 2026-07-25 with
  a live probe. Eitaa answers with a **4-byte, payload-less reply, cid=0xdc252379**
  — not `contacts.importedContacts`, and not any constructor in the Telegram schema
  (searched; it is Eitaa-specific). **Both** phone formats (`+98…` and `98…`) got the
  identical reply, so the phone format was never the cause: this endpoint simply does
  not serve contact import off the browser path. Two earlier runs looked like
  "0 contacts found" only because an unexpected reply was being counted as
  "not on Eitaa".
  DO NOT re-investigate. Contact building now auto-hands-over to the bridge path.
  If it is ever worth another try, the only honest next step is to capture the
  browser's OWN successful importContacts response (`direct-inspect-capture` on the
  `contact` op) and compare it with this 4-byte reply.

## 6) Known limits / next steps

**Moved to `docs/ROADMAP.md`.** This section used to hold the TODO list and drifted
badly — it was still asking for the multi-part upload test and the first browser-free
send-to-all, both of which have since passed live. The roadmap now lives in one file.

Resolved since this section was written:
- ~~Verify multi-part (>512 KiB) file uploads live.~~ DONE — 9.4 MB / 19 parts.
- ~~First browser-free send-to-all.~~ DONE — the direct engine sends text and files.
- ~~Is the contact-build bug a phone-format problem?~~ ANSWERED, and it was neither:
  browser-free `importContacts` is simply not served off the browser path (§5).

Still open, carried into `docs/ROADMAP.md`:
- The **direct** engine cannot harvest peers itself: `contacts.importedContacts`
  ends with a `Vector<User>` whose row constructor is Eitaa-specific and unknown, so
  `parse_import_result()` deliberately stops at the safe rows. Peer harvesting needs
  one bridge-engine pass.
- token1/token2 stability across browser restarts; ideally source the token from the
  session export (localStorage `token`) instead of the newest capture.
- Not built: resume/checkpoint for the panel's send job, recipient selection/dedupe,
  2FA live, scheduling, periodic batch cooldown in the panel, groups/channels and
  receiving messages on the direct path.

## 7) Workflow notes
- Confirm before brand-new directions; standing "بساز" to keep extending the direct
  client + capture + bot.
- Push via the GitHub power (never raw `git push`). Update `bot/README.md`,
  `direct/README.md`, and this file on every change.
- Detailed knowledge is also stored in the assistant's learnings.

---

Latest branch: https://github.com/shootingv818/Mkwlsoso/tree/feat/eitaa-web-capture


---

## 2026-07-29 — engines unified, panel made honest, everything measured

All of this was driven by measurements on the live host, not guesses:
1 CPU core with **30-89% steal**, 961 MB RAM, **158-203s just to open Chromium**,
and 1-3s per HTTPS request to Eitaa.

### The three engines are now one code path

`bot/transports.py` gives the send loop a single interface, so the browser-free
engine stopped being a duplicated second loop that nobody maintained:

| Engine | Sends through | Safety net |
|---|---|---|
| `bridge` | the real web app inside Chromium | — (it *is* the proven path) |
| `hybrid` | plain HTTPS, no browser | the page, per recipient |
| `direct` | plain HTTPS, no browser | none (needs `MKWL_ENABLE_DIRECT=1`) |

The engine switch is back in Settings and cycles `bridge → hybrid → direct`.

**Why the direct engine was unusable before, and what changed**

* It needed a session context (two envelope tokens + the account's own peer) that
  only a hand-run CLI capture produced, so it went stale and answered
  "no browser-free session capture". `bot/direct_ctx.py` now dumps the app's own
  worker traffic whenever a browser session is open anyway and saves it, so the
  context refreshes itself.
* It could only address peers that had been separately *harvested*. The API
  contacts list provides `access_hash` for every contact, so any contact is now
  addressable without a browser.
* A recipient it cannot deliver to falls through to the page automatically. A
  server **refusal** (PEER_FLOOD) does not: the server already answered, and the
  page would only repeat it more slowly.

### Refused recipients are remembered

Measured on a healthy account: 12 contacts → **6 delivered, 6 PEER_FLOOD**, and
the split was per RECIPIENT (identical sequentially at 3s and concurrently at 1s).
PEER_FLOOD there means Eitaa will not deliver from this account to that person; it
does not expire on a timer. `bot/blocked_store.py` remembers them, so later runs
skip them instead of spending half the run collecting the same errors. Timed
`FLOOD_WAIT_n` is never treated this way. Panel: `⛔ Reset Refused`, and the
account card shows `Reachable: N of M`.

### The panel no longer blocks the sending

`LiveCard` used to `await` the Telegram edit **inside the send loop**, so every
progress update put a Telegram round trip (and any edit rate limiting Telethon
silently sleeps through) between two messages. It now stashes the newest text and
a background painter delivers it; intermediate states are dropped, not queued.

### Every run reports where its time went

A run measured **6.2s per message while Eitaa answered in 1-2s** and nothing could
say what the other 4s were. Each run now ends with a `⏱ RUN TIMING` card and a
`[send] timing:` log line splitting total time into sending / slow path / pacing /
everything else, plus the achieved msg/s.

### Rate maths (measured, not theoretical)

    rate = concurrency / (RTT + delay)

With the measured RTT of ~1.9s: `conc=3, delay=1` → **~1.03 msg/s** (180 messages
in under 3 minutes). `conc=3, delay=3` is *slower* than `conc=1, delay=1` - the
delay and the round trip add up, they do not overlap.

### Tests

    python -m bot.tests.test_bot_logic     # panel/store/cards/guards
    python -m bot.tests.test_send_loop     # the send loop against a fake Eitaa
    python -m bot.tests.test_engines       # bridge/hybrid/direct + the new stores
    python -m bot.tests.test_live_card     # the card never blocks a job
    python -m bot.tests.test_login_settle  # confirming a login after sign-in
    python -m direct.tests.test_direct     # wire-format tests (unchanged)

### Rollback

Tag `baseline-before-hybrid` is this branch's starting point;
`baseline-before-optimizations` and branch `feat/fast-send-multi-account` are the
older, known-good states.


---

## 2026-07-31 → 2026-08-06 — the portal / logbus / worker era

Recorded 2026-08-13 from the commit history on `update-test` (@ `caf8ce8`); this log
had stopped at the 2026-07-29 entry while ten features landed. Roadmap status for each
of these lives in `docs/ROADMAP.md` — here is only what was built, newest last.

### Deployment moved to a US server
Eitaa turned out to be reachable from the US host (~0.3–0.8s RTT, concurrency scaling
roughly linearly to ~4.8 req/s at conc=6), which is far better than the 1.9s measured
before. This quietly retires the "run everything in Iran, proxy only Telegram" plan in
`docs/NETWORK_ARCHITECTURE.md` — it is on hold, not pending. Also fixed on the way: the
`.env` loader did not strip inline comments, so the bot died on a copied `.env.example`
(branch `fix/dotenv-inline-comments`).

### `.apk` sending fixed (branches `feat/apk-send-mode`, `feat/apk-bridge-fix`)
Root cause and proof are in `docs/APK_SEND_STATUS.md`. Eitaa filters the *MIME*, not the
filename or the bytes. `direct/apk_mode.py` rewrites `.apk` to
`application/octet-stream` and keeps the real name in `documentAttributeFilename`; it is
applied on both the direct path and the bridge path (`eitaa/driver.py` →
`bridge_file_init`). Toggle `📦 APK send mode`, default OFF, defensive fallback to the OS
MIME. Live: 11/11 recipients, upload 5.7s, delivery 2.71 msg/s.

### Multi-account parallel send
`run_send_multi` grew a sliding window so 2 accounts send at once
(`MKWL_MULTI_PARALLEL`, ceiling 2, `MULTI_SHARE_BUDGET`, 10s stagger). Width 1 is
byte-for-byte the old sequential run — that is an explicit test invariant. Two bugs
fixed alongside: Force Stop did not reach every child, and a newly added account jumped
to the front of the list instead of the end.

### Feature packages
- **Session check** (`session_check/checker.py`) — three signals: `is_logged_in()` →
  `self_peer_id()` → `bridge_stats(with_pvs=False)`. Panel: `pnl:check`.
- **Warm Path** (`eitaa/warmpath.py`, `MKWL_WARMPATH`, OFF) — reuse the booted page
  instead of re-navigating, and skip the ~98s `getDialogs` PV count on the Settings
  stats refresh. Invariant: OFF behaves exactly like the old bot.
- **Photo export** (`photo_export/`) — read-only; one photo per PDF page via headless
  Chromium. The tests encode the real paging traps: `getDialogs` returns 25 then 100 (so
  walk until EMPTY), and `messages.search` can return a null `count` (so walk until
  SHORT).
- **Contact Boost** (`contacts_boost/`, `MKWL_BOOST`, OFF) — probe blocks of numbers via
  `contacts.importContacts`. Hardened after the first version: per-account blocks so two
  accounts never probe the same number, random picking within a prefix, several prefixes
  with one chosen at random, cursor persisted after every batch, waits out FLOOD rather
  than aborting. Needs `MKWL_BOOST_PREFIX` set even when toggled on.
- **SMS login probe** (`probe_login_sms.py`) — standalone, writes a full transcript.

### Settings menu split into categories
One flat Settings screen became `set:cat:*` groups (sending / engine / contacts /
portal), because the flat list had outgrown a single Telegram keyboard.

### Web login portal (`portal/`, OFF by default)
Built bottom-up: durable attempt stats and live tunnel status → the login adapter →
FastAPI + cloudflared tunnel + owner panel → the project's own polished Eitaa-branded UI
wired to the real API. It drives the **same warm-pool browser login** as the panel, so
there is one login implementation, not two. Per-attempt hmac ownership tokens, a global
registry lock, capacity with a queue position. Last change (`caf8ce8`): a 5-digit code
UI, TTL shortened to 350s, and a refresh or re-click now gets a genuinely fresh session.
Needs `fastapi`, `uvicorn`, `httpx` and a `cloudflared` binary — none declared in
`requirements.txt`; every import is lazy, so a missing dependency leaves the portal off
rather than crashing the bot.

### Central log group (`bot/logbus.py`, inert by default)
Mirrors send completions (`bot/runner.py`, both send paths) and portal logins
(`portal/login_adapter.py`) into a Telegram group, configured from the portal panel.
Guarded end to end: it never raises, and it stays inert until `MKWL_LOG_GROUP_ID` is set.

### Live-tunable warm-browser ceiling
`MKWL_POOL_MAX_OPEN` + `pool.set_max_open()` + a panel toggle, so the parallelism knob
can move on a bigger host without a restart. Default stays 1 — that was measured as
mandatory on the old 961 MB box.

### Distributed worker fleet — core tested, transport scaffolded
The goal is browser work on extra servers behind one Telegram panel.
- Tested core: `worker/store.py` (JSON registry, tags, capacity, account→worker affinity
  map), `worker/selection.py` (affinity, least-loaded round-robin, failover),
  `worker/health.py` (TCP + API probe, backoff to 10 min).
- Scaffolds, self-labelled and not unit-tested: `worker/transport.py` (SSH local port
  forward via `asyncssh`) and `worker/agent.py` (worker-side FastAPI `/ping`, `/health`,
  `/login/*`, `/send/*`, bearer token, loopback bind).
- **Honest status: `worker/` is not imported anywhere outside its own tests.** No panel
  screen, no routing in `run_send`, no health loop. `MKWL_MASTER_AS_WORKER=1` (default)
  means everything runs in-process exactly as before. It needs a second real server to
  go further.

### Tests
Now 15 offline suites under `bot/tests/` plus the wire-format suite in `direct/tests/`.
All 16 verified passing on 2026-08-13 (Python 3.9 and 3.11). New since the last entry:
`test_engines`, `test_scenarios` (18 fault-injection cases, including the dangerous pool
ones — session dies while warm, cancelled mid-lease, eviction, recycling, idle expiry),
`test_multi_parallel`, `test_contacts_boost`, `test_photo_export`, `test_warmpath`,
`test_portal`, `test_portal_login`, `test_worker`, `test_logbus`, `test_pool`.
Still uncovered: the FastAPI/uvicorn/cloudflared layers, `worker/transport.py`,
`worker/agent.py`, real Chromium session opening, `cli.py`, `jobs/campaign.py`, `deploy/`.
