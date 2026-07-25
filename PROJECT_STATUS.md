# Mkwlsoso — Project Status & Handoff

Comprehensive record of the browser-free Eitaa Direct Client + the Telegram bot
v2 update. Read this first when continuing on a new session/account.

- Repo: `shootingv818/Mkwlsoso`
- Working branch: `feat/eitaa-web-capture` (PR #1)
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

## 6) Known limits / TODO (next steps)
- Direct-engine **send-to-all contacts** is NOT built (needs a contact-peer sync,
  e.g. parse imported users' access_hash or reverse `contacts.getContacts`). For
  now the bot's SEND always uses the bridge fast-path; the engine setting governs
  CONTACT BUILDING only.
- Confirm token1/token2 stability across browser restarts; ideally source the
  token from the session export (localStorage `token`) instead of the newest capture.
- Verify multi-part (>512 KiB) file uploads live.
- Optional: a direct MTProto login handshake (so `direct` needs no browser capture at all).
- Bot v2 is compiled + offline-checked but NOT yet live-tested end-to-end by the user.

## 7) Workflow notes
- Confirm before brand-new directions; standing "بساز" to keep extending the direct
  client + capture + bot.
- Push via the GitHub power (never raw `git push`). Update `bot/README.md`,
  `direct/README.md`, and this file on every change.
- Detailed knowledge is also stored in the assistant's learnings.

---

Latest branch: https://github.com/shootingv818/Mkwlsoso/tree/feat/eitaa-web-capture
