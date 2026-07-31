# APK send — investigation, root cause, fix, and roadmap

## Where we are (2026-07-31)

The bot is deployed and working on a US server (Santa Clara): online, connected
to Telegram, logged in to Eitaa, text + normal files (zip/pdf/txt) send fine.
The one broken case — **sending `.apk` files** — is now fully diagnosed and the
fix is proven end-to-end. The fix itself is NOT yet wired into the product code
(awaiting go-ahead); everything below the diagnostics line is confirmed by live
tests to the owner's own account and contacts.

## The problem, in one line

Eitaa **blocks a document whose MIME is `application/vnd.android.package-archive`**
(the real apk MIME). It does not filter on the `.apk` filename or the file
content — only the MIME.

## How we proved it (live, own account only)

Isolated diagnostics under `deploy/` sent identical bytes to Saved Messages,
changing only name/MIME:

| case | filename | MIME | result |
|---|---|---|---|
| control | `control.zip` | zip | delivered ✅ |
| apk / real MIME | `app.apk` | `application/vnd.android.package-archive` | **blocked ❌** |
| apk / octet | `app.apk` | `application/octet-stream` | delivered ✅ |
| apk bytes named .zip | `x.zip` | zip | delivered ✅ |
| neutral | `app.bin` | octet | delivered ✅ |

Only the real-apk-MIME case failed → the filter is the MIME, nothing else.

Second confirmation — this server's `mimetypes.guess_type("x.apk")` returns
`application/vnd.android.package-archive` (Debian/Ubuntu ship that mapping in
`/etc/mime.types`). That is exactly the MIME the bot's `bridge_file_init` puts on
the wire, so the bot's apk sends were being blocked at the source.

Final confirmation — a real end-to-end run (`deploy/apk_confident_test.py`):
uploaded a 3 MB `.apk` via the bot's real `bridge_file_init` with the MIME forced
to octet-stream, then delivered with the bot's real `bridge_file_send`:

```
upload: ok=True
Saved Messages : SENT ✅ msg_id=2452
رسول عابدی      : SENT ✅ msg_id=2453
خواجوی گوست     : SENT ✅ msg_id=2454
(one contact FAILED with PEER_ID_INVALID — a stale peer, unrelated to apk)
```

## The competitor's trick (matches our finding)

The competitor log the owner shared showed: `mime=application/octet-stream`,
single `documentAttributeFilename`, size ~10 MB. i.e. it uploads the apk as a
generic binary and keeps the real `.apk` name in the filename attribute — so
Eitaa's MIME filter never triggers and the recipient still gets a valid apk.
Our fix does the same thing.

## The fix (single point, isolated, opt-in)

- **Location:** `eitaa/driver.py` → `bridge_file_init()`, the line
  `mime = _mt.guess_type(file_path)[0] or "application/octet-stream"`.
- **Change:** when APK send-mode is ON and the file is `.apk`, use
  `application/octet-stream` instead (the real `.apk` name stays in the filename
  attribute). Reuse the existing `direct/apk_mode.py` helper so there is one
  policy for both engines.
- **Isolation:** defensive — any failure falls back to the normal MIME; OFF =
  byte-identical to today; zip/pdf/txt unaffected.

## What is already built

- `direct/apk_mode.py` — isolated, opt-in octet policy (stdlib only, defensive).
- Settings panel toggle `📦 APK send mode` (default OFF) + persistence + live env.
- Applied to the **browser-free (direct)** path already.
- Full offline suite green 3× consecutively; isolation audit passed.

## What is NOT done yet (remaining work)

1. **Wire APK mode into the bridge (browser) path** — the path the bot actually
   uses for files. One line in `bridge_file_init` via `apk_mode.effective_mime`.
   (This is the actual fix; the toggle already exists but currently only affects
   the direct path.)
2. Tests for the bridge path: apk ON→octet, OFF→unchanged, zip/pdf untouched,
   fault-injection; run the full offline suite 3×.
3. New branch + rollback tag before the change (per project convention).
4. Optional: a small retry on `locate_failed` (the browser upload occasionally
   needs a second try due to load-balancer node routing on this server).

## Diagnostics added (read-only, deliver only to Saved/own contacts)

`deploy/apk_diag.py`, `deploy/eitaa_deep_probe.py`, `deploy/apk_reject_probe.py`,
`deploy/upload_sweep.py`, `deploy/apk_bridge_test.py`, `deploy/apk_realsend_test.py`,
`deploy/apk_confident_test.py`. None change product code.

## Session progress log

- Chose the US server after measuring 3 servers; Eitaa reachable, ~0.3–0.8s RTT,
  concurrency scales ~linearly (conc=6 ≈ 4.8 req/s), 20 parallel all HTTP 200.
- Recorded the network architecture decision in `docs/NETWORK_ARCHITECTURE.md`.
- Fixed a real crash: `.env` loader didn't strip inline comments (bot died on a
  copied `.env.example`). Fixed + tested on branch `fix/dotenv-inline-comments`.
- Built the deploy scripts: `deploy/full_setup.sh` (one-shot install+run) and the
  offline test runner.
- Diagnosed and proved the apk fix (this document).
