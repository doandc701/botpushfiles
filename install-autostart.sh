#!/usr/bin/env bash
# Install a systemd user service so the bot starts with the computer and restarts if it crashes
# (but not when it exits because another machine took the bot over).
# Remove with: systemctl --user disable --now botpushfiles && rm ~/.config/systemd/user/botpushfiles.service
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

pkill -f "$DIR/start.sh" 2>/dev/null || true
pkill -f "python bot.py" 2>/dev/null || true
pkill -f "telegram-bot-api/bin/telegram-bot-api" 2>/dev/null || true

mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/botpushfiles.service <<EOF
[Unit]
Description=Telegram bot botpushfiles (local Bot API server + bot)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$DIR/.venv/bin/python $DIR/run_bot.py
WorkingDirectory=$DIR
Environment=PYTHONUNBUFFERED=1
# on-failure: when another machine takes the bot over, bot.py exits 0 and should stay stopped until next boot.
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now botpushfiles.service
# Start at boot even before anyone logs in.
loginctl enable-linger "$USER" || echo "Không bật được linger: bot sẽ tự chạy sau khi bạn đăng nhập vào máy."

sleep 8
systemctl --user status botpushfiles.service --no-pager | head -12
