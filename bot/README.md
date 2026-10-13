# bot/ — Telegram control panel

Owner-only Telethon panel that drives the Eitaa manager. **Bridge-only**: every
job runs through the proven browser (tweb) path.

## Home (`/start`)
```
🤖 EITAA MANAGER
-------------------------------
• Bot      : 🟢 online
• Eitaa    : 🟢 84 ms
• Accounts : 8
• Contacts : 2,140
• Active   : 989304683887
• Running  : ⏳ 1 job(s)
• Content  : Text (42 chars): سلام …
• Version  : 1.0
-------------------------------
```
Four sections: 👤 **Accounts** · 🚀 **Multi Send** · 📝 **Content** · ⚙ **Settings**
(plus ➕ Add Account and 🔄 Refresh).

`Contacts` is the total number of **saved** contacts across all accounts — the
number a send actually delivers to.

## Paging — 10 per page
Every account list shows 10 entries per page with a `◀ 1/3 ▶` row, and each row
carries the account's saved contact count:

```
🟢 989304683887 · 350
⏳ 989121112233 · 512      ⏳ = a job is running
•  989153222956            no contacts saved yet
```

## 👤 Accounts → account panel
- 📤 **Send** — send the stored content to this account's contacts
- ➕ **Build Contacts** — create contacts from a mobile prefix
- 📥 **Save Contacts** — collect this account's full contacts list ONCE and cache it
- 🔄 **Refresh** — recompute contacts + chats from Eitaa
- ⏹ **Stop** (only while a job runs)
- 🗑 **Delete Account** — with confirm; removes the browser profile, saved
  contacts, saved peers and captured session

## 📥 Save Contacts — why it exists
Eitaa's contact list is virtualized, so reading it means scrolling the browser
for minutes. That used to happen at the **start of every single send**. Now it is
done once, cached in `DATA_DIR/contacts_<account>.json` (title + peer_id only,
no phone numbers), and every later send starts delivering immediately.

A send still works without it — it collects and saves the list on the way — but
the first one is slow.

## 🚀 Multi Send — its own section
A top-level section (not buried inside the accounts list). Tick accounts, see the
combined reach before starting, and send from all of them **at the same time**:

```
🚀 MULTI SEND
-------------------------------
• Accounts : 3 of 8 ticked
• Reach    : 1,180 contacts
• Content  : Text (42 chars): سلام …
• Delay    : 3s between messages
-------------------------------
Ready: 3 accounts → 1,180 contacts, all running at once into ONE live card.
```
Buttons: per-account ☑/☐ toggles (10 per page), `🚀 Send · 3 acct · 1,180 contacts`,
☑ All, 🧹 Clear.

All of them report into **ONE** live card with the combined totals plus a
per-account breakdown:

```
🚀 MULTI SEND — Live
-------------------------------
▰▰▰▰▰▱▱▱▱▱▱▱  38%
• Status   : 🟢 Sending
• Accounts : 3 · ✅3
• Now      : 989304683887
• Sent     : 177 of 470
• Failed   : 3
• Elapsed  : 00:06:52
• Left     : 00:11:03
-------------------------------
🟢 989304683887 · 137/350 · ✗3
🟢 989153222956 · 40/120
⏳ 989121112233 · 0/512
```

Accounts already running a job are skipped and named in the queued card. Accounts
that could not send at all are counted separately (`🚧 n no peers`) so a mostly
failed run can never look like a green success. One combined summary card closes
the run.

## Live cards
Send and contact-build each edit ONE card in place, with a progress bar, elapsed
time and an ETA from the pace so far. Errors, probes and limit detections post
their OWN separate cards (never overwriting a live card) and include
Trace / Where / Phase / Account / Target / Code / Detail / Time.

## Add Account — the number IS the account
Phone → code, both in Telegram (no noVNC, no name to invent). The profile is
named after the phone digits, so `0930…`, `930…` and `+98930…` all map to the
same account. Re-adding an existing number is refused.

2FA accounts are detected but not supported yet.

## Contact building
Batched `contacts.importContacts` through the browser. The first batch is probed
in both phone formats (`98…` / `+98…`) and the raw counts are posted as a
**🔬 IMPORT PROBE** card; if neither format matches anyone the job falls back to
the proven per-number add flow, which reports a real reason per number. Rate
limits abort instead of falling back. Imported contacts are named after the
account's own phone number.

## ⚙ Settings
Send delay · Contact delay · Log every N. Longer delays are safer against
Eitaa's rate limits.

## The engine switch (hidden)
The panel is bridge-only, so there is no Engine button. `direct/` is still fully
present in the source and its CLI commands still work — the browser-free **file**
send is proven live (a 9.5 MB zip in 19 parts, uploaded once). Only browser-free
**contact import** turned out to be unsupported by Eitaa (see PROJECT_STATUS.md).

To bring the switch back into Settings:
```
MKWL_ENABLE_DIRECT=1     # in .env
```
`store.engine` then honours the stored choice again; the stored value is never
overwritten while the flag is off.

## Files
- `app.py` — panel UI, callbacks, conversation flow, `LiveCard`, paging, account
  naming/deletion, server ping.
- `runner.py` — `JobManager`: send, contact build, save-contacts, multi-account
  `run_send_multi` + `AggregateProgress`, peer harvesting, bridge login,
  restriction detection.
- `cards.py` — all cards, plus the `bar()` / `eta()` helpers.
- `store.py` — settings, content, active account, multi-select, per-account meta.
- `contacts_store.py` — the per-account contacts cache.

## Revert
Everything browser-free lives in `direct/` and is imported lazily, so deleting
that folder leaves the bot working. To drop this whole update, switch back to the
`feat/eitaa-web-capture` branch.
