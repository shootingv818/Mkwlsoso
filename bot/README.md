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

## Paging — every list is 10 per page
Account lists never grow into a wall of buttons: they show **10 entries per
page** with a `◀ 1/3 ▶` row. Applies to the Accounts list and the
multi-account selection list.

## Accounts & the per-account panel
`Accounts` lists each account **by its own phone number and nothing else**.
Tapping a number opens that account's panel directly (it becomes active — no
separate tick step):
- 📤 **Send** — broadcast the stored content to ALL contacts
- ➕ **Build Contacts** — build contacts from a prefix
- 🔑 **Harvest Peers** — read this account's EXISTING contacts once through the
  browser and save their peers, which is what makes the fast (no-browser) engine
  usable on an account whose contacts were added earlier
- 🔄 **Refresh** — recompute contacts + chats
- ⏹ **Stop** (when a job is running)
- 🗑 **Delete Account** — asks to confirm, then removes the account's browser
  profile, its saved peers and its captured session

The panel shows the account's phone, contacts, chats and **Peers** (how many
contacts the browser-free engine can reach).

## Add Account — the number IS the account
Phone → code, both in Telegram (bridge login, no noVNC). **No name is asked
for**: the profile is named after the phone digits (`0930…`, `930…` and
`+98930…` all map to the same account `98…`). Adding a number that already
exists says so instead of creating a duplicate. On success the
**✅ ACCOUNT ADDED** card shows the account's contacts + chats count.

## 🚀 Multi-Account Send (simultaneous)
From `Accounts` → **Multi-Account Send**: tick several accounts (☑ / ☐, 10 per
page), then **Send from N account(s)**. They all run **at the same time**, each
to its own contacts, and they report into **ONE live card** showing the
**combined** progress plus a per-account breakdown:

```
📤 Multi-Account Send — Live
-------------------------------
• Accounts : 3
• Current  : 989360330317
• Engine   : direct
• Type     : Text
• Status   : 🟢 Sending
• Sent     : 412 of 1500 — 27%
• Failed   : 3
• Elapsed  : 00:02:11
-------------------------------
🟢 989360330317 · 210/500
🟢 989121112233 · 202/500 · ✗3
⏳ 989351234567 · 0/500
🕒 2026-07-25 05:05:52
```

`Sent … of …` is every selected account's contacts added together, so the top
block answers "how far along is the whole job". Accounts already running a job
are skipped and named in the queued card. One combined
**✅ MULTI-ACCOUNT SEND FINISHED** card is posted at the end.

## Engines (Settings → 🔧 Engine)
- **bridge** — drives Eitaa Web (tweb) in headless Chromium (default).
- **direct** — browser-free MTProto (`direct/`). Now used for **sending too**,
  not only contact building:
  - **Send** → `messages.sendMessage` / `sendMedia` straight over HTTPS. A file
    is uploaded **once** and re-sent to every recipient with no re-upload.
  - **Build Contacts** → NOT served browser-free. Eitaa answers `importContacts`
    off the browser path with a 4-byte reply `cid=0xdc252379` (settled live on
    2026-07-25; both phone formats, so the format was never the cause). The job
    detects this and **hands over to the bridge path automatically**, so contact
    building works no matter which engine is selected.
  - Requires a session capture for the account, and **saved peers** for the
    targets. With neither, the job posts a clear error saying what to do rather
    than silently sending nothing.

### Peers — what makes fast send possible
A browser-free send needs each contact's `user_id` **+ `access_hash`** (the
20-byte peer). Those are now harvested and stored automatically:
- while **building contacts** — `importContacts` returns the matched users with
  their `access_hash`;
- while **sending with the bridge** — Eitaa's own peer manager is asked to
  resolve the collected contacts.

Every harvest posts a **🔑 PEERS SAVED** card. Peers live in the gitignored
`artifacts/sessions/peers_<account>.json`, owned by `direct/peers.py`.

The direct engine can count imported contacts but cannot safely read their
`access_hash` (Eitaa's `User` row constructor is unknown, and guessing it would
create silently-wrong peers). When it imports contacts it says so with an
**ℹ️ PEERS NOT HARVESTED** card: run Build Contacts or one Send with the bridge
engine to harvest them.

## Contact building — probe instead of a silent zero
Both engines probe. The **bridge** path can fall back to the per-number UI add
flow when nothing matches; the **direct** path has no browser to fall back to, so
it keeps scanning with `+98` and says so in the card. The direct path also
verifies the reply really was `contacts.importedContacts` — an unexpected reply
constructor used to look identical to "imported 0" and is now reported as
`unexpected_reply` with its cid + head.

Contact building used to race through and report `Found: 0` with no reason. The
cause is that the server matches **nobody** and returns no error when the phone
format is not the one the build expects. So the first batch is now **probed in
both formats** (`98…` and `+98…`) and the raw answer is posted:

```
🔬 IMPORT PROBE
-------------------------------
• Account   : 989360330317
• Format 98 : imported=0 users=0 retry=0 of 50
• Format +98: imported=7 users=7 retry=0 of 50
-------------------------------
Using phone format +98 for the rest of this job.
```

If **neither** format matches anyone, the job automatically falls back to the
proven one-by-one UI add flow, which reports a real per-number reason. Rate
limits abort instead of falling back.

## Live cards (edited in place)
Contact build and single-account send each update ONE card in place, e.g.:

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

Errors, probes, peer harvests and limit detections post their OWN separate cards
(never overwriting a live card) and include Trace / Phase / Engine / Account /
Target / Code / Detail / Time.

## Contact naming
Imported contacts are named after the **account's own phone number** (no manual
name needed).

## Batch contact building
Both engines build in batches (bridge: tweb `importContacts` in 50s; direct:
HTTPS `importContacts` in 100s) — never the slow one-by-one UI add (that remains
only as a last-resort fallback).

## Settings
Engine · Text send delay · Contact create delay (default 0.2s, fast) · Log every N.

## Files
- `app.py` — panel UI, callbacks, conversation flow, `LiveCard`, paging helpers,
  account naming/deletion, server ping.
- `runner.py` — `JobManager`: send (bridge + direct), contacts (bridge + direct),
  multi-account `run_send_multi` + `AggregateProgress`, peer harvesting, bridge
  login, restriction detection, live-card updates.
- `cards.py` — English cards incl. `live_contacts`, `live_send`,
  `live_send_multi`, `multi_send_finished`, `contacts_probe`, `peers_saved`,
  `account_added`, `account_deleted`, `account_panel`, `panel_home`, `error_card`.
- `store.py` — persistent settings (incl. `engine`), content, active account,
  multi-account selection, and per-account metadata (phone/contacts/pvs).

## Revert
Everything browser-free lives in `direct/` and is imported **lazily**, so
deleting that folder leaves the browser bot working. To drop this whole update,
switch back to the `feat/eitaa-web-capture` branch.
