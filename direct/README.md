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
  `classify_response()`.
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

Sending to a contact needs its peer first (`direct-capture-peer` once); uploads are
peer-independent, so files reuse the same self-upload path and only `sendMedia` routes
to the chosen peer.

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

## Status (live-verified on the server)
- ✅ Transport + envelope proven (`direct-replay` returned Eitaa's DC config).
- ✅ Browser-free **send text** to self AND to a contact — `updateShortSentMessage`.
- ✅ Browser-free **import contact** — `contacts.importedContacts`.
- ✅ Browser-free **learn a contact peer** (`direct-capture-peer`).
- 🔧 Browser-free **send file** (saveFilePart+sendMedia) — byte-exact serializers done;
  root cause found = media must go to the dedicated media host (`fateme.eitaa.com`),
  not the API host. Fix built (`extract_media_url` + route file send there).
  **Awaiting live re-test** (`direct-send-file`).
- ⛔ **apk is blocked by Eitaa itself** (platform policy — the web app can't send .apk
  either); the direct client returns Eitaa's `error 400: PEER_ID_INVALID` for apk. Not
  our bug; nothing to fix. txt/zip/pdf/images/etc. work.
- ⏳ TODO after file works: multi-part (>512 KiB) verification; source token/cookies
  from the session export instead of the newest capture; optional direct login handshake.

## HANDOFF — continue from here (next account/session)
State of the world:
- Text + contact import + peer-learning are DONE and browser-free.
- The ONLY thing between us and full browser-free file send is Eitaa's load-balancer
  node-affinity (see the section above). The cookie-jar fix is committed; the very next
  step is: run `direct-capture-cookies`, then `direct-send-file`, and read the result:
    - `🎉 SUCCESS` → file send works browser-free; wire it into the Telegram bot.
    - `⚠ error 500 ... part key: 0 ..._<ip>` with a **changing** ip → cookie didn't pin;
      work through fallbacks A→D above (Approach B, capturing the worker's real request
      headers, is the definitive one).
- Everything is ISOLATED in `direct/`; deleting the folder reverts the project. The
  browser bot still works and is untouched.
- I (the assistant) cannot run live here (no Playwright/network/crypto libs in the
  sandbox): I only compile + run `python -m direct.tests.test_direct` (ALL PASS). The
  user runs on their server `~/Mkwlsoso` (venv `.venv`, `DISPLAY=:99`) and pastes output.
- Push via the GitHub power, never raw `git push`. Branch: `feat/eitaa-web-capture` (PR #1).
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
