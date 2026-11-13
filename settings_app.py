"""Settings app for botpushfiles (Windows, macOS, Linux): edit .env, start/stop the bot, read its log, start at login.

Run from source:  .venv/bin/python settings_app.py
The packaged app is this same file; it also serves as the launcher (--launcher), the bot (--bot) and the
tray icon (--tray).
"""

import asyncio
import sys

import flet as ft

import botctl

POLL_SECONDS = 1.5
MONO = "monospace"


def main(page: ft.Page) -> None:
    home = botctl.app_home()
    env_file = botctl.env_path(home)
    page.title = "Bot Push Files"
    page.theme_mode = ft.ThemeMode.SYSTEM
    page.theme = ft.Theme(color_scheme_seed=ft.Colors.BLUE)
    page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.BLUE)
    page.window.width = 860
    page.window.height = 760
    page.window.min_width = 520
    page.window.min_height = 560
    icon = botctl.icon_path(".ico" if botctl.SYSTEM == "Windows" else ".png")
    if icon.exists():
        page.window.icon = str(icon)
    # One settings window at a time; the tray icon opens this window again after it is closed.
    botctl.claim(botctl.ui_pid_path(home))
    botctl.install_menu_entry(home)
    try:
        botctl.ensure_tray(home)
    except OSError as exc:
        print(f"Tray icon not started: {exc}", file=sys.stderr)
    page.padding = 16

    def toast(message: str, error: bool = False) -> None:
        page.show_dialog(
            ft.SnackBar(ft.Text(message), bgcolor=ft.Colors.ERROR_CONTAINER if error else None, duration=4000)
        )

    # --- Settings form ---------------------------------------------------------------------------------------

    values = botctl.read_env(env_file)
    inputs: dict[str, ft.Control] = {}
    sections: list[ft.Control] = []
    for group in dict.fromkeys(f.group for f in botctl.FIELDS):
        controls: list[ft.Control] = [ft.Text(group, size=16, weight=ft.FontWeight.BOLD)]
        for field in (f for f in botctl.FIELDS if f.group == group):
            if field.switch:
                control = ft.Switch(label=f"{field.label}: {field.help}", value=values.get(field.key, "1") != "0")
            else:
                control = ft.TextField(
                    label=field.label,
                    value=values.get(field.key, ""),
                    helper=field.help,
                    helper_max_lines=2,
                    password=field.secret,
                    can_reveal_password=field.secret,
                    dense=True,
                )
            inputs[field.key] = control
            controls.append(control)
        column = ft.Column(controls, spacing=12, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        sections.append(ft.Card(ft.Container(column, padding=16)))

    def form_values() -> dict[str, str]:
        result = {}
        for key, control in inputs.items():
            if isinstance(control, ft.Switch):
                result[key] = "1" if control.value else "0"
            else:
                result[key] = (control.value or "").strip()
        return result

    def show_errors(errors: list[str]) -> bool:
        for field in botctl.FIELDS:
            control = inputs[field.key]
            if isinstance(control, ft.TextField):
                control.error = next((e for e in errors if field.key in e or field.label.lower() in e.lower()), None)
        page.update()
        if errors:
            toast(errors[0], error=True)
        return not errors

    async def save(e=None) -> bool:
        data = form_values()
        if not show_errors(botctl.validate(data)):
            return False
        botctl.write_env(env_file, data, template=botctl.SOURCE_DIR / ".env.example")
        toast("Đã lưu. Khởi động lại bot để áp dụng." if botctl.running_pid(home) else "Đã lưu cài đặt.")
        return True

    check_results = ft.Column(spacing=4)

    async def check(e) -> None:
        data = form_values()
        if not show_errors(botctl.validate(data)):
            return
        check_results.controls = [ft.Row([ft.ProgressRing(width=16, height=16), ft.Text("Đang hỏi Telegram...")])]
        page.update()
        results = await asyncio.to_thread(botctl.check_telegram, data)
        check_results.controls = [
            ft.Row(
                [
                    ft.Icon(
                        ft.Icons.CHECK_CIRCLE if ok else ft.Icons.ERROR, color=ft.Colors.GREEN if ok else ft.Colors.RED
                    ),
                    ft.Text(text, expand=True),
                ]
            )
            for ok, text in results
        ]
        page.update()

    settings_tab = ft.ListView(
        [
            *sections,
            ft.Row(
                [
                    ft.FilledButton("Lưu", icon=ft.Icons.SAVE, on_click=save),
                    ft.OutlinedButton("Kiểm tra token và channel", icon=ft.Icons.FACT_CHECK, on_click=check),
                ],
                wrap=True,
            ),
            check_results,
            ft.Text(f"File cài đặt: {env_file}", size=12, color=ft.Colors.ON_SURFACE_VARIANT, selectable=True),
        ],
        spacing=12,
        padding=ft.Padding(0, 12, 0, 12),
        expand=True,
    )

    # --- Log -------------------------------------------------------------------------------------------------

    log_text = ft.Text("", font_family=MONO, size=12, selectable=True)
    log_view = ft.ListView([log_text], expand=True, auto_scroll=True, padding=12)
    log_tab = ft.Column(
        [
            ft.Text(f"Log: {botctl.log_path(home)}", size=12, color=ft.Colors.ON_SURFACE_VARIANT, selectable=True),
            ft.Container(log_view, expand=True, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT), border_radius=8),
        ],
        expand=True,
    )

    # --- Header: status, start/stop, start at login ----------------------------------------------------------

    status_dot = ft.Icon(ft.Icons.CIRCLE, size=14)
    status_text = ft.Text(weight=ft.FontWeight.BOLD)
    busy = ft.ProgressRing(width=18, height=18, visible=False)

    async def run_action(action, message_ok: str | None = None) -> None:
        busy.visible = True
        for b in (start_btn, stop_btn, restart_btn):
            b.disabled = True
        page.update()
        try:
            message = await asyncio.to_thread(action)
            toast(message_ok or message)
        except Exception as exc:  # Shown to the user rather than crashing the window.
            toast(f"Lỗi: {exc}", error=True)
        finally:
            busy.visible = False
            await refresh()

    async def on_start(e) -> None:
        if not env_file.exists() and not await save():
            return
        if botctl.validate(botctl.read_env(env_file)):
            toast("Điền và lưu cài đặt trước khi bật bot.", error=True)
            return
        await run_action(lambda: botctl.start(home))

    async def on_stop(e) -> None:
        await run_action(lambda: botctl.stop(home))

    async def on_restart(e) -> None:
        def restart() -> str:
            botctl.stop(home)
            return botctl.start(home)

        await run_action(restart, "Đã khởi động lại bot.")

    start_btn = ft.FilledButton("Bật bot", icon=ft.Icons.PLAY_ARROW, on_click=on_start)
    stop_btn = ft.OutlinedButton("Tắt bot", icon=ft.Icons.STOP, on_click=on_stop)
    restart_btn = ft.OutlinedButton("Khởi động lại", icon=ft.Icons.RESTART_ALT, on_click=on_restart)

    async def on_autostart(e) -> None:
        wanted = autostart.value
        error = await asyncio.to_thread(botctl.set_autostart, wanted, home)
        if error:
            autostart.value = not wanted
            toast(f"Không đổi được chế độ tự chạy: {error}", error=True)
        else:
            toast("Bot sẽ tự chạy khi bật máy." if wanted else "Đã tắt tự chạy khi bật máy.")
        page.update()

    autostart = ft.Switch(label="Tự chạy khi bật máy", value=False, on_change=on_autostart)

    async def refresh() -> None:
        running = botctl.running_pid(home) is not None
        status_dot.color = ft.Colors.GREEN if running else ft.Colors.GREY
        status_text.value = "Bot đang chạy" if running else "Bot đang tắt"
        start_btn.disabled = running
        stop_btn.disabled = restart_btn.disabled = not running
        log = botctl.tail(botctl.log_path(home))
        if log != log_text.value:
            log_text.value = log or "(chưa có log)"
        page.update()

    header = ft.Row(
        [
            ft.Row([status_dot, status_text, busy], spacing=8),
            ft.Row([start_btn, stop_btn, restart_btn, autostart], wrap=True, spacing=8),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        wrap=True,
    )

    page.add(
        ft.SafeArea(
            ft.Column(
                [
                    header,
                    ft.Tabs(
                        length=2,
                        expand=True,
                        content=ft.Column(
                            [
                                ft.TabBar(
                                    tabs=[ft.Tab(label="Cài đặt", icon=ft.Icons.SETTINGS),
                                          ft.Tab(label="Log", icon=ft.Icons.TERMINAL)]
                                ),  # fmt: skip
                                ft.TabBarView([settings_tab, log_tab], expand=True),
                            ],
                            expand=True,
                        ),
                    ),
                ],
                expand=True,
            ),
            expand=True,
        )
    )

    async def poll() -> None:
        autostart.value = await asyncio.to_thread(botctl.autostart_enabled, home)
        while True:
            await refresh()
            await asyncio.sleep(POLL_SECONDS)

    page.run_task(poll)


def entry() -> None:
    # The packaged app has no separate Python: it runs itself as the launcher and as the bot.
    if "--launcher" in sys.argv:
        sys.exit(botctl.launcher_main())
    if "--tray" in sys.argv:
        from tray_app import tray_main

        sys.exit(tray_main())
    if "--bot" in sys.argv:
        import bot

        bot.main()
        return
    home = botctl.app_home()
    if botctl.ui_pid(home):
        return  # Already open (e.g. clicked in the menu twice).
    try:
        ft.run(main)
    finally:
        botctl.release(botctl.ui_pid_path(home))


if __name__ == "__main__":
    entry()
