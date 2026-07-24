# direct/ — headless Eitaa client (foundation)

An **isolated, experimental** MTProto client that talks to Eitaa over HTTPS
**without a browser**. Goal: kill the Chromium RAM/CPU cost and the crashes,
run many accounts on a small server, and send faster.

## Isolation (important)
- `direct/` imports **nothing** from `bot/`, `eitaa/`, or `capture/`.
- The working browser bot does **not** import from `direct/`.
- If this experiment breaks anything, **delete the `direct/` folder** and the
  project is exactly back to square one. Nothing else depends on it.

## What's built + verified offline (unit tests pass)
- `aes.py` — AES (prefers pycryptodome/`cryptography`, pure-Python fallback).
  Matches the NIST AES-256 known-answer vector.
- `crypto.py` — AES-IGE, SHA-1/256, MTProto 2.0 `kdf`, `auth_key_id`, `msg_key`.
- `tl.py` — TL wire primitives (int/long/int128/256/bytes/string/vector/…).
- `mtproto.py` — encrypted message envelope build/parse with `msg_key`
  verification and tamper detection.
- `session.py` — `Session` model + flexible loader for the exported browser
  session.
- `errors.py` — RpcError / FloodWait / SecurityError.

Run the tests:
```
python -m direct.tests.test_direct
```

## The bridge from the browser
`python cli.py bridge-export-session --account <name>` dumps the browser
profile's MTProto session (auth_key / server_salt / DC / user id) from
IndexedDB into a **gitignored** `artifacts/sessions/*.json`. The direct client
reuses that authorized session — **no re-login**.

## Network layer (built; verified offline, first live call pending)
- `dc.py` — dc_id -> Eitaa shard URL (env `MKWL_DC_HOSTS` override) + api creds.
- `transport.py` — HTTPS POST MTProto transport (stdlib only).
- `service.py` — parses core MTProto service messages (rpc_result/rpc_error/
  bad_server_salt/msg_container/gzip_packed/new_session_created/pong/…).
- `schema.py` — minimal encoders (invokeWithLayer/initConnection/help.getConfig/
  users.getUsers). Layer-sensitive ids flagged; adjusted from a real capture if off.
- `client.py` — `DirectClient`: load session -> connect -> invoke (salt/seq
  bookkeeping, bad_server_salt retry) -> decrypt+parse.

Session layout is CONFIRMED: browser localStorage holds `dc`, `user_auth`
{dcID,id}, `dc<N>_auth_key` (512-hex), `dc<N>_server_salt` (16-hex).

## How to bring it live (on the authorized server, browser can be closed for the probe)
1. `python cli.py bridge-export-session --account <acct>`   → session JSON.
2. `python cli.py direct-capture-transport --account <acct>` → pins the real DC URL.
3. `python cli.py direct-probe --session artifacts/sessions/<acct>_*.json --url <captured>`
   → first live `help.getConfig`. rpc_result = the headless client talks to Eitaa. 🎉

Everything deterministic here is validated by `python -m direct.tests.test_direct`
(AES NIST vector, IGE, MTProto envelope, TL, session loader, service parser,
schema wrap, transport URL). Only the live round-trip needs the server.
