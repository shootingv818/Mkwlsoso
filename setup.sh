#!/usr/bin/env bash
# One-time setup for the Eitaa web capture tool.
# Run this on YOUR server (needs internet access to download Chromium).
set -euo pipefail

echo "[setup] creating virtualenv (.venv)"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[setup] installing python dependencies"
pip install --upgrade pip
pip install -r requirements.txt

echo "[setup] installing Chromium + system deps for Playwright"
python -m playwright install --with-deps chromium

if [ ! -f .env ]; then
  echo "[setup] creating .env from .env.example"
  cp .env.example .env
fi

echo "[setup] done."
echo "Next:"
echo "  source .venv/bin/activate"
echo "  python cli.py login --account test1     # log in manually in the browser"
echo "  python cli.py capture --account test1 --op send_text"
echo "  python cli.py analyze --run <run_id>"
