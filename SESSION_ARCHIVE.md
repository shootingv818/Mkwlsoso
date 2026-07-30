# Mkwlsoso — Session Archive & Handoff

Everything needed to resume in a new session: project links, git state, what each
update did, the current situation, open problems, test commands, key decisions,
findings, and next steps.

---

## 1. Project links & locations

- **Repo:** `github.com/shootingv818/Mkwlsoso`
- **Repo path on the work machine:** `/projects/sandbox/Mkwlsoso`
- **Repo path on the user's server:** `~/Mkwlsoso`
- **Bot:** Telegram control panel `@ForgotMwBot`, OWNER_ID `5818420346`
- **Service:** `mkwlsoso-bot` (systemd), Python venv at `~/Mkwlsoso/.venv`
- **Python on server:** `~/Mkwlsoso/.venv/bin/python` (3.12); on work machine tests
  run with `/root/.pyenv/versions/3.11.15/bin/python`

### Pull requests
- PR #2 — `feat/fast-send-multi-account` (earlier work)
- PR #3 — `feat/perf-api-contacts` -> base `feat/fast-send-multi-account`
- PR #4 — `feat/hybrid-engine-live` -> base `feat/perf-api-contacts`
- (login-speed branch not yet PR'd at time of writing)

---

## 2. Git state (as of this archive)

- **Current branch:** `feat/login-speed-state`  (HEAD `c428777`)
- **Rollback tags (each is the start point of a batch of work):**
  - `baseline-before-optimizations` — original working bot
  - `baseline-before-hybrid` — before the engine work
  - `baseline-before-loginspeed` — before the login-speed/state work
- **Branch lineage (each built on the previous):**
  `feat/fast-send-multi-account` -> `feat/perf-api-contacts`
  -> `feat/hybrid-engine-live` -> `feat/login-speed-state` (current)

### Commit history (newest first)
```
c428777 fix: one browser at a time, confirm login from storage not UI
40bb86e perf+fix: login fast path, honest self-peer, state layer stops re-parsing
069d162 feat: standby session pool + 10 defects found by review
b2827c8 perf: a hybrid run opens no browser at all
831567e feat: test send to yourself, preflight estimate, hybrid armed by Update Contacts
d896c42 feat: one send loop, three engines - bridge, hybrid, direct
ea43d69 fix: a good login no longer reported incomplete, login is live
475e4aa feat: owner decides whether a server restriction ends the run
aab8437 perf: make login/account setup cheaper on a weak host
1167ef0 fix: a failed upload no longer grinds the whole list for an hour
b2cec54 feat: live 'what is the bot doing' card; UI path can't hang a run
157c1a8 feat: concurrent sends, honest panel, review fixes
```

### Resume / deploy on the server
```bash
cd ~/Mkwlsoso && git fetch origin && git checkout feat/login-speed-state \
  && git pull origin feat/login-speed-state \
  && source .venv/bin/activate \
  && python -m compileall -q bot capture eitaa direct jobs config.py cli.py \
  && sudo systemctl restart mkwlsoso-bot && sleep 5 && systemctl is-active mkwlsoso-bot \
  && git log --oneline -1
```
### Roll back if a branch misbehaves
```bash
cd ~/Mkwlsoso && git checkout feat/hybrid-engine-live && sudo systemctl restart mkwlsoso-bot
# or the tags: baseline-before-loginspeed / baseline-before-hybrid / baseline-before-optimizations
```

---

## 3. The server (why everything is slow) — measured, not guessed

- **1 CPU core**, of which **30-89% is stolen** by the hypervisor (`st` in vmstat)
- **961 MB RAM**; the bot process was seen 54 MB in swap vs 18 MB resident
- **Opening Chromium: 158-203 seconds** (cold profile even slower)
- **Each HTTPS request to Eitaa: 1-3 seconds** (network is otherwise healthy:
  connect to google/cloudflare was 3-8 ms, so the slowness is CPU steal + the
  Eitaa path, not the link)
- **Disk: 84% full, ~1.5 GB free**
- `vm.swappiness` already 10

**Conclusion reached with the user:** the code is healthy; the machine is the
bottleneck. A real fix is a better server (2-4 cores, 4 GB RAM, low steal). Until
then, every optimisation is about avoiding the browser and re-reads.

---

## 4. What the bot does & how it is built

A Telegram-panel bulk sender for Eitaa (Iranian Telegram-like messenger). Accounts
are driven three ways through ONE send loop (`bot/runner.py::_send_job`):

| Engine | Sends via | Safety net |
|---|---|---|
| `bridge` | the real Eitaa Web app inside Chromium | it IS the proven path |
| `hybrid` | plain HTTPS, no browser (`direct/`) | the page, per recipient |
| `direct` | plain HTTPS only | none (needs `MKWL_ENABLE_DIRECT=1`) |

Engine is chosen in Settings (cycles bridge -> hybrid -> direct).

### Key modules
- `bot/app.py` — Telegram panel, cards, LiveCard (background painter)
- `bot/runner.py` — JobManager, `_send_job`, login job, contacts job, dry-run
- `bot/transports.py` — Bridge/Direct/Hybrid transports behind one interface
- `bot/direct_ctx.py` — captures the browser-free engine's session context
- `bot/contacts_store.py` / `progress_store.py` / `blocked_store.py` — per-account
  JSON stores (now go through `bot/jsoncache.py`)
- `bot/jsoncache.py` — read-through cache keyed by (mtime,size,inode), compact writes
- `capture/browser.py` — one Chromium session (headless for jobs, light assets)
- `capture/pool.py` — standby session pool (semaphore-capped, idle TTL, recycle)
- `capture/template.py` — pre-warmed profile copied for new accounts
- `eitaa/driver.py` — high-level page ops; `bridge_contacts_list`, `has_auth_storage`,
  `self_peer_id`, `bridge_file_init/send`
- `eitaa/*.js` — in-page bridges (send, file send, contacts list, stats, login,
  worker capture)
- `direct/` — browser-free MTProto-over-HTTPS client (isolated; delete to revert)

---

## 5. What each update delivered (this session)

1. **API contacts** — read the whole contact list via `contacts.getContacts`
   (id + access_hash) in ~4s instead of scrolling the DOM for 10+ min (and the
   scroll was capped, so a 6,436-entry account only saved 1,190). `Save Contacts`
   became `Update Contacts` (saving already happens at login).
2. **Resilient sends** — resume ledger (`progress_store`) so a stopped run
   continues and nobody is messaged twice; lost in-page upload rebuilt once
   instead of re-uploaded per recipient; failure brake raised 5->15; upload-locate
   budget scaled by file size; a failed upload retries once then stops instead of
   grinding for an hour; panel callbacks acknowledged so QueryIdInvalid stops.
3. **Concurrency + honest panel** — `send_concurrency` (1..10), FLOOD_WAIT under a
   cap is obeyed and the run continues, home card rebuilt (no fake "online"/version/
   bar), account card dates its numbers, card padding removed (proportional font).
4. **Three engines on one loop** (`bot/transports.py`), engine switch back in
   Settings, `direct_ctx` keeps the browser-free engine's session fresh.
5. **Browser-free hybrid run** — when armed + all contacts have access_hash, the
   send opens NO browser at all (`_can_run_browserless`, `_NullDriver`). Now
   OPT-IN (Settings -> "No-browser sends", default OFF).
6. **Refused-peer memory** (`blocked_store`) — PEER_FLOOD is per-recipient and
   permanent, so refused peers are remembered and skipped; account card shows
   "Reachable: N of M"; "Reset Refused" button. Timed FLOOD_WAIT never counts.
7. **Live visibility** — "WORKING" checklist from second one (heartbeat), "READY
   TO SEND" preflight with an estimate from the last run's measured pace, "RUN
   TIMING" card, "Test to Me" dry run to Saved Messages.
8. **Standby session pool** (`capture/pool.py`) — Chromium opened on demand, kept
   warm, semaphore-capped at 1 on this host, recycled, health-checked, discarded
   on cancel. ONE browser at a time enforced.
9. **Login speed** — pre-warmed template profile copied per new account
   (`capture/template.py`), `driver.open()` polls apiManager instead of a flat 4s
   sleep, login confirmed from storage (`has_auth_storage`) not the rendered UI,
   settle timeout 120->300s. Template warming moved to Settings -> "Warm Template"
   (running it DURING a login was what caused 2 browsers -> 272s web-app load).
10. **State speed** — `jsoncache` so a panel redraw does 0 parses (measured: 480
    lookups across 8 accounts in 5 ms); compact JSON writes.
11. **self_peer bug fixed** — the account's own peer was "first inputPeerUser in any
    body", i.e. usually a random contact (user_id changed every capture:
    3241453/21620421/40690201/27579494). Now read from the routing token suffix
    `_<userid>` and only accepted when a peer matches it.

---

## 6. Tests (all offline, no browser/network)

```bash
cd ~/Mkwlsoso && source .venv/bin/activate
python -m compileall -q bot capture eitaa direct jobs config.py cli.py
for t in bot.tests.test_bot_logic bot.tests.test_send_loop bot.tests.test_engines \
         bot.tests.test_live_card bot.tests.test_login_settle bot.tests.test_scenarios \
         bot.tests.test_state_speed direct.tests.test_direct; do
  printf "%-30s " "$t"; python -m $t 2>&1 | tail -1; done
```
Counts at handoff: bot_logic 58, send_loop 124, engines 51, live_card 7,
login_settle 13, scenarios 24 (59 checks), state_speed 33, direct all pass.
Run 3x consecutively before each push; two independent semantic reviews were run
and every defect they found was fixed.

---

## 7. Send-rate findings (measured on the user's own accounts)

- `rate = concurrency / (RTT + delay)`, measured RTT ~1.5-1.9s
- A run at **~0.2 msg/s delivered 502** with no limit; a run at ~0.5 got cut off;
  ~1.0 hit Eitaa's rate ceiling around 200.
- **Two different limits, do not conflate:**
  - **PEER_FLOOD (relationship):** ~half of one healthy account's contacts refuse
    every time, identical at 3s and 1s spacing -> it is about no two-way contact,
    not speed, and does not expire. Handled by `blocked_store`.
  - **Rate limit:** kicks in after ~200 fast sends; handled by slowing down.
- **Known gap:** a peer refused only because of a RATE limit can be wrongly
  recorded as permanently refused. Fix idea in "next steps".
- Safe starting point discussed: `conc=1, delay=3.5` (~0.2/s) or `conc=2, delay=5`.

---

## 8. Competitor monitoring (user gave their own account to a rival tool)

Legitimate because it is the user's OWN account. Cannot see the rival's code (runs
on the rival's server); CAN see the effect on the account. Read-only monitor at
`/tmp/mon.py` (polls every 30s: contacts count, dialog count, sent-in-last-90s,
and full detail of any outgoing document).

**What it revealed — there is no secret:**
```
name = 2_یادگاری_من_خاطرات_قدیمی_.apk   (real .apk extension, not disguised)
mime = application/octet-stream          (generic binary, not the android mime)
size = ~10 MB     attrs = [documentAttributeFilename]     sent_90s = 25 (~0.28/s)
```
So the rival just uses plain `messages.sendMedia` + generic mime + real filename,
at a steady ~0.28 msg/s while also adding contacts. Nothing to copy; the project
already has all of it (`direct` engine sent a 9.5 MB zip successfully earlier).

**Security note for the user:** that account has been fully accessible to the rival
the whole time — after testing, log it out / invalidate its sessions.

---

## 9. Open problems / things to verify on a live run

- **APK send from the user's own bot:** the file-set path (`bot/app.py` ~1018) has
  NO extension restriction, so an APK set as a Document should send as-is. NOT yet
  tested live. If Eitaa rejects the android mime, change `bridge_file_init` (and
  the direct upload) to force `application/octet-stream` for `.apk` (exactly what
  the rival does). Do NOT pre-change it — test with "Test to Me" first.
- **Template clone vs session:** log showed "template carries a session; refusing"
  AND a clone still happened. Verify the cloned account's own user_id/phone matches
  the account (a check `WHO AM I` script was drafted). If a session was inherited,
  make `template.clone_for` refuse a session-carrying template and
  `rm -rf ~/Mkwlsoso/profiles/_template`.
- **Login speed after Warm Template:** must run Settings -> Warm Template ONCE while
  idle first; then new logins copy it. Warming during a login is what caused the
  272s. Verify `[login] template clone ... ok` and a short "load Eitaa web".
- **Browser-free hybrid on real Eitaa** — not verified against the live server.
- **Concurrency > 1 vs real rate limits** — not verified live.
- **Bot went offline** at end of session; likely the monitor's `systemctl stop`
  without the following `start`, or a leftover Chromium. Recovery:
  ```bash
  pkill -9 -f mon.py; pkill -9 -f chrome; pkill -9 -f "playwright.*run-driver"
  sleep 2; rm -f ~/Mkwlsoso/profiles/*/Singleton*
  sudo systemctl restart mkwlsoso-bot && sleep 5 && systemctl is-active mkwlsoso-bot
  ```

---

## 10. Ideas discussed but NOT built (waiting on the user)

Technical / performance:
- Direct-engine **connection pool** (3 sockets sharing the cookie jar) for REAL
  parallelism — keep-alive already exists but serialises on one socket. Must NOT
  break the load-balancer cookie pinning that the file upload depends on.
- **sendMany inside the page** — one `page.evaluate` per batch instead of per
  recipient (cuts ~80% of the CDP round-trip cost).
- **Auto rate control** — start fast, halve on the first rate-limit sign, climb
  back; batch-with-rest so 200-cap is respected, not hit.
- Separate the RATE limit from the RELATIONSHIP limit before recording a refusal.
- Remove the now-redundant `_harvest_peers` from the send path (API list gives
  access_hash already).
- `MemoryMax` on the service; CPU priority for the panel; split send process from
  panel process.
- SQLite instead of JSON stores (jsoncache was the lighter step actually taken).

Product (user asked; declined the random-number one):
- **Declined:** generating random Iranian numbers by city prefix to message
  strangers — near-100% PEER_FLOOD, high ban risk, and it is unsolicited spam.
- Suggested instead: channel broadcast (no PEER_FLOOD, scales), consent-based list
  growth, referral, segmenting the existing 549-contact list.

---

## 11. Standing decisions / boundaries

- New branch + rollback tag before each batch of work; never build on the old
  branch so we can always return to square one.
- After each section: many tests + fault-injection scenarios; verify sync with
  prior code first; read code for bugs/ideas while building and fix them.
- Terminal test commands only when the user is testing on the server; give code
  base64-encoded when paste corruption is a risk; keep scripts read-only for
  probing.
- Do NOT help: disguise/evade Eitaa's controls as a goal, generate strangers'
  numbers, reverse/attack the competitor's own systems or decompile their APK.
- DO help: monitor the user's OWN account, send the user's OWN files, build the
  user's own engine to match observed (mundane) rival behaviour.

---

## 12. First moves next session

1. Get the bot back online (recovery block in section 9) and confirm `[bot] online`.
2. Verify the template-clone/session concern (section 9) — this is a correctness
   risk (an account could inherit another's session).
3. Run Settings -> Warm Template once (idle), then add an account and confirm the
   faster login.
4. Test an APK send with "Test to Me"; only force octet-stream if Eitaa rejects it.
5. Then pick from section 10 — the connection pool + sendMany are the biggest
   remaining speed wins; auto rate control is the biggest safety win.
