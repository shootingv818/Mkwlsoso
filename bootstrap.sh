#!/usr/bin/env bash
# Mkwlsoso - bootstrap + offline test run for a raw server (1 vCPU / ~1GB RAM)
#
#   BRANCH=feat/hybrid-engine-live   which branch to test (default)
#   INSTALL_BROWSER=1               also download Chromium (~450MB, only needed
#                                   for the 'bridge' engine / real runs)
#   ROUNDS=3                        consecutive full test rounds
set -uo pipefail

OWNER=shootingv818
NAME=Mkwlsoso
BRANCH="${BRANCH:-test/server-run}"
PAT="${PAT:-}"
DIR="${DIR:-$HOME/$NAME}"
VENV="$DIR/.venv"
LOG="$HOME/bootstrap.log"
ROUNDS="${ROUNDS:-3}"
INSTALL_BROWSER="${INSTALL_BROWSER:-0}"

if [ -n "$PAT" ]; then REPO_URL="https://$PAT@github.com/$OWNER/$NAME.git"
else REPO_URL="${REPO_URL:-https://github.com/$OWNER/$NAME.git}"; fi

exec > >(tee -a "$LOG") 2>&1
say(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn(){ printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

say "0) System"
echo "kernel : $(uname -srm)"
echo "distro : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "cpu    : $(nproc) core(s)"
free -m | head -2; df -h / | tail -1
echo "steal  : $(top -bn1 | awk '/Cpu\(s\)/{print $(NF-1)" "$NF}')"

# -------------------------------------------------------------------- swap
say "1) Swap (mandatory on ~1GB RAM)"
if swapon --show 2>/dev/null | grep -q .; then swapon --show
else
  SW=/swapfile
  $SUDO fallocate -l 2G $SW 2>/dev/null || $SUDO dd if=/dev/zero of=$SW bs=1M count=2048 status=none
  $SUDO chmod 600 $SW && $SUDO mkswap -q $SW && $SUDO swapon $SW
  grep -q "$SW" /etc/fstab 2>/dev/null || echo "$SW none swap sw 0 0" | $SUDO tee -a /etc/fstab >/dev/null
  echo "vm.swappiness=20" | $SUDO tee /etc/sysctl.d/99-mkw.conf >/dev/null
  $SUDO sysctl -q -p /etc/sysctl.d/99-mkw.conf 2>/dev/null
  echo "2G swap enabled"
fi

# ---------------------------------------------------------------- packages
say "2) System packages"
A="git curl ca-certificates build-essential python3 python3-pip python3-venv python3-dev libffi-dev libssl-dev zlib1g-dev sqlite3 jq tmux tzdata xvfb"
R="git curl ca-certificates gcc gcc-c++ make python3 python3-pip python3-devel libffi-devel openssl-devel zlib-devel sqlite jq tmux tzdata xorg-x11-server-Xvfb"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq --no-install-recommends $A || die "apt failed"
elif command -v dnf >/dev/null; then $SUDO dnf install -y -q $R || die "dnf failed"
elif command -v yum >/dev/null; then $SUDO yum install -y -q $R || die "yum failed"
else die "unsupported package manager"; fi
echo "python : $(python3 -V)"

# -------------------------------------------------------------------- code
say "3) Repository (branch: $BRANCH)"
if [ -d "$DIR/.git" ]; then
  CUR=$(git -C "$DIR" rev-parse --abbrev-ref HEAD)
  if [ "$CUR" != "$BRANCH" ]; then
    warn "existing clone is on '$CUR', re-cloning '$BRANCH'"
    mv "$DIR" "$DIR.bak.$(date +%s)"
  fi
fi
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull -q --ff-only && echo "updated existing clone"
else
  git clone -q --depth 1 -b "$BRANCH" "$REPO_URL" "$DIR" \
    || die "clone failed. private repo? rerun with: PAT=your_token"
  echo "cloned -> $DIR"
fi
cd "$DIR" || die "cd failed"
echo "branch : $(git rev-parse --abbrev-ref HEAD)"
echo "commit : $(git log -1 --pretty='%h %s')"
PY=$(find . -name '*.py' -not -path './.venv/*' -not -path './.git/*' | wc -l)
echo "py file: $PY"
[ "$PY" -eq 0 ] && die "branch '$BRANCH' contains no python code"

# -------------------------------------------------------------------- venv
say "4) Virtualenv + low-memory tuning"
python3 -m venv "$VENV" 2>/dev/null || die "venv failed"
# shellcheck disable=SC1091
. "$VENV/bin/activate"
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2
python -m pip install -q -U pip setuptools wheel || warn "pip upgrade failed"

say "5) Dependencies"
[ -f requirements.txt ] || die "requirements.txt missing"
echo "--- requirements.txt ---"; grep -v '^\s*#' requirements.txt | grep -v '^\s*$'
pip install -q -r requirements.txt || die "pip install -r requirements.txt failed"
python - <<'P'
import importlib
for m in ("playwright","telethon"):
    try:
        importlib.import_module(m); print(f"  ok  import {m}")
    except Exception as e:
        print(f"  --  import {m}: {e}")
P
if [ "$INSTALL_BROWSER" = "1" ]; then
  say "5b) Chromium for Playwright (~450MB)"
  python -m playwright install --with-deps chromium || warn "chromium install failed"
else
  echo "skipping Chromium (INSTALL_BROWSER=1 to add it; needed for 'bridge' engine)"
fi
[ -f .env ] || { cp .env.example .env && echo ".env created from .env.example"; }

# ------------------------------------------------------------------- tests
say "6) Offline test suites ($ROUNDS consecutive rounds)"
SUITES=$(find bot/tests direct/tests -name 'test_*.py' 2>/dev/null \
         | sed 's|\.py$||; s|/|.|g' | sort)
echo "discovered:"; for s in $SUITES; do echo "  $s"; done
FAILED=0; SUMMARY=""
for r in $(seq 1 "$ROUNDS"); do
  echo ""; echo "########## ROUND $r/$ROUNDS ##########"
  for s in $SUITES; do
    printf '  %-32s ' "$s"
    T0=$(date +%s)
    OUT=$(python -m "$s" 2>&1); RC=$?
    T=$(( $(date +%s) - T0 ))
    if [ $RC -eq 0 ]; then
      echo "PASS  (${T}s)  $(echo "$OUT" | grep -Eo '[0-9]+ passed[^,]*' | tail -1)"
    else
      echo "FAIL  (${T}s)"; FAILED=$((FAILED+1))
      SUMMARY="$SUMMARY\n  R$r $s"
      echo "$OUT" | tail -25 | sed 's/^/      | /'
    fi
  done
done
echo ""
if [ $FAILED -eq 0 ]; then echo "ALL $ROUNDS ROUNDS CLEAN"
else printf 'TOTAL FAILURES: %s%b\n' "$FAILED" "$SUMMARY"; fi

say "DONE"
cat <<EOF
project : $DIR   (branch $(git rev-parse --abbrev-ref HEAD))
venv    : $VENV
log     : $LOG
next    : source $VENV/bin/activate && cd $DIR
          nano .env            # API_ID / API_HASH / BOT_TOKEN
          python run_bot.py
EOF
[ $FAILED -eq 0 ] || exit 1
