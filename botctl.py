"""Cross-platform control of the bot for the settings app: .env, start/stop, logs and start-at-login.

Nothing here imports bot.py, so the settings app works before a token is configured.
"""

import os
import platform
import plistlib
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
from dotenv import dotenv_values

APP_NAME = "botpushfiles"
SERVICE = f"{APP_NAME}.service"  # systemd user unit (Linux)
LAUNCHD_LABEL = f"vn.{APP_NAME}"  # LaunchAgent (macOS)
TASK_NAME = APP_NAME  # Task Scheduler (Windows)
SOURCE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = SOURCE_DIR / "assets"
TRAY_LAUNCHD_LABEL = f"{LAUNCHD_LABEL}.tray"
TRAY_TASK_NAME = f"{APP_NAME}-tray"
FROZEN = getattr(sys, "frozen", False)  # Running from the packaged app (PyInstaller)
SYSTEM = platform.system()  # "Linux", "Darwin" or "Windows"
LOG_MAX_BYTES = 5 * 1024 * 1024
RESTART_DELAY = 10


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    help: str
    group: str
    secret: bool = False
    switch: bool = False  # Stored as "1" / "0"
    default: str = ""


FIELDS = [
    Field("BOT_TOKEN", "Bot token", "Lấy từ @BotFather.", "Cơ bản", secret=True),
    Field("TARGET_CHAT_ID", "Channel/group đích", "Ví dụ @ten_channel hoặc -100123... Bot phải là admin.", "Cơ bản"),
    Field("ALLOWED_USER_IDS", "User ID được dùng bot", "Cách nhau bởi dấu phẩy. Gõ /start với bot để xem ID.",
          "Cơ bản"),
    Field("TELEGRAM_API_ID", "API ID", "Lấy tại my.telegram.org → API development tools.", "File lớn (2000 MB)"),
    Field("TELEGRAM_API_HASH", "API hash", "Đi cùng API ID.", "File lớn (2000 MB)", secret=True),
    Field("MTPROTO_UPLOAD", "Upload qua MTProto", "Có % tiến trình thật, file tới 2000 MB.", "File lớn (2000 MB)",
          switch=True, default="1"),
    Field("MAX_SIZE_MB", "Giới hạn mỗi file (MB)", "Để trống = tự chọn (2000 hoặc 50).", "File lớn (2000 MB)"),
    Field("LOCAL_BOT_API_URL", "Local Bot API server", "Ví dụ http://127.0.0.1:8081. Để trống nếu không dùng.",
          "Nâng cao"),
    Field("PROXY", "Proxy tải file", "Ví dụ socks5://host:1080, khi mạng chặn TikTok/YouTube.", "Nâng cao"),
]  # fmt: skip


# --- Paths --------------------------------------------------------------------------------------------------------


def app_home() -> Path:
    """Where .env, logs, downloads and the MTProto session live."""
    if os.getenv("BOTPUSHFILES_HOME"):
        return Path(os.environ["BOTPUSHFILES_HOME"])
    if not FROZEN:
        return SOURCE_DIR  # Source checkout: keep using the project folder.
    if SYSTEM == "Windows":
        return Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    if SYSTEM == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME


def env_path(home: Path) -> Path:
    return home / ".env"


def log_path(home: Path) -> Path:
    return home / "bot.log"


def pid_path(home: Path) -> Path:
    return home / "bot.pid"


def home_tray_pid_path(home: Path) -> Path:
    return home / "tray.pid"


def ui_pid_path(home: Path) -> Path:
    return home / "ui.pid"


# --- .env ---------------------------------------------------------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    values = {f.key: f.default for f in FIELDS}
    if path.exists():
        values.update({k: v or "" for k, v in dotenv_values(path).items()})
    return values


def quote(value: str) -> str:
    if value == "" or re.fullmatch(r"[\w@:/.,+\-=?&%~]*", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env(path: Path, values: dict[str, str], template: Path | None = None) -> None:
    """Set keys in .env, keeping its comments and order; a new .env starts from template (.env.example)."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif template and template.exists():
        lines = template.read_text(encoding="utf-8").splitlines()
    else:
        lines = []
    pending = dict(values)
    for i, line in enumerate(lines):
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            lines[i] = f"{key}={quote(pending.pop(key).strip())}"
    lines += [f"{key}={quote(value.strip())}" for key, value in pending.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if SYSTEM != "Windows":
        path.chmod(0o600)  # It holds the bot token.


def validate(values: dict[str, str]) -> list[str]:
    """Problems that would stop the bot from starting, in Vietnamese."""
    errors = []
    if not re.fullmatch(r"\d+:[\w-]{30,}", values.get("BOT_TOKEN", "").strip()):
        errors.append("Bot token không đúng dạng 123456:ABC... (lấy từ @BotFather).")
    ids = values.get("ALLOWED_USER_IDS", "").replace(" ", "")
    if ids and not re.fullmatch(r"\d+(,\d+)*,?", ids):
        errors.append("User ID chỉ gồm số, cách nhau bởi dấu phẩy.")
    if values.get("TELEGRAM_API_ID", "").strip() and not values["TELEGRAM_API_ID"].strip().isdigit():
        errors.append("API ID chỉ gồm số.")
    if bool(values.get("TELEGRAM_API_ID", "").strip()) != bool(values.get("TELEGRAM_API_HASH", "").strip()):
        errors.append("Cần điền cả API ID lẫn API hash (hoặc để trống cả hai).")
    size = values.get("MAX_SIZE_MB", "").strip()
    if size and not size.isdigit():
        errors.append("Giới hạn file phải là số MB.")
    for key in ("LOCAL_BOT_API_URL", "PROXY"):
        url = values.get(key, "").strip()
        if url and not re.match(r"(https?|socks5h?)://", url):
            errors.append(f"{key} phải bắt đầu bằng http://, https:// hoặc socks5://.")
    return errors


def check_telegram(values: dict[str, str], timeout: float = 15) -> list[tuple[bool, str]]:
    """Ask Telegram whether the token works and the bot can post to the target chat."""
    base = values.get("LOCAL_BOT_API_URL", "").strip().rstrip("/") or "https://api.telegram.org"
    api = f"{base}/bot{values.get('BOT_TOKEN', '').strip()}"
    results = []
    try:
        with httpx.Client(timeout=timeout, proxy=None) as client:
            me = client.get(f"{api}/getMe").json()
            if not me.get("ok"):
                return [(False, f"Token bị từ chối: {me.get('description')}")]
            bot = me["result"]
            results.append((True, f"Token đúng: @{bot['username']}"))
            target = values.get("TARGET_CHAT_ID", "").strip()
            if not target:
                results.append((True, "Không đặt channel đích: bot gửi lại vào chính chat."))
                return results
            member = client.get(f"{api}/getChatMember", params={"chat_id": target, "user_id": bot["id"]}).json()
            if not member.get("ok"):
                results.append((False, f"Không mở được {target}: {member.get('description')}"))
            elif member["result"]["status"] not in ("administrator", "creator"):
                results.append((False, f"Bot chưa là admin của {target}."))
            elif member["result"].get("can_post_messages") is False:
                results.append((False, f"Bot là admin của {target} nhưng chưa có quyền đăng bài."))
            else:
                results.append((True, f"Bot là admin của {target}."))
    except httpx.HTTPError as e:
        where = "Local Bot API server (chạy bot trước rồi kiểm tra lại)" if "api.telegram.org" not in base else base
        results.append((False, f"Không kết nối được {where}: {type(e).__name__}"))
    return results


# --- Commands -----------------------------------------------------------------------------------------------------


def launcher_cmd() -> list[str]:
    """Command that runs the launcher (run_bot.py) in the background, without a console window on Windows."""
    if FROZEN:
        return [sys.executable, "--launcher"]
    return [gui_python(), str(SOURCE_DIR / "run_bot.py")]


def gui_python() -> str:
    """Python for windowed processes: pythonw on Windows so no console window flashes up."""
    python = Path(sys.executable)
    if SYSTEM == "Windows" and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")
    return str(python)


def ui_cmd() -> list[str]:
    return [sys.executable] if FROZEN else [gui_python(), str(SOURCE_DIR / "settings_app.py")]


def tray_cmd() -> list[str]:
    return [*ui_cmd(), "--tray"]


def icon_path(suffix: str = ".png") -> Path:
    return ASSETS_DIR / f"icon{suffix}"


def bot_cmd() -> list[str]:
    if FROZEN:
        return [sys.executable, "--bot"]
    return [sys.executable, str(SOURCE_DIR / "bot.py")]


def child_env(home: Path) -> dict[str, str]:
    return {**os.environ, "BOTPUSHFILES_HOME": str(home), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}


def no_window_flags() -> int:
    if SYSTEM != "Windows":
        return 0
    return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=no_window_flags())


# --- Running state ------------------------------------------------------------------------------------------------


def alive_pid(pidfile: Path, matches) -> int | None:
    """PID stored in pidfile if that process still runs and its command line passes matches(), else None."""
    try:
        pid = int(pidfile.read_text().strip())
        proc = psutil.Process(pid)
        cmdline = proc.cmdline()
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
    except (OSError, ValueError, psutil.Error):
        return None
    return pid if matches(cmdline) else None  # Otherwise the PID was reused by an unrelated process.


def running_pid(home: Path) -> int | None:
    """PID of the launcher that is running for this home folder, or None."""
    return alive_pid(pid_path(home), lambda cmd: "--launcher" in cmd or any("run_bot" in part for part in cmd))


def tray_pid(home: Path) -> int | None:
    return alive_pid(home_tray_pid_path(home), lambda cmd: "--tray" in cmd)


def ui_pid(home: Path) -> int | None:
    """PID of an open settings window (not the tray, launcher or bot started from the same program)."""
    flags = {"--tray", "--launcher", "--bot"}
    return alive_pid(ui_pid_path(home), lambda cmd: not flags & set(cmd))


def claim(pidfile: Path) -> None:
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))


def release(pidfile: Path) -> None:
    try:
        if pidfile.read_text().strip() == str(os.getpid()):
            pidfile.unlink()
    except OSError:
        pass


def spawn_detached(cmd: list[str], home: Path) -> None:
    """Start cmd in the background, independent of this process and without a console window."""
    home.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        cmd,
        cwd=home,
        env=child_env(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=SYSTEM != "Windows",
        creationflags=no_window_flags(),
    )


def open_settings(home: Path) -> bool:
    """Open the settings window unless one is already open. Returns whether a new one was started."""
    if ui_pid(home):
        return False
    spawn_detached(ui_cmd(), home)
    return True


def ensure_tray(home: Path) -> None:
    if not tray_pid(home):
        spawn_detached(tray_cmd(), home)


def systemd_manages(home: Path) -> bool:
    """Whether the systemd user service runs the bot for this home folder (and not for another one)."""
    if SYSTEM != "Linux":
        return False
    try:
        unit = systemd_unit_path().read_text(encoding="utf-8")
    except OSError:
        return False
    match = re.search(r"^WorkingDirectory=(.+)$", unit, re.M)
    return bool(match) and Path(match.group(1).strip()).resolve() == home.resolve()


def start(home: Path) -> str:
    if running_pid(home):
        return "Bot đang chạy rồi."
    if systemd_manages(home):
        result = run(["systemctl", "--user", "start", SERVICE])
        return "Đã bật bot (systemd)." if result.returncode == 0 else f"Lỗi: {result.stderr.strip()}"
    spawn_detached(launcher_cmd(), home)
    return "Đã bật bot."


def stop(home: Path, timeout: float = 15) -> str:
    pid = running_pid(home)
    if not pid:
        return "Bot không chạy."
    if systemd_manages(home):
        main_pid = run(["systemctl", "--user", "show", "--property=MainPID", "--value", SERVICE]).stdout.strip()
        if main_pid == str(pid):
            # Stop through systemd so it doesn't count as a crash and the unit's state stays right.
            run(["systemctl", "--user", "stop", SERVICE])
            return "Đã tắt bot."
    proc = psutil.Process(pid)
    children = proc.children(recursive=True)
    # POSIX: SIGTERM lets the launcher stop the bot cleanly. Windows has no SIGTERM, so end the whole tree.
    for p in [proc] if SYSTEM != "Windows" else [proc, *children]:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs([proc, *children], timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    pid_path(home).unlink(missing_ok=True)
    return "Đã tắt bot."


def tail(path: Path, max_bytes: int = 64 * 1024) -> str:
    try:
        with path.open("rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - max_bytes))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return text.split("\n", 1)[1] if len(data) == max_bytes and "\n" in text else text


# --- Start at login -----------------------------------------------------------------------------------------------


def systemd_unit_path() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / SERVICE


def systemd_unit(cmd: list[str], home: Path) -> str:
    # on-failure: when another machine takes the bot over, the launcher exits 0 and must stay stopped.
    return f"""[Unit]
Description=Telegram bot {APP_NAME}
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={shlex.join(cmd)}
WorkingDirectory={home}
Environment=BOTPUSHFILES_HOME={home}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec={RESTART_DELAY}

[Install]
WantedBy=default.target
"""


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_plist(cmd: list[str], home: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": cmd,
            "WorkingDirectory": str(home),
            "EnvironmentVariables": {"BOTPUSHFILES_HOME": str(home), "PYTHONUNBUFFERED": "1"},
            "RunAtLoad": True,
            # Restart only after a crash, not after the bot handed itself over to another machine (exit 0).
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": RESTART_DELAY,
            "StandardOutPath": str(home / "launcher.log"),
            "StandardErrorPath": str(home / "launcher.log"),
        }
    )


def windows_task_create(cmd: list[str]) -> list[str]:
    return ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED", "/TN", TASK_NAME,
            "/TR", subprocess.list2cmdline(cmd)]  # fmt: skip


def xdg_autostart_path() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / f"{APP_NAME}-tray.desktop"


def menu_entry_path() -> Path:
    data = Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data / "applications" / f"{APP_NAME}.desktop"


def desktop_entry(cmd: list[str], home: Path, tray: bool = False) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=Bot Push Files",
        "Comment=Cài đặt và bật/tắt bot Telegram",
        f"Exec={shlex.join(cmd)}",
        f"Path={home}",
        f"Icon={icon_path()}",
        "Terminal=false",
        "Categories=Network;Utility;",
    ]
    if tray:
        lines += ["NoDisplay=true", "X-GNOME-Autostart-enabled=true"]
    return "\n".join(lines) + "\n"


def install_menu_entry(home: Path) -> None:
    """Linux: put the settings app in the applications menu, so it opens like any other app."""
    if SYSTEM != "Linux":
        return
    path = menu_entry_path()
    text = desktop_entry(ui_cmd(), home)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def set_tray_autostart(enabled: bool, home: Path) -> None:
    """Show the tray icon after login. It needs the desktop session, so it is separate from the bot's service."""
    if SYSTEM == "Linux":
        path = xdg_autostart_path()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(desktop_entry(tray_cmd(), home, tray=True), encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
    elif SYSTEM == "Darwin":
        plist = launchd_plist_path().with_name(f"{TRAY_LAUNCHD_LABEL}.plist")
        if enabled:
            plist.parent.mkdir(parents=True, exist_ok=True)
            data = {"Label": TRAY_LAUNCHD_LABEL, "ProgramArguments": tray_cmd(), "WorkingDirectory": str(home),
                    "EnvironmentVariables": {"BOTPUSHFILES_HOME": str(home)}, "RunAtLoad": True,
                    "ProcessType": "Interactive"}  # fmt: skip
            plist.write_bytes(plistlib.dumps(data))
        else:
            plist.unlink(missing_ok=True)
    elif SYSTEM == "Windows":
        if enabled:
            create = windows_task_create(tray_cmd())
            create[create.index("/TN") + 1] = TRAY_TASK_NAME
            run(create)
        else:
            run(["schtasks", "/Delete", "/F", "/TN", TRAY_TASK_NAME])


def autostart_enabled(home: Path) -> bool:
    if SYSTEM == "Linux":
        return systemd_manages(home) and run(["systemctl", "--user", "is-enabled", "--quiet", SERVICE]).returncode == 0
    if SYSTEM == "Darwin":
        return launchd_plist_path().exists()
    if SYSTEM == "Windows":
        return run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    return False


def set_autostart(enabled: bool, home: Path) -> str:
    """Turn start-at-login (bot and tray icon) on or off. Returns an error message, or "" on success."""
    error = set_bot_autostart(enabled, home)
    if not error:
        set_tray_autostart(enabled, home)
    return error


def set_bot_autostart(enabled: bool, home: Path) -> str:
    cmd = launcher_cmd()
    if SYSTEM == "Linux":
        unit = systemd_unit_path()
        if enabled:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(systemd_unit(cmd, home), encoding="utf-8")
            run(["systemctl", "--user", "daemon-reload"])
            result = run(["systemctl", "--user", "enable", SERVICE])
            # Linger starts user services at boot, before anyone logs in. Not fatal if refused.
            run(["loginctl", "enable-linger", os.getenv("USER", "")])
        else:
            result = run(["systemctl", "--user", "disable", SERVICE])
        return "" if result.returncode == 0 else result.stderr.strip()
    if SYSTEM == "Darwin":
        plist = launchd_plist_path()
        if enabled:
            plist.parent.mkdir(parents=True, exist_ok=True)
            plist.write_bytes(launchd_plist(cmd, home))
            # Registering loads the agent at the next login; RunAtLoad would also start it now, which the
            # launcher's single-instance check turns into a no-op if the bot is already running.
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        else:
            run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
            plist.unlink(missing_ok=True)
        return ""
    if SYSTEM == "Windows":
        result = run(windows_task_create(cmd) if enabled else ["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
        return "" if result.returncode == 0 else (result.stderr or result.stdout).strip()
    return f"Không hỗ trợ tự chạy trên {SYSTEM}."


# --- Launcher (run_bot.py / packaged app --launcher) --------------------------------------------------------------


def rotate(path: Path) -> None:
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))


def local_server_cmd(home: Path, values: dict[str, str]) -> list[str] | None:
    """The bundled/built telegram-bot-api server command, if LOCAL_BOT_API_URL points at this machine."""
    url = values.get("LOCAL_BOT_API_URL", "").strip()
    match = re.match(r"https?://(127\.0\.0\.1|localhost):(\d+)", url)
    name = "telegram-bot-api.exe" if SYSTEM == "Windows" else "telegram-bot-api"
    exe = SOURCE_DIR / "telegram-bot-api" / "bin" / name
    if not match or not exe.exists():
        return None
    return [str(exe), "--local", f"--api-id={values.get('TELEGRAM_API_ID', '')}",
            f"--api-hash={values.get('TELEGRAM_API_HASH', '')}", f"--dir={home / 'bot-api-data'}",
            f"--http-port={match.group(2)}"]  # fmt: skip


def launcher_main(home: Path | None = None, cmd: list[str] | None = None, restart_delay: float = RESTART_DELAY) -> int:
    """Run the bot until it exits cleanly; restart it after crashes. Output goes to bot.log."""
    home = home or app_home()
    home.mkdir(parents=True, exist_ok=True)
    if running_pid(home) not in (None, os.getpid()):
        return 0  # Another launcher already runs the bot for this folder.
    pid_path(home).write_text(str(os.getpid()))
    stopping = threading.Event()
    children: list[subprocess.Popen] = []

    def on_signal(signum, frame):
        stopping.set()
        for child in children:
            if child.poll() is None:
                child.terminate()

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}

    log = log_path(home)
    rotate(log)
    echo = sys.stdout is not None and sys.stdout.isatty()

    def pump(stream, prefix: str = "") -> None:
        with log.open("a", encoding="utf-8") as f:
            for line in iter(stream.readline, ""):
                f.write(prefix + line)
                f.flush()
                if echo:
                    sys.stdout.write(prefix + line)

    def spawn(command: list[str], prefix: str = "") -> subprocess.Popen:
        proc = subprocess.Popen(
            command, cwd=home, env=child_env(home), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=no_window_flags(),
        )  # fmt: skip
        children.append(proc)
        threading.Thread(target=pump, args=(proc.stdout, prefix), daemon=True).start()
        return proc

    try:
        server_cmd = local_server_cmd(home, read_env(env_path(home)))
        if server_cmd and cmd is None:
            spawn(server_cmd, "[bot-api] ")
            time.sleep(3)
        while not stopping.is_set():
            code = spawn(cmd or bot_cmd()).wait()
            if stopping.is_set() or code == 0:
                return 0  # Stopped, or handed over to another machine.
            with log.open("a", encoding="utf-8") as f:
                f.write(f"--- bot exited with code {code}, restarting in {restart_delay:g}s ---\n")
            stopping.wait(restart_delay)
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        try:
            if pid_path(home).read_text().strip() == str(os.getpid()):
                pid_path(home).unlink()
        except OSError:
            pass
