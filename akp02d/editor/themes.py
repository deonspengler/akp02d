"""The themes list: every theme in the themes folder, above the layers.

It shows and asks; the window does. A row is a theme folder; the one
open in the editor is bold, the one on the panel has a green dot. One
click opens a theme. Duplicate and delete are icons beside the heading,
for the open theme; rename is on the right-click menu with them. Putting
a theme on the panel is the toolbar's Apply to panel.

The folder is watched, so a theme added in a file manager -- or a panel
switched with `akp02d ctl` -- shows without a refresh.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QToolButton, QVBoxLayout, QWidget,
)

from .. import paths
from . import style


def _dot(colour: str | None) -> QIcon:
    """A small status dot, or an empty space the same size."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    if colour is not None:
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(6, 6, 8, 8)
        painter.end()
    pixmap.setDevicePixelRatio(2.0)
    return QIcon(pixmap)


class ThemeList(QWidget):
    """The themes folder as a list, with what can be done to each."""

    edit = Signal(str)
    duplicate = Signal(str)
    rename = Signal(str)
    delete = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._open: str | None = None
        self._panel: str | None = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 12, 0, 0)
        column.setSpacing(0)

        # The heading, with what can be done to the open theme beside it.
        top = QHBoxLayout()
        top.setContentsMargins(14, 0, 8, 4)
        top.setSpacing(2)
        heading = QLabel("THEMES")
        heading.setObjectName("heading")
        top.addWidget(heading)
        top.addStretch(1)
        self.buttons: dict[str, QToolButton] = {}
        for key, tip in (("duplicate", "Duplicate this theme under a new name"),
                         ("delete", "Move this theme to the trash")):
            button = QToolButton(self)
            button.setObjectName(f"theme.{key}")
            button.setIcon(style.action_icon(key))
            button.setIconSize(QSize(16, 16))
            button.setToolTip(tip)
            button.setAutoRaise(True)
            button.setStyleSheet("QToolButton { padding: 2px; border: none; "
                                 "background: transparent; }"
                                 f"QToolButton:hover {{ background: {style.RAISED}; }}")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _c=False, k=key: self._emit(k))
            top.addWidget(button)
            self.buttons[key] = button
        column.addLayout(top)

        self.list = QListWidget(self)
        self.list.setObjectName("themes")
        self.list.setStyleSheet(
            f"QListWidget {{ background: {style.PANEL}; border: none; outline: none; }}"
            f"QListWidget::item {{ height: 28px; padding-left: 4px; }}"
            f"QListWidget::item:selected {{ background: #24425a; color: {style.TEXT}; }}")
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        # One click opens -- or Enter, from the keyboard. Moving through
        # the list with the arrow keys alone does not.
        self.list.itemClicked.connect(lambda item: self.edit.emit(item.text()))
        self.list.itemActivated.connect(lambda item: self.edit.emit(item.text()))
        column.addWidget(self.list, 1)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(lambda _p: self._soon.start())
        # A copy or a save touches the folder several times: one refresh.
        self._soon = QTimer(self)
        self._soon.setSingleShot(True)
        self._soon.setInterval(150)
        self._soon.timeout.connect(self.reload)
        self.reload()

    # -- what it shows --

    def reload(self) -> None:
        folder = paths.themes_dir()
        if folder.is_dir() and str(folder) not in self._watcher.directories():
            self._watcher.addPath(str(folder))
        self.list.blockSignals(True)
        self.list.clear()
        for name in paths.theme_names(folder):
            QListWidgetItem(name, self.list)
        self.list.blockSignals(False)
        self._mark()
        self.show_open()

    def set_open(self, folder: Path | None) -> None:
        """The theme open in the editor: bold and selected, if it is one
        of these."""
        name = None
        if folder is not None:
            try:
                if folder.resolve().parent == paths.themes_dir().resolve():
                    name = folder.name
            except OSError:
                pass
        self._open = name
        self._mark()
        self.show_open()

    def show_open(self) -> None:
        """Select the open theme again -- after a click that did not open
        another (its unsaved changes were kept)."""
        self.list.blockSignals(True)
        self.list.clearSelection()
        for row in range(self.list.count()):
            if self.list.item(row).text() == self._open:
                self.list.setCurrentRow(row)
                break
        else:
            self.list.setCurrentItem(None)
        self.list.blockSignals(False)
        self._enable()

    def set_panel(self, name: str | None) -> None:
        """The theme on the panel (None: no daemon): a green dot."""
        if name != self._panel:
            self._panel = name
            self._mark()

    def _mark(self) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            font = QFont(item.font())
            font.setBold(item.text() == self._open)
            item.setFont(font)
            on_panel = item.text() == self._panel
            item.setIcon(_dot(style.OK if on_panel else None))
            item.setForeground(QBrush(QColor(style.TEXT if item.text() == self._open
                                              else style.MUTED)))
            tip = str(paths.themes_dir() / item.text())
            if on_panel:
                tip += "\nOn the panel now"
            item.setToolTip(tip)
        self._enable()

    def _enable(self) -> None:
        self.buttons["duplicate"].setEnabled(self._open is not None)
        deletable = self._open is not None and self._open != self._panel
        self.buttons["delete"].setEnabled(deletable)
        self.buttons["delete"].setToolTip(
            "Move this theme to the trash" if deletable or self._open is None
            else "On the panel: switch the panel to another theme first")

    def selected(self) -> str | None:
        item = self.list.currentItem()
        return item.text() if item is not None else None

    def names(self) -> list[str]:
        return [self.list.item(row).text() for row in range(self.list.count())]

    # -- asking --

    def _emit(self, key: str) -> None:
        if self._open is not None:
            getattr(self, key).emit(self._open)

    def _menu(self, pos: QPoint) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        name = item.text()
        menu = QMenu(self)
        duplicate = menu.addAction("Duplicate…")
        rename = menu.addAction("Rename…")
        menu.addSeparator()
        delete = menu.addAction("Delete…")
        if name == self._panel:
            for action in (rename, delete):
                action.setEnabled(False)
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        signal = {duplicate: self.duplicate, rename: self.rename,
                  delete: self.delete}.get(chosen)
        if signal is not None:
            signal.emit(name)


def ask_name(parent: QWidget, title: str, label: str, suggested: str = "") -> str | None:
    """A new theme's name, checked as it will be used: a folder in the
    themes folder that does not exist yet. None if cancelled."""
    name = suggested
    while True:
        name, ok = QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Normal, name)
        if not ok:
            return None
        name = name.strip()
        problem = paths.name_problem(name)
        if problem is None:
            return name
        label = f"{problem}\n\n{label.splitlines()[-1]}"
