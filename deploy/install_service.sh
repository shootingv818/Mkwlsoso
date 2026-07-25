#!/usr/bin/env bash
# One-shot installer for an always-on Mkwlsoso deployment (as root):
#
#   bash deploy/install_service.sh
#
# Sets up systemd services so everything survives SSH logout, crashes, and
# reboots:
#   - xvfb.service    : the virtual display :99 the headed browser draws on (essential)
#   - x11vnc.service  : exposes :99 over VNC on 5900        (only if x11vnc is installed)
#   - novnc.service   : web noVNC proxy 6080 -> 5900        (only if novnc_proxy is found)
#   - mkwlsoso-bot.service : the Telegram bot (depends on xvfb)
#
# Already-running manual instances are left alone; the services are enabled for
# boot and take over cleanly after the next reboot.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$DIR/.venv/bin/python"
DISPLAY_NUM=99
RES="1280x800x24"

if [ ! -x "$PY" ]; then
  echo "ERROR: venv python not found at $PY"
  echo "Create it first:  cd '$DIR' && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

echo "Install dir : $DIR"
echo "Python      : $PY"
echo "Display     : :$DISPLAY_NUM"
echo

XVFB_BIN="$(command -v Xvfb || true)"
X11VNC_BIN="$(command -v x11vnc || true)"
NOVNC_BIN="$(command -v novnc_proxy || true)"

# ---- xvfb.service (essential) ------------------------------------------
if [ -z "$XVFB_BIN" ]; then
  echo "WARNING: Xvfb not found. Install it (apt-get install -y xvfb) so the bot has a display."
else
  cat > /etc/systemd/system/xvfb.service <<EOF
[Unit]
Description=Xvfb virtual display :$DISPLAY_NUM (Mkwlsoso)
After=network.target

[Service]
Type=simple
ExecStart=$XVFB_BIN :$DISPLAY_NUM -screen 0 $RES -nolisten tcp
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  echo "wrote xvfb.service"
fi

# ---- x11vnc.service (optional: to SEE :99 over VNC) --------------------
if [ -n "$X11VNC_BIN" ]; then
  cat > /etc/systemd/system/x11vnc.service <<EOF
[Unit]
Description=x11vnc for display :$DISPLAY_NUM (Mkwlsoso)
After=xvfb.service
Requires=xvfb.service

[Service]
Type=simple
Environment=DISPLAY=:$DISPLAY_NUM
ExecStart=$X11VNC_BIN -display :$DISPLAY_NUM -forever -shared -rfbport 5900 -nopw
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  echo "wrote x11vnc.service"
fi

# ---- novnc.service (optional: web viewer on 6080) ----------------------
if [ -n "$NOVNC_BIN" ]; then
  cat > /etc/systemd/system/novnc.service <<EOF
[Unit]
Description=noVNC web proxy 6080 -> 5900 (Mkwlsoso)
After=x11vnc.service
Requires=x11vnc.service

[Service]
Type=simple
ExecStart=$NOVNC_BIN --vnc localhost:5900 --listen 6080
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  echo "wrote novnc.service"
fi

# ---- mkwlsoso-bot.service (depends on the display) ---------------------
cat > /etc/systemd/system/mkwlsoso-bot.service <<EOF
[Unit]
Description=Mkwlsoso Eitaa Telegram control bot
After=network-online.target xvfb.service
Wants=network-online.target xvfb.service

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=DISPLAY=:$DISPLAY_NUM
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY $DIR/run_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
echo "wrote mkwlsoso-bot.service"

systemctl daemon-reload

# Enable for boot; start now only if the resource isn't already in use so we
# never collide with a manual instance you already have running.
xvfb_running() { [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; }
port_used() { ss -ltn 2>/dev/null | grep -q ":$1 "; }

start_safe() {
  local unit="$1"; local busy_msg="$2"; shift 2
  systemctl enable "$unit" >/dev/null 2>&1 || true
  if "$@"; then
    echo "  $unit: $busy_msg already active -> enabled for boot (takes over after reboot)"
  else
    systemctl start "$unit" && echo "  $unit: started + enabled"
  fi
}

echo
echo "Enabling + starting:"
[ -n "$XVFB_BIN" ]  && start_safe xvfb.service   "display :$DISPLAY_NUM" xvfb_running
[ -n "$X11VNC_BIN" ] && start_safe x11vnc.service "port 5900"           port_used 5900
[ -n "$NOVNC_BIN" ]  && start_safe novnc.service  "port 6080"           port_used 6080
# The bot is safe to (re)start now regardless.
systemctl enable mkwlsoso-bot.service >/dev/null 2>&1 || true
systemctl restart mkwlsoso-bot.service && echo "  mkwlsoso-bot.service: started + enabled"

echo
sleep 2
systemctl --no-pager --full status mkwlsoso-bot.service || true
echo
echo "Done. Follow the bot logs with:  journalctl -u mkwlsoso-bot -f"
[ -z "$X11VNC_BIN" ] && echo "Note: x11vnc not installed -> only the headless display is a service (the bot works; the web noVNC view isn't managed)."
