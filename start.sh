#!/usr/bin/env bash
# Run the bot (and the local Bot API server, if LOCAL_BOT_API_URL points at this machine) in the foreground.
# Logs go to bot.log. Ctrl+C stops everything. Same launcher as the settings app and start-at-login.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python run_bot.py
