"""Import bot.py with a fake, isolated configuration instead of the real .env."""

import os
import sys
from pathlib import Path

# load_dotenv() never overrides variables that are already set, so these win over the real .env.
os.environ.update(
    BOT_TOKEN="123456:TEST-TOKEN",
    TARGET_CHAT_ID="",
    ALLOWED_USER_IDS="111,222",
    LOCAL_BOT_API_URL="",
    TELEGRAM_API_ID="",
    TELEGRAM_API_HASH="",
    MTPROTO_UPLOAD="0",
    MAX_SIZE_MB="1",
    PROXY="",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
