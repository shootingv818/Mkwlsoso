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

## Status
- ✅ Transport + envelope proven live (`direct-replay` returned Eitaa's DC config).
- ✅ Browser-free **send text** — live-confirmed (`updateShortSentMessage`).
- ✅ Browser-free **import contact** — live-confirmed (`contacts.importedContacts`).
- ✅ Browser-free **send file** (saveFilePart+sendMedia) — byte-exact serializers;
  single-part path built, multi-part loop built. **Pending live test** for txt/zip/apk.
- ✅ **send to a contact** via captured peer — built. **Pending live test.**
- ⏳ TODO: confirm token stability across browser restarts and source token from the
  session export (localStorage `token`) instead of the newest capture; direct login
  handshake; larger/multi-part file verification.
