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

## Roadmap

- **Phase 1 (this):** capture + analyze real operations.
- **Phase 2:** `EitaaClient` (login/session/contacts/send) via Browser-driver or
  Hybrid, chosen from the capture results.
- **Phase 3:** multi-account management + a controlled, restart-safe send queue
  (start/pause/resume/stop, rate limiting, de-dup, checkpoints).
