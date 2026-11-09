#!/usr/bin/env bash
# Start the local Telegram Bot API server (built into telegram-bot-api/bin), which allows files up to 2000 MB.
set -euo pipefail
cd "$(dirname "$0")"
set -a
# shellcheck source=/dev/null
source .env
set +a
mkdir -p bot-api-data
exec telegram-bot-api/bin/telegram-bot-api \
  --local \
  --api-id="$TELEGRAM_API_ID" \
  --api-hash="$TELEGRAM_API_HASH" \
  --dir="$PWD/bot-api-data" \
  --http-port=8081
