#!/usr/bin/env bash
# Install the Mkwlsoso bot as an always-on systemd service.
#
# Run once (as root):  bash deploy/install_service.sh
# It derives the real install path + venv automatically, writes the unit,
# enables it (start on boot), and starts it now.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$DIR/.venv/bin/python"
UNIT=/etc/systemd/system/mkwlsoso-bot.service
DISPLAY_ENV="${DISPLAY:-:99}"

if [ ! -x "$PY" ]; then
  echo "ERROR: venv python not found at $PY"
  echo "Create it first:  cd '$DIR' && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

echo "Install dir : $DIR"
echo "Python      : $PY"
echo "DISPLAY     : $DISPLAY_ENV"

cat > "$UNIT" <<EOF
[Unit]
Description=Mkwlsoso Eitaa Telegram control bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=DISPLAY=$DISPLAY_ENV
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $DIR/run_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mkwlsoso-bot
sleep 2
systemctl --no-pager --full status mkwlsoso-bot || true
echo
echo "Done. Follow logs with:  journalctl -u mkwlsoso-bot -f"
