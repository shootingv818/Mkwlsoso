---
inclusion: always
---

# Mkwlsoso project — always-on context

You are working on **Mkwlsoso**, a Telegram-panel bulk sender for Eitaa. Before
doing anything, read these two handoff files at the repo root — they hold the full
state, decisions, findings and next steps from prior sessions:

- `SESSION_ARCHIVE.md` — technical state: git branches/tags, what each update did,
  server constraints, open problems, ideas not yet built, first moves.
- `CHAT_LOG.md` — chronological log of the conversation that produced the work.

## Fast facts to carry into every session
- Repo: `github.com/shootingv818/Mkwlsoso`. Server checkout: `~/Mkwlsoso`.
- Current work branch: `feat/login-speed-state`. Rollback tags:
  `baseline-before-loginspeed`, `baseline-before-hybrid`,
  `baseline-before-optimizations`.
- The server is the bottleneck, not the code: 1 CPU core with 30-89% steal,
  961 MB RAM, Chromium takes 158-203s to open, each Eitaa request 1-3s, disk ~84%.
- Three engines on one send loop: `bridge` (browser), `hybrid` (browser-free with
  page fallback), `direct` (browser-free only).

## Working rules the user expects (kept consistent all session)
- New branch + a rollback tag BEFORE each batch of work; never build on the old
  branch, so we can always return to square one.
- After each section: many tests + fault-injection scenarios; check sync with
  prior code first; read code for bugs/ideas while building and fix them.
- Run the full offline test suite 3x consecutively before each push. Commit +
  push each finished batch; keep PRs stacked on the previous branch.
- When the user tests on the server, give terminal commands only. Base64-encode
  long scripts to survive paste corruption. Keep probe scripts read-only.
- Respond in Persian, concise, focused on the main topic.

## Boundaries agreed with the user
- Do NOT help: disguise/evade Eitaa's controls as a goal; generate strangers'
  phone numbers (city-prefix spam — near-100% PEER_FLOOD, ban risk); reverse or
  attack the competitor's own systems / decompile their APK.
- DO help: monitor the user's OWN account, send the user's OWN files, and build
  the user's own engine to match the (mundane) behaviour observed from the rival.

## Immediately outstanding (start here next time)
1. Bot went offline at end of last session — bring it back (recovery block in
   SESSION_ARCHIVE.md section 9), confirm `[bot] online`.
2. Verify the template-clone/session concern: an account may have inherited another
   account's session (check WHO AM I vs the account's own phone/user_id).
3. Warm Template once while idle, then confirm faster login.
4. Test an APK send via "Test to Me"; force octet-stream mime only if Eitaa rejects.
5. Biggest remaining wins: direct-engine connection pool + in-page sendMany
   (speed), auto rate control (safety).
