#!/usr/bin/env bash
# Install the Saba Tasks Monitor as a systemd service.
# Run as root:  sudo ./install.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/saba-tasks-monitor}"
SERVICE_NAME="saba-tasks-monitor"

echo "▶ Installing $SERVICE_NAME to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --exclude '.venv' --exclude '.git' --exclude '.env' \
  "$(dirname "$0")/../" "$APP_DIR/" || cp -r "$(dirname "$0")/../"* "$APP_DIR/"

echo "▶ Creating venv + installing deps"
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

if [ ! -f "$APP_DIR/.env" ]; then
  echo "⚠ No .env found — creating from example. FILL IN THE SECRETS!"
  cp .env.example .env
fi

echo "▶ Installing systemd unit"
cp deploy/saba-tasks-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

echo "▶ Status:"
systemctl status "$SERVICE_NAME" --no-pager | head -12
echo ""
echo "✅ Done. Logs: journalctl -u $SERVICE_NAME -f"
