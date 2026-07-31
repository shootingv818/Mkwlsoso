#!/usr/bin/env bash
# Mkwlsoso - full one-shot install + run.
# Clones (if needed), installs system + python deps, Chromium, Xvfb, writes .env,
# runs the offline test suite, and starts the bot under systemd if credentials
# are present. Safe to re-run.
#
#   BRANCH=feat/hybrid-engine-live   branch to deploy (default)
#   PAT=ghp_xxx                      only if the repo is private
#   SKIP_TESTS=1                     skip the 3x offline suite
#   SKIP_CHROMIUM=1                  do not download Chromium (~450MB)
set -uo pipefail

OWNER=shootingv818; NAME=Mkwlsoso
BRANCH="${BRANCH:-feat/hybrid-engine-live}"
PAT="${PAT:-}"
DIR="${DIR:-$HOME/$NAME}"
LOG="$HOME/mkwlsoso-setup.log"
if [ -n "$PAT" ]; then URL="https://$PAT@github.com/$OWNER/$NAME.git"
else URL="${REPO_URL:-https://github.com/$OWNER/$NAME.git}"; fi

exec > >(tee -a "$LOG") 2>&1
say(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

say "1) System packages"
APT="git curl ca-certificates build-essential python3 python3-pip python3-venv python3-dev libffi-dev libssl-dev zlib1g-dev sqlite3 jq tmux tzdata xvfb"
RPM="git curl ca-certificates gcc gcc-c++ make python3 python3-pip python3-devel libffi-devel openssl-devel zlib-devel sqlite jq tmux tzdata xorg-x11-server-Xvfb"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq --no-install-recommends $APT || die "apt failed"
elif command -v dnf >/dev/null; then $SUDO dnf install -y -q $RPM || die "dnf failed"
elif command -v yum >/dev/null; then $SUDO yum install -y -q $RPM || die "yum failed"
else die "no apt/dnf/yum"; fi
echo "python: $(python3 -V)"

say "2) Clone / update ($BRANCH)"
if [ -d "$DIR/.git" ]; then
  cur=$(git -C "$DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)
  [ "$cur" != "$BRANCH" ] && { echo "on '$cur', backing up and re-cloning '$BRANCH'"; mv "$DIR" "$DIR.bak.$(date +%s)"; }
fi
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q --ff-only || true
else git clone -q --depth 1 -b "$BRANCH" "$URL" "$DIR" || die "clone failed (private repo? rerun with PAT=...)"; fi
cd "$DIR" || die "cd failed"
echo "commit: $(git log -1 --pretty='%h %s')"

say "3) Virtualenv + python deps"
python3 -m venv .venv || die "venv failed"
# shellcheck disable=SC1091
. .venv/bin/activate
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
python -m pip install -q -U pip setuptools wheel
pip install -q -r requirements.txt || die "pip install -r requirements.txt failed"
python -c "import telethon, playwright; print('deps ok')" || die "telethon/playwright import failed"

if [ "${SKIP_CHROMIUM:-0}" != "1" ]; then
  say "4) Chromium for Playwright (~450MB)"
  python -m playwright install --with-deps chromium || echo "[!] chromium install failed - bridge engine unavailable"
else
  echo "skipping Chromium (SKIP_CHROMIUM=1)"
fi

say "5) .env"
[ -f .env ] || cp .env.example .env
echo "path: $DIR/.env"

if [ "${SKIP_TESTS:-0}" != "1" ]; then
  say "6) Offline tests (3 rounds)"
  SUITES=$(find bot/tests direct/tests -name 'test_*.py' | sed 's|\.py$||; s|/|.|g' | sort)
  fail=0
  for r in 1 2 3; do
    echo "-- round $r --"
    for s in $SUITES; do
      printf '  %-32s ' "$s"
      out=$(python -m "$s" 2>&1) && echo "PASS" || { echo "FAIL"; fail=$((fail+1)); echo "$out"|tail -12|sed 's/^/    | /'; }
    done
  done
  [ $fail -eq 0 ] && echo "ALL CLEAN" || echo "FAILURES: $fail"
fi

say "7) Run"
have_creds=1
for k in API_ID API_HASH BOT_TOKEN OWNER_ID; do
  grep -qE "^$k=.+" .env || have_creds=0
done
if [ "$have_creds" = "1" ]; then
  echo "credentials present -> installing systemd service"
  $SUDO bash deploy/install_service.sh || die "service install failed"
  echo "follow logs: journalctl -u mkwlsoso-bot -f"
else
  cat <<EOF

\033[1;33mNEXT STEP:\033[0m fill credentials, then the bot starts.
  nano $DIR/.env      # set API_ID, API_HASH, BOT_TOKEN, OWNER_ID
then either:
  $SUDO bash $DIR/deploy/install_service.sh          # always-on (recommended)
or run once in the foreground:
  cd $DIR && DISPLAY=:99 .venv/bin/python run_bot.py
EOF
fi
say "DONE"
