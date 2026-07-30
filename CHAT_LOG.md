# Mkwlsoso — Conversation Log (for the next session)

A chronological summary of the chat that produced this work. Read alongside
`SESSION_ARCHIVE.md` (which holds the technical state, links, and next steps).
Each entry is: what the user asked -> what was done.

---

### Early context (before this log)
- Built the multi-account fast sender, bridge-only panel, contacts cache, sequential
  multi-send, two-stage Stop. Branch `feat/fast-send-multi-account`, PR #2.

### Roubika / Eitaa app-sending question
- User asked how to send an app (APK) on Roubika/Eitaa, and about "hidden" methods.
- Answer: no evasion/disguise methods. Legitimate routes: official market, exact
  app name + search phrase, consent channels (SMS/email), normal ZIP if allowed.
  Clarified it was Eitaa, not Roubika.

### Business / automation ideas
- User wanted heavy bulk-advertising ideas, senders on their side.
- Gave consent-based ideas (multi-account engine, drip campaigns, targeted sends,
  multi-channel). Declined mass-spam-to-strangers framing.

### "Why is the bot slow?" investigation
- User: 300 messages in 47 min, login takes ~10 min, bot feels like a junk machine.
- Ran diagnostics. Found the ROOT CAUSE is the server, not the code:
  1 core with 30-89% CPU steal, 961 MB RAM, Chromium takes 158-203s to open,
  each Eitaa request 1-3s, disk 84% full. Network itself is fine.
- Found several real bugs from the logs (see below).

### Bug hunt from logs (fixed across the session)
- Contacts were DOM-scrolled (10+ min, capped) -> 6,436-entry account saved only
  1,190. Switched to `contacts.getContacts` API: ~4s, complete, with access_hash.
- Failure brake tripped at 5 -> killed a 1,099 run at 300. Raised to 15.
- No resume ledger -> re-runs re-sent to the first 300. Added `progress_store`.
- Lost in-page upload -> 25s re-upload per recipient. Rebuild once instead.
- 90-iteration locate loop -> 600s stalls. Budget scaled by file size.
- Playwright driver leaked; stale Chromium SingletonLock killed jobs; panel
  QueryIdInvalid. All fixed.

### Update batches (each on a new branch + rollback tag)
1. `feat/perf-api-contacts` (PR #3): API contacts, Update-Contacts rename,
   zero-recipient guard, drop the 98s PV count from login.
2. Resilient sends: resume, upload rebuild, brake, locate budget, callback ack.
3. Concurrency + honest panel; FLOOD_WAIT-under-cap continues; "Pause on limit".
4. `feat/hybrid-engine-live` (PR #4): one send loop + three engines
   (bridge/hybrid/direct), `direct_ctx` keeps the browser-free engine fresh.
5. Browser-free hybrid run (opt-in), refused-peer memory (`blocked_store`),
   live "WORKING"/"READY TO SEND"/"RUN TIMING" cards, "Test to Me" dry run.
6. Standby session pool (`capture/pool.py`), then 10 review defects fixed.
7. `feat/login-speed-state`: template profile, poll-not-sleep in driver.open(),
   login confirmed from storage, jsoncache state layer, self_peer bug fixed,
   one-browser-at-a-time semaphore, Warm Template moved to Settings.

### Rate-limit findings (from the user's own accounts)
- rate = concurrency / (RTT + delay); RTT ~1.5-1.9s.
- ~0.2 msg/s delivered 502 cleanly; ~1.0 hit Eitaa's ~200 ceiling.
- PEER_FLOOD is per-recipient/relationship (half of one account refused every
  time, same at 3s and 1s) and permanent -> remembered & skipped. Rate limit is
  separate and handled by slowing down. Known gap: a rate-limited peer can be
  wrongly marked permanently refused.
- User mentally set concurrency to 3.

### Login speed push (user's main goal)
- User: login/code entry is very slow, make it as fast as possible.
- Built template profile + prewarm. FIRST attempt made it WORSE: prewarm ran
  during a login -> two Chromiums on 961 MB -> "load Eitaa web" hit 272s.
- Fixed: semaphore caps browsers at 1; prewarm refuses while busy and a login
  cancels it; Warm Template moved to a Settings button (run while idle); login
  confirmed from storage (auth keys land instantly) not the rendered chat list;
  settle 120->300s.
- Result seen live: `clone 8.4MB in 0.5s`, `auth found in storage after 1 check`,
  `saved 827 contacts` — much faster.

### Competitor monitoring (user gave their OWN account to a rival tool)
- User wanted to know how the rival is fast and how it sends APK.
- Established boundary: monitoring your OWN account = fine; reversing/attacking the
  rival's server or decompiling their APK = no. Their code isn't visible anyway
  (runs on the rival's server).
- Built read-only monitor `/tmp/mon.py` (30s poll: contacts, dialogs, sent-90s,
  outgoing document detail).
- CAUGHT the rival's method — and it is mundane:
  `name=...apk (real ext), mime=application/octet-stream (generic), size=10MB,
   attrs=[documentAttributeFilename], sent_90s=25 (~0.28 msg/s)`.
  Plain `messages.sendMedia`, generic mime, real filename, steady rate, adds
  contacts while sending. Nothing to copy; the project already does all of it.
- Security note: that account was fully exposed to the rival the whole time —
  user should log it out / invalidate sessions.

### APK sending from the user's own bot
- Confirmed Eitaa DOES allow file sends (rival sent a 10MB apk on the user's
  account; the project earlier sent a 9.5MB zip). The file-set path has NO
  extension restriction, so an APK set as a Document should send as-is.
- NOT tested live yet. If Eitaa rejects the android mime, force octet-stream for
  .apk (what the rival does). Test with "Test to Me" first; don't pre-change.

### End of session
- Bot went offline (likely the monitor's `systemctl stop` without the following
  `start`, or a leftover Chromium). Recovery block is in SESSION_ARCHIVE.md sec 9.
- Created `SESSION_ARCHIVE.md` (full handoff) and this `CHAT_LOG.md`, pushed to
  `feat/login-speed-state`.

### First moves next session (also in SESSION_ARCHIVE.md sec 12)
1. Bring the bot back online and confirm `[bot] online`.
2. Verify the template-clone/session concern (an account may have inherited a
   session — check WHO AM I vs the account's own phone/user_id).
3. Warm Template once while idle, then add an account and confirm faster login.
4. Test an APK send via "Test to Me"; force octet-stream only if rejected.
5. Then: direct-engine connection pool + in-page sendMany (biggest speed wins),
   auto rate control (biggest safety win).
