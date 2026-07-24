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

## Not done yet (needs the user's authorized account, live)
- Network transport (HTTPS POST to the Eitaa DC shards) + obfuscation/framing.
- The layer-135 TL schema for the actual methods (messages.sendMessage, etc.).
- Finalizing `session.load_export()` against the **real** exported key names
  (pinned from one `bridge-export-session` run).
- First live call: reuse the session and read config/contacts with the browser
  CLOSED.

Everything here is deterministic and was validated without a network.
