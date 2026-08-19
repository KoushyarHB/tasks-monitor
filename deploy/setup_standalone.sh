#!/usr/bin/env bash
# One-shot setup: install the standalone monitor as a USER systemd service.
# No sudo required. Run from the repo root:
#   ./deploy/setup_standalone.sh "<NEW_BOT_TOKEN>" "<NEW_CHANNEL_ID>"
set -euo pipefail

TOKEN="${1:-}"
CHANNEL="${2:-}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="saba-tasks-monitor"

if [ -z "$TOKEN" ] || [ -z "$CHANNEL" ]; then
  echo "usage: $0 <NEW_BOT_TOKEN> <NEW_CHANNEL_ID>"
  echo "  NEW_BOT_TOKEN  from @BotFather (a fresh bot, NOT the Hermes one)"
  echo "  NEW_CHANNEL_ID e.g. -1001234567890 (channel the new bot is admin of)"
  exit 1
fi

echo "▶ Service: user-level systemd ($SERVICE)"
echo "▶ App dir : $APP_DIR"

# 1. venv (idempotent)
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  echo "▶ Creating venv…"
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
echo "▶ deps installed"

# 2. .env — keep Plane values, swap Telegram to the NEW bot + channel
if [ -f "$APP_DIR/.env" ]; then
  # preserve existing plane values
  cp "$APP_DIR/.env" "$APP_DIR/.env.old"
  grep -E "^(PLANE_|POLL_|STATE_)" "$APP_DIR/.env.old" > "$APP_DIR/.env" || true
fi
{
  echo "TG_BOT_TOKEN=$TOKEN"
  echo "TG_CHAT_ID=$CHANNEL"
  echo "TG_PROXY="
} >> "$APP_DIR/.env"
echo "▶ .env updated (new bot token + channel; TG_PROXY cleared for server-side deploy)"

# 3. systemd user unit
mkdir -p "$HOME/.config/systemd/user"
cp "$APP_DIR/deploy/saba-tasks-monitor.user.service" "$HOME/.config/systemd/user/$SERVICE.service"
systemctl --user daemon-reload

# 4. enable + start
systemctl --user enable "$SERVICE"
systemctl --user restart "$SERVICE"

echo ""
echo "✅ Installed. Status:"
systemctl --user status "$SERVICE" --no-pager | head -10
echo ""
echo "Logs : journalctl --user -u $SERVICE -f"
echo "Stop : systemctl --user stop $SERVICE"
