"""Draw the app icon (an upload arrow into a tray on a blue tile) into assets/: png, ico and icns.

    .venv/bin/python make_icon.py
"""

import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ASSETS = Path(__file__).resolve().parent / "assets"


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 100  # Draw on a 100×100 grid.

    gradient = QLinearGradient(0, 0, 0, size)
    gradient.setColorAt(0, QColor("#3B82F6"))
    gradient.setColorAt(1, QColor("#1D4ED8"))
    p.setBrush(gradient)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(4 * s, 4 * s, 92 * s, 92 * s), 22 * s, 22 * s)

    pen = QPen(QColor("white"), 8 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # Arrow pointing up.
    p.drawLine(QPointF(50 * s, 60 * s), QPointF(50 * s, 22 * s))
    arrow = QPainterPath(QPointF(34 * s, 37 * s))
    arrow.lineTo(50 * s, 21 * s)
    arrow.lineTo(66 * s, 37 * s)
    p.drawPath(arrow)
    # Tray.
    tray = QPainterPath(QPointF(24 * s, 58 * s))
    tray.lineTo(24 * s, 76 * s)
    tray.lineTo(76 * s, 76 * s)
    tray.lineTo(76 * s, 58 * s)
    p.drawPath(tray)
    p.end()
    return image


def main() -> int:
    QGuiApplication(sys.argv)
    ASSETS.mkdir(exist_ok=True)
    # .ico tops out at 256 px; macOS wants a large .icns.
    for name, size in (("icon.png", 512), ("icon.ico", 256), ("icon.icns", 1024)):
        draw(size).save(str(ASSETS / name))
        print(ASSETS / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
