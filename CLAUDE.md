# botpushfiles

Telegram bot: a user sends a link or a file, the bot uploads it to `TARGET_CHAT_ID`. All logic is in `bot.py`
(python-telegram-bot v22, polling). User-facing text is Vietnamese; code, comments and logs are English.

## Commands

```bash
.venv/bin/pip install -r requirements-dev.txt   # app + pytest, ruff
.venv/bin/ruff check .                           # lint (config in pyproject.toml)
.venv/bin/python -m pytest -q                    # tests; no network, no Telegram, no real .env
systemctl --user restart botpushfiles            # deploy a change on this machine (service runs start.sh)
tail -f bot.log                                  # or: journalctl --user -u botpushfiles -f
```

CI (`.github/workflows/ci.yml`) runs ruff + pytest on Python 3.12 and 3.14, ShellCheck on `*.sh`, and a Docker build.

## How it works

- Links: `classify_link` decides direct file / audio-only page / video page. Video pages get an inline menu
  (`FORMATS`); the URL is kept in `bot_data["pending"]` under a 12-hex key because callback data is limited to 64 bytes.
- Downloads: httpx for direct files, yt-dlp for pages (with `curl_cffi` impersonation; TikTok returns 403 without it).
  Audio is copied out with ffmpeg without re-encoding (`extract_audio`).
- Links come from Telegram entities (`extract_urls`), so `www.x.com` without a scheme works.
- NhacCuaTui (`NCT_RE`, no yt-dlp extractor): `nct_song` reads the page's `__NUXT_DATA__` payload and
  `download_nct` fetches the free 320 kbps MP3 (else 128). FLAC exists but Telegram would send it as a document.
- Uploads: MTProto via Telethon when `TELEGRAM_API_ID/HASH` are set (real progress, 2000 MB), else the Bot API
  (local server at `LOCAL_BOT_API_URL` for 2000 MB, otherwise 50 MB). Files sent to the bot are `copy_message`d.
- Takeover: only one machine may poll a token. On `Conflict` the bot exits with code 0 unless it started less than
  `TAKEOVER_GRACE` seconds ago, so the newest machine keeps the bot. This relies on `Restart=on-failure` (systemd)
  and `restart: on-failure` (Docker); `Restart=always` would make machines fight.

## Settings app

- `settings_app.py` (Flet 1.x UI) → `botctl.py` (OS-specific logic, no UI, never imports `bot`) → `run_bot.py`
  (launcher: starts the local Bot API server if configured, runs `bot.py`, restarts on non-zero exit, stops on 0,
  writes `bot.log` and `bot.pid`). systemd, LaunchAgent, Task Scheduler and `start.sh` all run the launcher.
- Data folder (`botctl.app_home`, passed to the bot as `BOTPUSHFILES_HOME`): the project folder from source, a per-user
  config folder in the packaged app. The packaged app runs itself with `--launcher` / `--bot`.
- Tray icon: `tray_app.py` (PySide6, its own process: `settings_app.py --tray`), so it outlives the window. It works
  on GNOME/Wayland through StatusNotifierItem. Single instance via `tray.pid` / `ui.pid` (`botctl.alive_pid`).
  Start-at-login adds the tray too (XDG autostart, LaunchAgent, Task Scheduler). `make_icon.py` draws `assets/`.
- `python build_app.py` packages the current OS (PyInstaller via `flet pack`); `.github/workflows/build-app.yml`
  builds Windows, macOS and Linux.

## Rules

- Never commit or print `.env`, `*.session` or `dist/` (they hold the bot token / login session).
- Tests import `bot` with fake settings from `tests/conftest.py`; keep new module-level config overridable there.
- The company network blocks YouTube/TikTok; a download failing with ConnectError here is not a code bug.
