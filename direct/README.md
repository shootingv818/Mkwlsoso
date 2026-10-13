# direct/ — headless (browser-free) Eitaa client

خلاصه فارسی: این پوشه ایتا رو **بدون مرورگر/کروم** اجرا می‌کنه — سریع، سبک، بدون
قفل کردن سرور. پیام، فایل و ساخت مخاطب مستقیم از پایتون فرستاده می‌شن. اگه یه وقت
خراب شد، کافیه **کل پوشه `direct/` رو پاک کنی** و پروژه دقیقاً برمی‌گرده سر جای اولش.

An **isolated** client that talks to Eitaa over HTTPS **without Chromium**.
Goal: remove the browser RAM/CPU cost and crashes, run on a small server, send fast.

## Isolation (important)
- `direct/` imports **nothing** from `bot/`, `eitaa/`, or `capture/`.
- The working browser bot does **not** import from `direct/`.
- Break glass: **delete `direct/`** and the project is exactly back to square one.

---

## The big discovery (evidence-based, from real worker captures)

Eitaa Web's MTProto worker POSTs to a shard host, e.g. `https://majid.eitaa.com/eitaa/`
(hosts are interchangeable across `.com`/`.ir`). Every request has this framing:

```
ed77be7a                     4-byte constant MAGIC
len1 (1B) | token1           ASCII routing token  "9179.c756a2d10f.e41c4e_<userid>"
len2 (1B) | token2           ASCII session id     "mrtpgmi2y9fm222__web"
bodyLen (4B, BIG-endian)     length of the payload
body                         <-- BARE, UN-ENCRYPTED standard Telegram TL (layer 135)
0000008700000020000000       11-byte constant trailer (encodes layer=135, 32)
```

**Key finding:** the body is **plain Telegram TL** — there is **NO AES / auth_key /
msg_id** on this path. Auth is the token; HTTP request/response is 1:1 so no MTProto
message container is needed. (The `auth_key`/`salt` in localStorage are not used here.)

### Confirmed constructors (little-endian on the wire)
| method | id |
|---|---|
| messages.sendMessage | `0x520c3870` (flags, peer, message, random_id) |
| messages.sendMedia | `0x3491eba9` (flags, peer, media, message, random_id, entities) |
| contacts.importContacts | `0x2c800be5` (Vector\<inputPhoneContact\>) |
| inputPhoneContact | `0xf392b7f4` (client_id, phone, first, last) |
| upload.saveFilePart | `0xb304a621` + **Eitaa 24-byte trailer** (flag=3, uploadPeer `0x59511722`+self_id, size) |
| inputMediaUploadedDocument | `0x5b38c6c1` |
| inputFile | `0xf52ff27f` |
| documentAttributeFilename | `0x15590068` |
| self / Saved-Messages peer | custom `inputPeerUser` ctor `0xdde8a54c` + user_id:long + access_hash:long (20B) |

Every serializer in `eitaa_tl.py` reproduces the **real captured bytes byte-for-byte**
(see `direct/tests/test_direct.py`).

---

## Files
- `transport.py` — HTTPS POST (stdlib) + `wrap_eitaa()` / `unwrap_eitaa()` envelope.
- `eitaa_tl.py` — method serializers, self-peer, file-send builder, `extract_context()`
  (pulls token1/token2/self-peer from the newest capture), `find_message_peer()`,
  `parse_import_result()`, `classify_response()`.
- `peers.py` — **the peer store**. Owns `artifacts/sessions/peers_<account>.json`
  (the format `direct-capture-peer` already wrote, plus an `id:<user_id>` alias per
  entry). `save_users()` bulk-saves harvested `{user_id, access_hash}` rows,
  `targets(account)` returns every sendable `(label, peer_bytes)`, `resolve()` looks
  one up by name or id, `forget()` drops the file when an account is deleted. Both
  the CLI and the Telegram bot use this one implementation.
- `sender.py` — **the reusable sender**: the long-running form of the proven
  `direct-send` / `direct-send-file` commands. `DirectSender(account)` loads the
  session context, keeps ONE keep-alive connection per host, and exposes
  `send_text(peer, text)`, `upload_file(path)` (once) + `send_uploaded_file(peer,
  caption)` (per recipient, no re-upload), and `import_contacts(entries)`. Results
  use the SAME dict shape as the browser bridge (`{ok, method, code, limit}`) so
  callers can treat both engines identically. Hosts are read from the account's own
  capture (API vs media) instead of being hardcoded.
- `tl.py` — TL wire primitives.
- `aes.py`, `crypto.py`, `mtproto.py`, `session.py`, `service.py`, `schema.py`, `dc.py`
  — the original encrypted-MTProto scaffolding, kept for a future direct **login**
  handshake (not needed for the token-based send path above).

Run all offline tests (no network, no deps):
```
python -m direct.tests.test_direct        # -> ALL DIRECT TESTS PASSED
```

---

## Commands (run on the authorized server; venv active)

Capture (needs the browser, `DISPLAY=:99`) — session constants live only in
gitignored `artifacts/sessions/`:
```
DISPLAY=:99 python cli.py direct-capture-all  --account <acct>          # text+file+contact wire bytes
DISPLAY=:99 python cli.py direct-capture-peer --account <acct> --to "<contact>"   # learn a contact's peer
```

Browser-free actions (no DISPLAY needed):
```
python cli.py direct-replay    --account <acct>                         # prove transport (idempotent config)
python cli.py direct-send      --account <acct> --text "hi"             # -> Saved Messages
python cli.py direct-send      --account <acct> --to "<contact>" --text "hi"
python cli.py direct-send-file --account <acct> --file path.zip         # -> Saved Messages
python cli.py direct-send-file --account <acct> --to "<contact>" --file path.apk --caption "..."
python cli.py direct-import    --account <acct> --phone "+98..." --first Name
```

Sending to a contact needs its peer first; uploads are peer-independent, so files
reuse the same self-upload path and only `sendMedia` routes to the chosen peer.

## Peers — the thing that unblocked browser-free sending
A send needs the target's `user_id` **and** `access_hash` (the 20-byte
`inputPeerUser`). Learning them one contact at a time with `direct-capture-peer`
needs a browser per contact, which does not scale.

The fix was not new protocol work — the data was already on the wire and being
discarded in two places:
- `contacts.importContacts` answers with the matched users **including their
  access_hash**; `eitaa/contacts_bridge.js` reduced it to a boolean.
- `appPeersManager.getInputPeerById` resolves a known contact to a real
  `inputPeer`; the bridge-reach report only asked *whether* a hash existed.

Both now return the value, and it is persisted through `peers.save_users()`.
Practically: build (or collect) contacts once with the **bridge** engine, and from
then on the **direct** engine can send to all of them with no browser at all.

Note the direct engine can count imported contacts but does NOT read their
access_hash: `contacts.importedContacts` ends with a `Vector<User>` whose row
constructor is Eitaa-specific and unknown. Guessing it would produce
silently-wrong peers, so `parse_import_result()` stops at the safe standard rows
(`imported`, giving `imported_ids`) and peer harvesting is left to the bridge.

---

## Load balancing gotcha (THE file-upload blocker)
The shard host is load-balanced across backend nodes. An uploaded file part is
stored on the **local temp disk of the node that handled `saveFilePart`**, and
`sendMedia` must run on that **same node**, or the server fails with
`INTERNAL_SERVER_ERROR "part key: 0 filename: /var/www/.../temp/<24hex>_<file_id>.<ext>_<internal_ip>"`.
The changing `_<internal_ip>` suffix (seen: .14/.29/.31/.83) proves each request
hit a **different** node.

What we tried and learned:
1. `file_id` must be **positive** — a negative id corrupts the server temp path. FIXED.
2. **Keep-alive alone did NOT fix it** — Eitaa's balancer routes per-request, not
   per-TCP-connection, so a single connection still scattered across nodes.
3. **Node stickiness is cookie-based** (standard L7 sticky session). The browser
   carries a sticky cookie (very likely HttpOnly, so JS can't read it); our bare
   Python client sent none → scattered.

### ACTUAL ROOT CAUSE (found via `direct-inspect-capture`)
There are **no cookies** at all (`direct-capture-cookies` returned 0). The real
reason: **media uses a DEDICATED media host, not the API host.** The browser's
`upload.saveFilePart` AND `messages.sendMedia` both went to **`fateme.eitaa.com`**,
while text/contacts went to `bagher.eitaa.ir`. We were POSTing media to
`majid.eitaa.com` (a regular API host with multiple nodes + local temp files) →
"part key: 0". The media host keeps upload + sendMedia on the same storage.

Fix (built, awaiting live test): `eitaa_tl.extract_media_url(capture)` pulls the
exact host the browser used for saveFilePart/sendMedia out of the capture, and
`direct-send-file` POSTs there (fallback `fateme.eitaa.com`, then `majid`). The
cookie jar + keep-alive stay in the transport as belt-and-suspenders.

Use `python cli.py direct-inspect-capture --account <acct>` (READ-ONLY) to see
the host + TL method of every captured request — this is how the media host was found.

### How to test the file fix
```
python cli.py direct-send-file --account test1 --file /tmp/t.zip --caption "zip"
python cli.py direct-send-file --account test1 --to "علی" --file /tmp/t.apk --caption "apk"
```
(`direct-capture-cookies` is optional now — media host is the real fix.)

### If it STILL fails — fallback approaches (in order)
- **A. Cookie value diff.** If `direct-send-file` prints `cookies=N` but still gets
  "part key: 0" with a *changing* `_<ip>`, the sticky cookie name we send is wrong.
  Compare `cookies_test1.json` names against what actually pins: try sending only
  a single likely LB cookie (e.g. one named `SERVERID`/`route`/`AWSALB`/`__cf*`/a PHP
  session id). Print the response's `Set-Cookie` (add a debug print in `post()`), and
  confirm the same node IP repeats after the first upload.
- **B. Capture real request headers.** Our `worker_capture.js` records body+url only.
  Extend it (or use CDP `Network.enable` on the worker target via
  `Target.setAutoAttach(flatten)`) to record the EXACT request headers the browser
  worker sends to `/eitaa/`, including `Cookie`. Replicate them verbatim.
- **C. Dedicated upload host.** Check the real URL the browser's `saveFilePart` used
  (records in `artifacts/sessions/capall_*.json` have per-request `url`). Media may
  need `hadi.eitaa.com` (single upload node / shared storage) rather than `majid`.
  If so, upload parts to that host and only `sendMedia` to `majid`.
- **D. Same-node handshake.** If the first response's `Set-Cookie` names the node,
  the jar (already implemented) should pin it. Verify the jar is actually receiving
  a `Set-Cookie` on the FIRST `saveFilePart` (debug print). If none is sent, the
  pinning must come from the pre-loaded login cookie (Approach A).
- **E. Sticky by source-port/TLS.** Unlikely, but if no cookie pins, the LB may use
  TLS session resumption — reuse one `ssl` context + connection (keep-alive already
  does this); if that were it, keep-alive would have fixed it (it didn't), so deprioritize.

## Wired into the Telegram bot
`bot/runner.py` now routes on the engine setting for **sending** as well, not only
contact building:
- `engine=direct` → `_send_job_direct`, which takes its targets from `peers.py` and
  sends through `sender.py`. All blocking HTTPS runs in worker threads
  (`asyncio.to_thread`) so the panel stays responsive.
- `engine=bridge` → the unchanged, proven tweb path, which now also harvests peers
  as a side effect.

Everything is imported LAZILY from `bot/`, so deleting `direct/` still leaves the
browser bot fully working.

## Status (live-verified on the server)
- ✅ Transport + envelope proven (`direct-replay` returned Eitaa's DC config).
- ✅ Browser-free **send text** to self AND to a contact — `updateShortSentMessage`.
- ✅ Browser-free **import contact** — `contacts.importedContacts`.
- ✅ Browser-free **learn a contact peer** (`direct-capture-peer`).
- ✅ Browser-free **send file** (saveFilePart+sendMedia) — root cause was that media must
  go to the dedicated media host (`fateme.eitaa.com`), not the API host; `extract_media_url`
  routes it there. Proven live, including **multi-part**: a 9,494,529 B zip went out as
  19 parts, uploaded once.
- ✅ **`.apk` sending** — Eitaa filters the *MIME*, not the name or the bytes.
  `direct/apk_mode.py` sends `.apk` as `application/octet-stream` with the real name in
  `documentAttributeFilename`. Toggle `📦 APK send mode`, OFF by default. Full story in
  `docs/APK_SEND_STATUS.md`. (The old `error 400: PEER_ID_INVALID` on apk was this MIME
  filter, not a peer problem.)
- ⛔ Browser-free **`contacts.importContacts`** — settled dead end: Eitaa replies with a
  4-byte payload-less `cid=0xdc252379`, identically for `+98…` and `98…`. Contact
  building hands over to the bridge automatically. Do not re-investigate.

## HANDOFF — continue from here (updated 2026-08-13)

**→ For priorities, read `docs/ROADMAP.md`.** It is the canonical roadmap; this section
is only the operating context for working on `direct/`.

State of the world:
- Text send, file send (incl. multi-part), contact import via bridge handover, and
  peer-learning are all DONE. There is no open blocker in `direct/`.
- The engine is reachable from the panel as `hybrid` (direct HTTPS with the page as a
  per-recipient fallback) and, behind `MKWL_ENABLE_DIRECT=1`, as pure `direct`.
- The two remaining `direct/`-specific improvements, both optional:
  1. Source token1/token2 from the session export (localStorage `token`) instead of the
     newest capture, which would remove the staleness window `MKWL_CONTEXT_MAX_AGE_H`
     currently papers over.
  2. Reverse the Eitaa-specific `User` row in `contacts.importedContacts` — the last
     thing forcing a browser pass for contacts.
- Everything is ISOLATED in `direct/`; deleting the folder reverts the project. The
  browser bot still works and is untouched.
- The assistant cannot run live (no Playwright/network in the sandbox) — it compiles and
  runs the offline suites (`python -m direct.tests.test_direct` and the 15 under
  `bot/tests/`; all 16 pass). The user runs live on the server and pastes output back.
- Secrets (tokens/cookies/peers) live ONLY in gitignored `artifacts/sessions/*.json` on
  the server, never in the repo or learnings.

Artifacts the direct client reads (all gitignored, per account):
- `artifacts/sessions/capall_<acct>_*.json` or `worker_tx_<acct>_*.json` — session
  constants (token1/token2/self-peer), newest by mtime.
- `artifacts/sessions/peers_<acct>.json` — learned contact peers (name -> peer).
- `artifacts/sessions/cookies_<acct>.json` — exported eitaa cookies (name -> value).

## Honest error reporting
`eitaa_tl.classify_response()` decodes Eitaa's own error wrapper
`eitaa_error#c4b9f9bb (code:int, message:string)` and the standard rpc_error, so a
failed call prints e.g. `error 400: PEER_ID_INVALID` / `error 500: INTERNAL_SERVER_ERROR...`
instead of a false success.
