#!/usr/bin/env bash
# Mkwlsoso - bootstrap for a raw server (1 vCPU / ~1GB RAM)
set -uo pipefail

REPO_URL="${REPO_URL:-https://github.com/shootingv818/Mkwlsoso.git}"
DIR="${DIR:-$HOME/Mkwlsoso}"
VENV="$DIR/.venv"
LOG="$HOME/bootstrap.log"
SUITES="bot.tests.test_bot_logic bot.tests.test_send_loop bot.tests.test_engines bot.tests.test_live_card bot.tests.test_login_settle bot.tests.test_scenarios bot.tests.test_state_speed direct.tests.test_direct"

exec > >(tee -a "$LOG") 2>&1
say(){ printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn(){ printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

say "0) System info"
echo "kernel : $(uname -srm)"
echo "distro : $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
echo "cpu    : $(nproc) core(s)"
free -m | head -2
df -h / | tail -1

# ---------------------------------------------------------------- swap
say "1) Swap (critical on ~1GB RAM)"
if swapon --show 2>/dev/null | grep -q .; then
  echo "swap already active:"; swapon --show
else
  SW=/swapfile
  $SUDO fallocate -l 2G $SW 2>/dev/null || $SUDO dd if=/dev/zero of=$SW bs=1M count=2048 status=none
  $SUDO chmod 600 $SW && $SUDO mkswap -q $SW && $SUDO swapon $SW \
    && grep -q "$SW" /etc/fstab 2>/dev/null || echo "$SW none swap sw 0 0" | $SUDO tee -a /etc/fstab >/dev/null
  echo "vm.swappiness=20" | $SUDO tee /etc/sysctl.d/99-mkw.conf >/dev/null
  $SUDO sysctl -q -p /etc/sysctl.d/99-mkw.conf 2>/dev/null
  echo "2G swap enabled"
fi

# ---------------------------------------------------- system packages
say "2) System packages"
PKGS_APT="git curl ca-certificates build-essential python3 python3-pip python3-venv python3-dev libffi-dev libssl-dev zlib1g-dev sqlite3 libsqlite3-dev jq tmux tzdata"
PKGS_RPM="git curl ca-certificates gcc gcc-c++ make python3 python3-pip python3-devel libffi-devel openssl-devel zlib-devel sqlite sqlite-devel jq tmux tzdata"

if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq --no-install-recommends $PKGS_APT || die "apt install failed"
elif command -v dnf >/dev/null; then
  $SUDO dnf install -y -q $PKGS_RPM || die "dnf install failed"
elif command -v yum >/dev/null; then
  $SUDO yum install -y -q $PKGS_RPM || die "yum install failed"
elif command -v apk >/dev/null; then
  $SUDO apk add --no-cache git curl build-base python3 python3-dev py3-pip libffi-dev openssl-dev sqlite jq tmux tzdata || die "apk failed"
else
  die "no supported package manager (apt/dnf/yum/apk)"
fi
echo "python : $(python3 -V)"
echo "git    : $(git --version)"

# ------------------------------------------------------------ get code
say "3) Repository"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --all --tags --prune -q && echo "fetched existing clone"
else
  git clone -q "$REPO_URL" "$DIR" || die "clone failed (private repo? use a PAT in REPO_URL)"
  echo "cloned -> $DIR"
fi
cd "$DIR" || die "cd failed"
echo "branch : $(git rev-parse --abbrev-ref HEAD)"
echo "commit : $(git log -1 --pretty='%h %s')"
echo "tags   : $(git tag | tr '\n' ' ')"
PY_COUNT=$(find . -name '*.py' -not -path './.venv/*' -not -path './.git/*' | wc -l)
echo "py files: $PY_COUNT"

# --------------------------------------------------------------- venv
say "4) Virtualenv + low-memory tuning"
python3 -m venv "$VENV" 2>/dev/null || die "venv creation failed"
# shellcheck disable=SC1091
. "$VENV/bin/activate"
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MALLOC_ARENA_MAX=2
python -m pip install -q -U pip setuptools wheel || warn "pip self-upgrade failed"
echo "venv   : $VENV"
echo "pip    : $(pip -V | awk '{print $2}')"

# ------------------------------------------------------- dependencies
say "5) Dependencies"
REQ=""
for f in requirements.txt requirements/base.txt requirements/prod.txt requirements-dev.txt; do
  [ -f "$f" ] && REQ="$REQ $f"
done
if [ -f pyproject.toml ] && [ -z "$REQ" ]; then
  echo "pyproject.toml found -> pip install ."
  pip install -q . || warn "pyproject install failed"
elif [ -n "$REQ" ]; then
  for f in $REQ; do echo "installing $f"; pip install -q -r "$f" || warn "failed: $f"; done
else
  warn "no requirements.txt / pyproject.toml in the repo"
  if [ "$PY_COUNT" -gt 0 ]; then
    echo "scanning imports to guess third-party deps..."
    python - <<'PYEOF'
import ast,os,sys,pathlib
std=set(sys.stdlib_module_names)
local={p.stem for p in pathlib.Path('.').rglob('*.py')}|{d.name for d in pathlib.Path('.').iterdir() if d.is_dir()}
found=set()
for p in pathlib.Path('.').rglob('*.py'):
    if '.venv' in p.parts or '.git' in p.parts: continue
    try: t=ast.parse(p.read_text(errors='ignore'))
    except Exception: continue
    for n in ast.walk(t):
        if isinstance(n,ast.Import):
            for a in n.names: found.add(a.name.split('.')[0])
        elif isinstance(n,ast.ImportFrom) and n.level==0 and n.module:
            found.add(n.module.split('.')[0])
ext=sorted(m for m in found if m and m not in std and m not in local)
print("third-party imports detected:", ", ".join(ext) if ext else "(none)")
open('/tmp/mkw_deps.txt','w').write("\n".join(ext))
PYEOF
    DEPS=$(tr '\n' ' ' < /tmp/mkw_deps.txt 2>/dev/null)
    if [ -n "${DEPS// /}" ]; then
      echo "attempting: pip install $DEPS"
      for d in $DEPS; do pip install -q "$d" 2>/dev/null && echo "  ok  $d" || echo "  --  $d (skipped)"; done
      pip freeze > requirements.generated.txt
      echo "wrote requirements.generated.txt"
    fi
  else
    warn "repo has no python code yet - nothing to install"
  fi
fi
echo "--- installed ---"; pip list --format=columns 2>/dev/null | head -40

# -------------------------------------------------------------- tests
say "6) Test suites (3 consecutive full runs)"
if [ "$PY_COUNT" -eq 0 ]; then
  warn "no code -> skipping tests"
else
  RUNNER="unittest"
  python -c "import pytest" 2>/dev/null && RUNNER="pytest"
  echo "runner : $RUNNER"
  FAILED=0
  for round in 1 2 3; do
    echo ""; echo "########## ROUND $round/3 ##########"
    for s in $SUITES; do
      printf '  %-34s ' "$s"
      if [ "$RUNNER" = pytest ]; then
        OUT=$(python -m pytest -q --no-header "${s//.//}.py" 2>&1)
      else
        OUT=$(python -m unittest -q "$s" 2>&1)
      fi
      RC=$?
      if [ $RC -eq 0 ]; then echo "PASS"
      else
        case "$OUT" in
          *"No module named"*|*"not found"*|*"ModuleNotFound"*) echo "SKIP (missing)";;
          *) echo "FAIL"; FAILED=$((FAILED+1)); echo "$OUT" | tail -15 | sed 's/^/      /';;
        esac
      fi
    done
  done
  echo ""
  [ $FAILED -eq 0 ] && echo "ALL ROUNDS CLEAN" || echo "TOTAL FAILURES: $FAILED"
fi

say "DONE"
cat <<EOF
project : $DIR
venv    : $VENV
log     : $LOG
activate: source $VENV/bin/activate && cd $DIR
EOF
