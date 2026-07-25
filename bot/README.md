# bot/ — Telegram control panel (v2)

Owner-only Telethon panel that drives the Eitaa manager. English ReconBot-style
cards; same boxed layout, just cleaner.

## Home (`/start`)
Shows live status: **Bot** (online), **Engine**, **Server** ping (TCP RTT to the
Eitaa host), **Version**, **Accounts**, **Active**. Buttons:
- 👤 **Accounts** — list of accounts (by phone number)
- ➕ **Add Account** — add a new account (no noVNC)
- 📝 **Content** — set the text/file to send
- ⚙ **Settings**

(Removed: Help, the standalone Stats button, and "Add via noVNC".)

## Accounts & the per-account panel
`Accounts` lists each account **by its own phone number**. Tapping a number
opens that account's panel directly (it becomes active — no separate tick step):
- 📤 **Send** — broadcast the stored content to ALL contacts (bridge fast-path)
- ➕ **Build Contacts** — build contacts from a prefix (uses the selected engine)
- 🔄 **Refresh** — recompute contacts + chats
- ⏹ **Stop** (when a job is running)

The panel shows the account's phone, contacts and chats counts.

## Add Account
Name → phone → code, all in Telegram (bridge login, no noVNC). On success the
**✅ ACCOUNT ADDED** card shows the account's **contacts + chats count**, and the
phone/counts are stored (so the Accounts list and panels show them).

## Engines (Settings → 🔧 Engine)
- **bridge** — drives Eitaa Web (tweb) in headless Chromium (default).
- **direct** — browser-free MTProto (`direct/`). Used for **contact building**
  (`contacts.importContacts` in batches over HTTPS, no browser). Requires a
  session capture for the account (`artifacts/sessions/capall_*`/`worker_tx_*`);
  if absent, the job posts a clear error telling you to capture or use bridge.
- **Sending** always uses the proven bridge fast-path regardless of the setting
  (direct send-to-all needs a contact-peer sync that isn't built yet).

## Live cards (edited in place)
Contact build and send each update ONE card in place, e.g.:

```
🔎 Discover Friends by Prefix — Live
-------------------------------
📱 989360330317
• Prefix  : 09164
• Engine  : direct
• Status  : 🟢 Searching
• Found   : 4 of 1200 — 0%
• Probed  : 14
🕒 2026-07-25 05:05:52
```

Send uses the same shell (`📤 Send to Contacts — Live`). Errors and limit
detections post their OWN separate cards (never overwrite the live card) and now
include Trace / Phase / Engine / Account / Target / Code / Detail / Time.

## Contact naming
Imported contacts are named after the **account's own phone number** (no manual
name needed).

## Batch contact building
Both engines build in batches (bridge: tweb `importContacts` in 50s; direct:
HTTPS `importContacts` in 100s) — never the slow one-by-one UI add (that remains
only as a last-resort bridge fallback).

## Settings
Engine · Text send delay · Contact create delay (default 0.2s, fast) · Log every N.

## Files
- `app.py` — panel UI, callbacks, conversation flow, `LiveCard`, server ping.
- `runner.py` — `JobManager`: send / contacts (bridge + direct) jobs, bridge
  login, restriction detection, live-card updates.
- `cards.py` — English cards incl. `live_contacts`, `live_send`, `account_added`,
  `account_panel`, `panel_home`, richer `error_card`.
- `store.py` — persistent settings (incl. `engine`), content, active account,
  and per-account metadata (phone/contacts/pvs).
