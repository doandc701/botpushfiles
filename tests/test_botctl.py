import plistlib
import sys

import httpx
import pytest

import botctl

TOKEN = "123456:" + "A" * 35


# --- .env ---------------------------------------------------------------------------------------------------------


def test_write_env_keeps_comments_and_order(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# token\nBOT_TOKEN=old\n\n# proxy\nPROXY=\nOTHER=keep\n")
    botctl.write_env(env, {"BOT_TOKEN": TOKEN, "PROXY": "socks5://h:1", "TARGET_CHAT_ID": "@ch"})
    assert env.read_text() == (
        f"# token\nBOT_TOKEN={TOKEN}\n\n# proxy\nPROXY=socks5://h:1\nOTHER=keep\nTARGET_CHAT_ID=@ch\n"
    )
    if sys.platform != "win32":
        assert env.stat().st_mode & 0o777 == 0o600


def test_write_env_starts_from_template(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text("# Token lấy từ @BotFather\nBOT_TOKEN=123:ABC\n")
    env = tmp_path / "home" / ".env"
    botctl.write_env(env, {"BOT_TOKEN": TOKEN}, template=template)
    assert env.read_text(encoding="utf-8") == f"# Token lấy từ @BotFather\nBOT_TOKEN={TOKEN}\n"


def test_write_env_round_trips_awkward_values(tmp_path):
    env = tmp_path / ".env"
    tricky = 'a b # "c" \\ d'
    botctl.write_env(env, {"PROXY": tricky})
    assert botctl.read_env(env)["PROXY"] == tricky


def test_read_env_defaults(tmp_path):
    values = botctl.read_env(tmp_path / "missing.env")
    assert values["MTPROTO_UPLOAD"] == "1"
    assert values["BOT_TOKEN"] == ""


def valid(**overrides):
    return {"BOT_TOKEN": TOKEN, "ALLOWED_USER_IDS": "1, 2", **overrides}


def test_validate_accepts_good_settings():
    assert botctl.validate(valid(TELEGRAM_API_ID="12", TELEGRAM_API_HASH="abc", PROXY="socks5://h:1")) == []


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"BOT_TOKEN": "nope"}, "Bot token"),
        ({"ALLOWED_USER_IDS": "12,abc"}, "User ID"),
        ({"TELEGRAM_API_ID": "12"}, "API hash"),
        ({"TELEGRAM_API_ID": "x", "TELEGRAM_API_HASH": "h"}, "API ID"),
        ({"MAX_SIZE_MB": "big"}, "MB"),
        ({"PROXY": "host:1080"}, "PROXY"),
    ],
)
def test_validate_rejects(overrides, fragment):
    errors = botctl.validate(valid(**overrides))
    assert len(errors) == 1 and fragment in errors[0]


# --- Telegram check -----------------------------------------------------------------------------------------------


def fake_telegram(monkeypatch, status="administrator", ok=True, can_post=True):
    def handler(request):
        if request.url.path.endswith("/getMe"):
            if not ok:
                return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
            return httpx.Response(200, json={"ok": True, "result": {"id": 7, "username": "pushbot"}})
        assert request.url.params["user_id"] == "7"
        member = {"status": status, "can_post_messages": can_post}
        return httpx.Response(200, json={"ok": True, "result": member})

    real = httpx.Client
    monkeypatch.setattr(botctl.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))


def test_check_telegram_ok(monkeypatch):
    fake_telegram(monkeypatch)
    results = botctl.check_telegram(valid(TARGET_CHAT_ID="@ch"))
    assert results == [(True, "Token đúng: @pushbot"), (True, "Bot là admin của @ch.")]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"ok": False}, "Token bị từ chối: Unauthorized"),
        ({"status": "member"}, "Bot chưa là admin của @ch."),
        ({"can_post": False}, "Bot là admin của @ch nhưng chưa có quyền đăng bài."),
    ],
)
def test_check_telegram_problems(monkeypatch, kwargs, expected):
    fake_telegram(monkeypatch, **kwargs)
    results = botctl.check_telegram(valid(TARGET_CHAT_ID="@ch"))
    assert results[-1] == (False, expected)


# --- Start at login -----------------------------------------------------------------------------------------------


def test_systemd_unit_does_not_restart_after_takeover(tmp_path):
    unit = botctl.systemd_unit(["/py", "/app/run_bot.py"], tmp_path)
    assert "ExecStart=/py /app/run_bot.py" in unit
    assert "Restart=on-failure" in unit
    assert f"Environment=BOTPUSHFILES_HOME={tmp_path}" in unit


def test_launchd_plist(tmp_path):
    plist = plistlib.loads(botctl.launchd_plist(["/py", "/app/run_bot.py"], tmp_path))
    assert plist["ProgramArguments"] == ["/py", "/app/run_bot.py"]
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["RunAtLoad"] is True


def test_windows_task_quotes_paths():
    cmd = botctl.windows_task_create([r"C:\Program Files\Py\pythonw.exe", r"C:\bot\run_bot.py"])
    assert cmd[cmd.index("/TR") + 1] == r'"C:\Program Files\Py\pythonw.exe" C:\bot\run_bot.py'
    assert cmd[cmd.index("/SC") + 1] == "ONLOGON"


# --- Launcher -----------------------------------------------------------------------------------------------------


def script(tmp_path, body):
    path = tmp_path / "fake_bot.py"
    path.write_text(body)
    return [sys.executable, str(path)]


def test_launcher_restarts_after_crash_and_stops_on_clean_exit(tmp_path):
    counter = tmp_path / "runs"
    cmd = script(
        tmp_path,
        f"from pathlib import Path\np = Path({str(counter)!r})\nn = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\nprint('run', n + 1)\nraise SystemExit(0 if n == 1 else 3)\n",
    )
    assert botctl.launcher_main(tmp_path, cmd, restart_delay=0.1) == 0
    assert counter.read_text() == "2"  # Crashed once, restarted, then exited cleanly.
    log = botctl.log_path(tmp_path).read_text()
    assert "run 1" in log and "exited with code 3" in log and "run 2" in log
    assert not botctl.pid_path(tmp_path).exists()


def test_running_pid_ignores_stale_and_reused_pids(tmp_path):
    botctl.pid_path(tmp_path).write_text("999999999")
    assert botctl.running_pid(tmp_path) is None
    botctl.pid_path(tmp_path).write_text(str(__import__("os").getpid()))  # pytest: not a launcher
    assert botctl.running_pid(tmp_path) is None


def test_tail(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text("".join(f"line {i}\n" for i in range(1000)))
    text = botctl.tail(log, max_bytes=100)
    assert text.endswith("line 999\n")
    assert text.startswith("line ")  # A partial first line is dropped.
    assert botctl.tail(tmp_path / "missing.log") == ""


def test_systemd_manages_only_its_own_home(monkeypatch, tmp_path):
    unit = tmp_path / "botpushfiles.service"
    monkeypatch.setattr(botctl, "SYSTEM", "Linux")
    monkeypatch.setattr(botctl, "systemd_unit_path", lambda: unit)
    assert not botctl.systemd_manages(tmp_path)  # No unit at all
    unit.write_text(botctl.systemd_unit(["/py", "run_bot.py"], tmp_path / "real"))
    assert botctl.systemd_manages(tmp_path / "real")
    assert not botctl.systemd_manages(tmp_path / "other")  # A test or second copy must not touch the real service.


def test_stop_does_not_touch_systemd_for_other_home(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(botctl, "running_pid", lambda home: None)
    monkeypatch.setattr(botctl, "run", lambda cmd: calls.append(cmd))
    assert botctl.stop(tmp_path) == "Bot không chạy."
    assert calls == []


# --- Tray icon, menu entry, single window -------------------------------------------------------------------------


def test_desktop_entries(tmp_path):
    entry = botctl.desktop_entry(["/py", "/app/settings_app.py", "--tray"], tmp_path, tray=True)
    assert "Exec=/py /app/settings_app.py --tray\n" in entry
    assert f"Icon={botctl.icon_path()}\n" in entry
    assert "NoDisplay=true" in entry  # The tray autostart entry must not show up in the app menu.
    assert "NoDisplay" not in botctl.desktop_entry(["/py", "/app/settings_app.py"], tmp_path)


def test_tray_autostart_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(botctl, "SYSTEM", "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    botctl.set_tray_autostart(True, tmp_path)
    path = tmp_path / "config" / "autostart" / "botpushfiles-tray.desktop"
    assert "--tray" in path.read_text()
    botctl.set_tray_autostart(False, tmp_path)
    assert not path.exists()


def test_tray_follows_bot_autostart(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(botctl, "set_bot_autostart", lambda on, home: "" if on else "systemd refused")
    monkeypatch.setattr(botctl, "set_tray_autostart", lambda on, home: calls.append(on))
    assert botctl.set_autostart(True, tmp_path) == ""
    assert botctl.set_autostart(False, tmp_path) == "systemd refused"
    assert calls == [True]  # A failed change leaves the tray setting alone.


def test_open_settings_only_once(monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(botctl, "spawn_detached", lambda cmd, home: spawned.append(cmd))
    assert botctl.open_settings(tmp_path)
    botctl.claim(botctl.ui_pid_path(tmp_path))  # This test process now counts as an open window.
    assert not botctl.open_settings(tmp_path)
    assert len(spawned) == 1
    botctl.release(botctl.ui_pid_path(tmp_path))
    assert not botctl.ui_pid_path(tmp_path).exists()


def test_alive_pid_checks_command_line(tmp_path):
    pidfile = tmp_path / "x.pid"
    botctl.claim(pidfile)
    assert botctl.alive_pid(pidfile, lambda cmd: True) == __import__("os").getpid()
    assert botctl.alive_pid(pidfile, lambda cmd: "--tray" in cmd) is None
