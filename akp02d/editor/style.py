"""Look and feel: a dark Fusion palette and a few drawn icons.

Dark because the thing being edited is a dark panel with bright
widgets, and judging colour against a white window is misleading. The
system font and Fusion style are kept, so it still reads as a Qt app on
whatever desktop runs it.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication

GROUND = "#16181d"
CHROME = "#1e2128"
PANEL = "#1b1e24"
FIELD = "#22262e"
RAISED = "#2a2f39"
BORDER = "#333946"
TEXT = "#e6e8ec"
MUTED = "#a3a9b6"
FAINT = "#8b92a0"
ACCENT = "#3daee9"
OK = "#4cc38a"
# The theme on the panel: a light cyan, apart from OK's "valid" green.
ON_PANEL = "#6ee7f7"
WARN = "#e6c46b"
BAD = "#ff6b6b"
CANVAS = "#121418"
# The two lists on the left (themes, layers): a size of their own, not
# the desktop's default, which can be small enough to squint at.
LIST_PX = 14
LIST_DETAIL_PX = 13


def list_font(px: int = LIST_PX, mono: bool = False):
    from PySide6.QtGui import QFont

    font = QFont()
    if mono:
        font.setFamily("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(px)
    return font

QSS = f"""
QMainWindow, QDialog {{ background: {GROUND}; }}
QToolBar {{ background: {CHROME}; border: none; border-bottom: 1px solid #2b303a;
           spacing: 6px; padding: 4px 8px; }}
QStatusBar {{ background: {CHROME}; color: {MUTED}; border-top: 1px solid #2b303a; }}
QStatusBar QLabel {{ color: {MUTED}; padding: 0 8px; }}
QSplitter::handle {{ background: #2b303a; }}
QLabel#heading {{ color: {FAINT}; font-weight: 600; font-size: 12px; }}
QLabel#title {{ font-size: 15px; font-weight: 600; }}
QLabel#mono, QLabel#path {{ font-family: monospace; color: {FAINT}; font-size: 12px; }}
QLabel#hint {{ color: {FAINT}; font-size: 12px; }}
QLabel#ok {{ color: #7fd6a8; font-size: 12px; }}
QLabel#bad {{ color: {BAD}; font-size: 12px; }}
QFrame#side, QWidget#side {{ background: {PANEL}; }}
QFrame#problem {{ background: #2a1d20; border: 1px solid #5a2e33; border-radius: 6px; }}
QFrame#warning {{ background: #2a2619; border: 1px solid #5a4e2e; border-radius: 6px; }}
QFrame#footer {{ background: {CHROME}; border-top: 1px solid #2b303a; }}
QListWidget {{ background: {PANEL}; border: none; outline: none; }}
QListWidget::item {{ height: 30px; padding: 0 6px; border-radius: 5px; color: {TEXT}; }}
QListWidget::item:selected {{ background: #24425a; color: {TEXT}; }}
QListWidget::item:hover:!selected {{ background: {RAISED}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {FIELD}; border: 1px solid {BORDER}; border-radius: 5px;
    padding: 3px 6px; color: {TEXT}; selection-background-color: #24425a; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QLineEdit[invalid="true"] {{ border-color: {BAD}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    color: #5c6270; background: {PANEL}; border-color: #2a2f39;
}}
QComboBox QAbstractItemView {{ background: {FIELD}; border: 1px solid {BORDER};
                               selection-background-color: #24425a; }}
QPushButton, QToolButton {{ background: {FIELD}; border: 1px solid {BORDER};
    border-radius: 5px; padding: 4px 10px; color: {TEXT}; }}
QPushButton:hover, QToolButton:hover {{ background: {RAISED}; }}
QPushButton:checked, QToolButton:checked {{ background: #3a4150; border-color: #4a5160; }}
QPushButton:disabled, QToolButton:disabled {{ color: #6b7180; }}
QPushButton#primary {{ background: {ACCENT}; color: #0d1117; border: none; font-weight: 600; }}
QPushButton#primary:disabled {{ background: #2a4a5c; color: #8aa4b3; }}
QToolButton#flat, QPushButton#flat {{ background: transparent; border: 1px solid transparent; }}
QToolButton#flat:hover, QPushButton#flat:hover {{ background: {RAISED}; }}
QToolButton#seg {{ border-radius: 0; border-left-width: 0; padding: 4px 12px; }}
QToolButton#segFirst {{ border-top-right-radius: 0; border-bottom-right-radius: 0; padding: 4px 12px; }}
QToolButton#segLast {{ border-top-left-radius: 0; border-bottom-left-radius: 0;
                       border-left-width: 0; padding: 4px 12px; }}
QGroupBox {{ border: none; border-top: 1px solid #2b303a; margin-top: 14px; padding-top: 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 0; top: 2px; color: {FAINT};
                    font-weight: 600; font-size: 12px; }}
QScrollArea {{ border: none; background: {PANEL}; }}
QScrollArea > QWidget > QWidget {{ background: {PANEL}; }}
QCheckBox {{ color: {TEXT}; }}
/* Spin boxes without arrows: values are typed, scrolled, or stepped with
   the Up/Down keys. Hidden here rather than with NoButtons, which with a
   styled box makes Qt draw no text at all. */
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    width: 0px; border: none; }}
/* Slim scroll bars in the window's colours, with no arrow buttons. */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: #3a4150; border-radius: 3px; margin: 2px; }}
QScrollBar::handle:vertical {{ min-height: 28px; }}
QScrollBar::handle:horizontal {{ min-width: 28px; }}
QScrollBar::handle:hover {{ background: #4a5263; }}
QScrollBar::handle:pressed {{ background: {ACCENT}; }}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0px; height: 0px; border: none; background: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
QAbstractScrollArea::corner {{ background: transparent; }}
QToolTip {{ background: {FIELD}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu {{ background: {FIELD}; border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background: #24425a; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: GROUND,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: FIELD,
        QPalette.ColorRole.AlternateBase: PANEL,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: FIELD,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.Highlight: "#24425a",
        QPalette.ColorRole.HighlightedText: TEXT,
        QPalette.ColorRole.ToolTipBase: FIELD,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.PlaceholderText: FAINT,
        QPalette.ColorRole.Link: ACCENT,
    }
    for role, colour in roles.items():
        palette.setColor(role, QColor(colour))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#6b7180"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
                     QColor("#6b7180"))
    app.setPalette(palette)
    app.setStyleSheet(QSS)


def plugin_icon(plugin: str, colour: str = MUTED, size: int = 16) -> QIcon:
    """A small line icon for a plugin. Unknown plugins get a box."""
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(size * scale / 16, size * scale / 16)
    pen = QPen(QColor(colour), 1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    path = QPainterPath()
    if plugin == "label":
        path.moveTo(3, 4); path.lineTo(13, 4); path.moveTo(8, 4); path.lineTo(8, 13)
    elif plugin == "gauge":
        path.arcMoveTo(QRectF(3, 6.5, 10, 10), 180)
        path.arcTo(QRectF(3, 6.5, 10, 10), 180, -180)
        path.moveTo(8, 11.5); path.lineTo(10.5, 8.5)
    elif plugin == "graph":
        path.moveTo(2, 12); path.lineTo(5.5, 7); path.lineTo(8.5, 10); path.lineTo(14, 3)
    elif plugin == "spectrum":
        for x, top in ((3, 8), (6, 4), (9, 6), (12, 9)):
            path.moveTo(x, 13); path.lineTo(x, top)
    elif plugin == "bar":
        path.addRoundedRect(QRectF(2, 6, 12, 4.5), 2, 2)
        path.moveTo(4, 8.25); path.lineTo(9, 8.25)
    elif plugin == "image":
        path.addRoundedRect(QRectF(2, 3, 12, 10), 1.5, 1.5)
        path.moveTo(2.5, 12); path.lineTo(6.5, 7.5); path.lineTo(9, 10)
        path.lineTo(10.5, 8.5); path.lineTo(13.5, 12)
        path.addEllipse(QPointF(10.5, 5.8), 1.1, 1.1)
    elif plugin == "theme":
        # Settings: three sliders.
        for y, knob in ((4, 10.5), (8, 5.5), (12, 9)):
            path.moveTo(2.5, y); path.lineTo(13.5, y)
            path.addEllipse(QPointF(knob, y), 1.4, 1.4)
    else:
        path.addRoundedRect(QRectF(3, 3, 10, 10), 2, 2)
    painter.drawPath(path)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return QIcon(pixmap)


def _line_icon(draw, size: int = 20) -> QIcon:
    """An icon from a drawing function, in normal and disabled shades.

    `draw(path)` adds strokes to a QPainterPath in a 16x16 box.
    """
    path = QPainterPath()
    draw(path)
    icon = QIcon()
    for colour, mode in ((TEXT, QIcon.Mode.Normal), ("#5d6371", QIcon.Mode.Disabled)):
        scale = 2
        pixmap = QPixmap(size * scale, size * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size * scale / 16, size * scale / 16)
        pen = QPen(QColor(colour), 1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        icon.addPixmap(pixmap, mode)
    return icon


def action_icon(name: str) -> QIcon:
    """Toolbar icons: new, save, undo, redo; and the themes list's
    duplicate and delete."""

    def new(p: QPainterPath) -> None:
        p.moveTo(9.5, 2); p.lineTo(4, 2); p.lineTo(4, 14); p.lineTo(12.5, 14)
        p.lineTo(12.5, 5); p.lineTo(9.5, 2); p.lineTo(9.5, 5); p.lineTo(12.5, 5)
        p.moveTo(8.25, 7.5); p.lineTo(8.25, 11.5); p.moveTo(6.25, 9.5); p.lineTo(10.25, 9.5)

    def folder(p: QPainterPath) -> None:
        # An open folder: for choosing a picture file.
        p.moveTo(2, 12.5); p.lineTo(2, 3.5); p.lineTo(6, 3.5); p.lineTo(7.5, 5)
        p.lineTo(13, 5); p.lineTo(13, 7)
        p.moveTo(2, 12.5); p.lineTo(4, 7); p.lineTo(14.5, 7); p.lineTo(12.5, 12.5)
        p.closeSubpath()

    def duplicate(p: QPainterPath) -> None:
        # Two sheets, the front one with a plus.
        p.addRoundedRect(QRectF(5.5, 5.5, 8, 8), 1.2, 1.2)
        p.moveTo(3.5, 10.5); p.lineTo(2.5, 10.5); p.lineTo(2.5, 2.5)
        p.lineTo(10.5, 2.5); p.lineTo(10.5, 3.5)
        p.moveTo(9.5, 7.5); p.lineTo(9.5, 11.5); p.moveTo(7.5, 9.5); p.lineTo(11.5, 9.5)

    def delete(p: QPainterPath) -> None:
        # A bin: lid, handle, body, two ribs.
        p.moveTo(2.5, 4.5); p.lineTo(13.5, 4.5)
        p.moveTo(6, 4.5); p.lineTo(6.5, 2.5); p.lineTo(9.5, 2.5); p.lineTo(10, 4.5)
        p.moveTo(3.75, 4.5); p.lineTo(4.75, 13.5); p.lineTo(11.25, 13.5); p.lineTo(12.25, 4.5)
        p.moveTo(6.75, 7); p.lineTo(7, 11.5); p.moveTo(9.25, 7); p.lineTo(9, 11.5)

    def save(p: QPainterPath) -> None:
        p.moveTo(2.5, 2.5); p.lineTo(11, 2.5); p.lineTo(13.5, 5); p.lineTo(13.5, 13.5)
        p.lineTo(2.5, 13.5); p.closeSubpath()
        p.moveTo(5, 2.5); p.lineTo(5, 6); p.lineTo(10.5, 6); p.lineTo(10.5, 2.5)
        p.addRect(QRectF(5, 9, 6, 4.5))

    def undo(p: QPainterPath) -> None:
        p.moveTo(5.5, 3.5); p.lineTo(2.5, 6.5); p.lineTo(5.5, 9.5)
        p.moveTo(2.5, 6.5); p.lineTo(10, 6.5)
        p.cubicTo(15, 6.5, 15, 13, 10, 13); p.lineTo(6.5, 13)

    def redo(p: QPainterPath) -> None:
        p.moveTo(10.5, 3.5); p.lineTo(13.5, 6.5); p.lineTo(10.5, 9.5)
        p.moveTo(13.5, 6.5); p.lineTo(6, 6.5)
        p.cubicTo(1, 6.5, 1, 13, 6, 13); p.lineTo(9.5, 13)

    ring = QRectF(2.5, 2.5, 11, 11)

    def gap_at(p: QPainterPath, bottom: bool) -> None:
        # A scale with its ends -- and its zero -- at the bottom or top:
        # the ring with a gap there, a dot marking the spot.
        begin = 240 if bottom else 60
        p.arcMoveTo(ring, begin)
        p.arcTo(ring, begin, -300)
        y = 13.5 if bottom else 2.5
        p.addEllipse(QPointF(8, y), 0.7, 0.7)

    def turning(p: QPainterPath, clockwise: bool) -> None:
        # Most of a circle with an arrowhead: the way the scale runs.
        sign = 1 if clockwise else -1
        begin = 210 if clockwise else -30
        p.arcMoveTo(ring, begin)
        p.arcTo(ring, begin, -240 * sign)
        end = math.radians(-30 if clockwise else 210)
        tx, ty = 8 + 5.5 * math.cos(end), 8 - 5.5 * math.sin(end)
        # On screen, clockwise travel at angle a heads (sin a, cos a).
        dx, dy = math.sin(end) * sign, math.cos(end) * sign
        px, py = -dy, dx
        for wing in (1, -1):
            p.moveTo(tx, ty)
            p.lineTo(tx - 3.0 * dx + 2.2 * wing * px, ty - 3.0 * dy + 2.2 * wing * py)

    drawings = {"new": new, "save": save, "undo": undo, "redo": redo,
                "duplicate": duplicate, "delete": delete, "folder": folder,
                "start_bottom": lambda p: gap_at(p, True),
                "start_top": lambda p: gap_at(p, False),
                "direction_clockwise": lambda p: turning(p, True),
                "direction_counterclockwise": lambda p: turning(p, False)}
    return _line_icon(drawings[name])
