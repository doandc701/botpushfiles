"""Tray icon (Windows, macOS, Linux): open the settings window, start/stop the bot, see whether it runs.

It is its own process (settings_app.py --tray) so it stays after the settings window is closed. Qt is used
because it shows tray icons with menus on all three systems, including GNOME on Wayland.
"""

import signal
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import botctl

REFRESH_MS = 3000


class Notifier(QObject):
    """Carries results from worker threads back to the Qt thread."""

    done = Signal(str)


def tray_main(home: Path | None = None) -> int:
    home = home or botctl.app_home()
    if botctl.tray_pid(home):
        return 0  # The icon is already in the tray.
    app = QApplication(sys.argv)
    app.setApplicationName("Bot Push Files")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("No system tray on this desktop", file=sys.stderr)
        return 1
    botctl.claim(botctl.home_tray_pid_path(home))

    icon_on = QIcon(str(botctl.icon_path()))
    icon_off = QIcon(icon_on.pixmap(64, 64, QIcon.Mode.Disabled))  # Greyed out while the bot is stopped
    tray = QSystemTrayIcon(icon_on)
    menu = QMenu()
    open_action = QAction("Mở cài đặt")
    toggle_action = QAction("Bật bot")
    restart_action = QAction("Khởi động lại bot")
    quit_action = QAction("Ẩn icon")
    menu.addAction(open_action)
    menu.addSeparator()
    menu.addAction(toggle_action)
    menu.addAction(restart_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    notifier = Notifier()
    busy = threading.Event()

    def refresh() -> None:
        running = botctl.running_pid(home) is not None
        tray.setIcon(icon_on if running else icon_off)
        tray.setToolTip("Bot Push Files: " + ("bot đang chạy" if running else "bot đang tắt"))
        toggle_action.setText("Tắt bot" if running else "Bật bot")
        toggle_action.setEnabled(not busy.is_set())
        restart_action.setEnabled(running and not busy.is_set())

    def in_background(action) -> None:
        """start/stop can take seconds; run them off the Qt thread so the menu stays responsive."""

        def work() -> None:
            try:
                message = action()
            except Exception as e:  # Shown as a notification rather than killing the tray.
                message = f"Lỗi: {e}"
            busy.clear()
            notifier.done.emit(message)

        busy.set()
        refresh()
        threading.Thread(target=work, daemon=True).start()

    def toggle() -> None:
        if botctl.running_pid(home):
            in_background(lambda: botctl.stop(home))
        elif botctl.validate(botctl.read_env(botctl.env_path(home))):
            tray.showMessage("Bot Push Files", "Chưa cài đặt xong. Mở cài đặt để điền token.")
            botctl.open_settings(home)
        else:
            in_background(lambda: botctl.start(home))

    def restart() -> None:
        def work() -> str:
            botctl.stop(home)
            botctl.start(home)
            return "Đã khởi động lại bot."

        in_background(work)

    def on_done(message: str) -> None:
        tray.showMessage("Bot Push Files", message, QSystemTrayIcon.MessageIcon.Information, 3000)
        refresh()

    def on_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            botctl.open_settings(home)

    open_action.triggered.connect(lambda: botctl.open_settings(home))
    toggle_action.triggered.connect(toggle)
    restart_action.triggered.connect(restart)
    quit_action.triggered.connect(app.quit)
    notifier.done.connect(on_done)
    tray.activated.connect(on_activated)

    # Quit cleanly on SIGTERM (logout, kill). Python handles signals when Qt hands it control, which the
    # refresh timer does every few seconds.
    signal.signal(signal.SIGTERM, lambda *_: app.quit())
    timer = QTimer()
    timer.timeout.connect(refresh)
    timer.start(REFRESH_MS)
    refresh()
    tray.show()
    try:
        return app.exec()
    finally:
        botctl.release(botctl.home_tray_pid_path(home))


if __name__ == "__main__":
    sys.exit(tray_main())
