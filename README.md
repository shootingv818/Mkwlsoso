# Mkwlsoso — Eitaa Web Capture (Phase 1)

A capture-first foundation for building an **Eitaa account manager / contact
sender** on top of Eitaa Web. Phase 1 does not send anything on its own: it
records exactly what the web client does for each operation you perform, so the
sending engine (Phase 2+) can be built on real, verified behavior.

> Scope: this tool automates **your own accounts only**. It does **not** bypass
> login, OTP, CAPTCHA, or rate limits, and it never stores secrets in clear text.

## What it does

For a single operation (e.g. sending one contact card) it records:

- HTTP requests/responses (redacted)
- WebSocket connections and frames (redacted)
- console / page errors
- before/after screenshots and a Playwright trace
- an **idle baseline** first, so background noise can be separated from the action

Then `analyze` produces a `report.md` that highlights the likely request(s)
behind the action and recommends a Browser-driver or Hybrid approach.

## Requirements

- A server or machine **with internet access** (this environment has none).
- Python 3.11+
- A **visible browser** for manual login. On a headless server use `xvfb`
  (e.g. `xvfb-run`) or a noVNC desktop, or set `HEADED=0` only after the
  session already exists.

## Setup (on your server)

```bash
git clone https://github.com/shootingv818/Mkwlsoso.git
cd Mkwlsoso
bash setup.sh
source .venv/bin/activate
cp .env.example .env   # then edit EITAA_WEB_URL if needed
```

## Usage

1. Log in once (session is saved to an isolated profile per account):

   ```bash
   python cli.py login --account test1
   ```

2. Capture one operation. You do nothing during the baseline, then perform the
   single action in the browser using the printed marker text, then press ENTER:

   ```bash
   python cli.py capture --account test1 --op send_text
   ```

3. Build the report:

   ```bash
   python cli.py analyze --run <run_id>
   ```

4. List runs:

   ```bash
   python cli.py list
   ```

## Output layout

```
artifacts/<run_id>/
├── meta.json          run metadata + event counts
├── events.jsonl       all events, globally ordered
├── http.jsonl         http requests/responses (redacted)
├── ws.jsonl           websocket frames (redacted)
├── before.png
├── after.png
├── trace.zip          open with: python -m playwright show-trace trace.zip
└── report.md          analysis (created by `analyze`)
```

`profiles/`, `artifacts/`, and `.env` are gitignored and must never be committed.

## Safety notes

- Cookies, tokens, OTP codes, passwords, phone numbers, and message content are
  redacted before anything is written to disk. Field names, shapes, sizes, and
  stable hashes are kept so captures remain comparable.
- Use a dedicated **test account** and a **test chat** you control.
- Enter the login code yourself in the browser; never paste it into chat or code.

## Broadcaster (tabchi)

Capture confirmed Eitaa Web sends over an encrypted MTProto-like binary
transport (no WebSocket, no plain JSON), so the sender drives the real web UI
(Browser-driver). The broadcaster is controlled and restart-safe.

### 1. Send one message (test)

```bash
DISPLAY=:99 python cli.py send --account test1 --to "نام چت" --text "سلام"
```

### 2. Collect recipient names

```bash
DISPLAY=:99 python cli.py collect --account test1 --out recipients_all.txt --users-only
```

Scrolls the whole chat list and writes names to `recipients_all.txt`. `--users-only`
excludes groups/channels. **Edit the file** to keep only the recipients you want.

### 3. Run a campaign

```bash
DISPLAY=:99 python cli.py campaign --account test1 --file recipients_all.txt --text "متن پیام"
```

It sends to each recipient with a humane random delay, checkpoints after every
one, and prints progress. Note the printed `job_id`.

### 4. Status / stop / resume

```bash
python cli.py campaign-status --job <job_id>
python cli.py campaign-stop   --job <job_id>     # stops after the current recipient
DISPLAY=:99 python cli.py campaign --account test1 --resume <job_id>
```

### Send pacing (in .env)

The Telegram panel and the CLI campaign have SEPARATE pacing settings.

```
# panel (bot/runner.py)
TEXT_SEND_DELAY=3          # seconds between sends (between batches when SEND_CONCURRENCY > 1)
SEND_CONCURRENCY=1         # recipients in flight at once on the fast path (1-10)
MAX_FLOOD_WAIT=90          # obey a server-declared wait up to this, then continue

# CLI campaign only (jobs/campaign.py) - the panel ignores these
SEND_MIN_DELAY=8
SEND_MAX_DELAY=18
SEND_BATCH_SIZE=20
SEND_BATCH_COOLDOWN=90

MAX_CONSECUTIVE_FAILURES=5 # both
```

The campaign is restart-safe: if the server reboots or the process is killed,
`--resume <job_id>` continues from the first pending recipient and never
re-sends to anyone already marked `sent`.

## Roadmap

**→ `docs/ROADMAP.md` is the canonical roadmap.** It lists what is done, what is
built but not yet live-verified, what is only scaffolded, and what comes next.

Phases delivered so far:

- **Phase 1:** capture + analyze real operations. (done)
- **Phase 2:** Browser-driver `send_text` + inspect. (done)
- **Phase 3:** controlled, restart-safe broadcaster (collect/campaign). (done)
- **Phase 4:** Telegram control panel (`bot/`) — accounts, login, contact cache,
  live cards, resume ledger. (done)
- **Phase 5:** three send engines behind one interface (`bridge` / `hybrid` /
  `direct`), including browser-free text and multi-part file send. (done)
- **Phase 6:** feature packages — contact boost, photo export, session check,
  warm path, APK send mode. (built; several still off by default)
- **Phase 7:** web login portal, central log group, distributed worker fleet.
  (in progress — the portal and log group are wired but off by default; the
  worker fleet is a tested core plus unwired scaffolds)
