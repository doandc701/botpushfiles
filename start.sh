#!/usr/bin/env bash
# Start the local Bot API server (if configured) and the bot together; Ctrl+C stops both.
set -euo pipefail
cd "$(dirname "$0")"

if grep -q '^LOCAL_BOT_API_URL=.\+' .env; then
  ./run_bot_api.sh >bot-api.log 2>&1 &
  trap 'kill $! 2>/dev/null' EXIT
  sleep 3
fi
.venv/bin/python bot.py
