"""Package the settings app (with the bot inside) for the current OS into dist/.

    .venv/bin/pip install -r requirements-app.txt
    .venv/bin/python build_app.py

Windows → dist/BotPushFiles/BotPushFiles.exe, macOS → dist/BotPushFiles.app, Linux → dist/BotPushFiles/BotPushFiles.
PyInstaller cannot cross-compile: build each OS on that OS (the GitHub "Build app" workflow does all three).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEP = ";" if os.name == "nt" else ":"  # PyInstaller's --add-data separator


def main() -> int:
    pyinstaller_args = [
        # yt-dlp loads site extractors lazily, Telethon its TL types, so PyInstaller can't see them by tracing imports.
        "--collect-submodules=yt_dlp",
        "--collect-submodules=telethon",
        "--collect-all=curl_cffi",
        "--collect-data=imageio_ffmpeg",  # The bundled ffmpeg binary
        "--noconfirm",
    ]
    cmd = [
        sys.executable, "-m", "flet.cli", "pack", "settings_app.py",
        "--name", "BotPushFiles",
        "--product-name", "Bot Push Files",
        "--bundle-id", "vn.botpushfiles.settings",
        "--onedir",
        "--yes",
        "--icon", str(ROOT / "assets" / ("icon.ico" if os.name == "nt" else "icon.icns" if sys.platform == "darwin"
                                           else "icon.png")),
        "--hidden-import", "bot", "botctl", "tray_app",
        "--add-data", f"{ROOT / '.env.example'}{SEP}.",
        "--add-data", f"{ROOT / 'assets'}{SEP}assets",
        # Options starting with "-" must be attached with "=", one per flag.
        *(f"--pyinstaller-build-args={arg}" for arg in pyinstaller_args),
    ]  # fmt: skip
    print(" ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
