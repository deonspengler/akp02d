"""The themes list: every theme in the themes folder, above the layers.

It shows and asks; the window does. A row is a theme folder; the one
open in the editor is bold, the one on the panel has a green dot. Edit
(or a double-click), Apply and Duplicate are buttons under the list;
Rename and Delete are on the right-click menu, beside the same three.

The folder is watched, so a theme added in a file manager -- or a panel
switched with `akp02d ctl` -- shows without a refresh.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QPushButton, QVBoxLayout, QWidget,
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
    apply = Signal(str)
    duplicate = Signal(str)
    rename = Signal(str)
    delete = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._open: str | None = None
        self._panel: str | None = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.list = QListWidget(self)
        self.list.setObjectName("themes")
        self.list.setStyleSheet(
            f"QListWidget {{ background: {style.PANEL}; border: none; outline: none; }}"
            f"QListWidget::item {{ height: 28px; padding-left: 4px; }}"
            f"QListWidget::item:selected {{ background: #24425a; color: {style.TEXT}; }}")
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemDoubleClicked.connect(lambda item: self.edit.emit(item.text()))
        self.list.currentItemChanged.connect(lambda *_: self._enable())
        column.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(14, 0, 14, 8)
        buttons.setSpacing(6)
        self.buttons: dict[str, QPushButton] = {}
        for key, text, tip in (
            ("edit", "Edit", "Open this theme in the editor (or double-click it)"),
            ("apply", "Apply", "Show this theme on the panel"),
            ("duplicate", "Duplicate", "Copy this theme under a new name, and open the copy"),
        ):
            button = QPushButton(text, self)
            button.setToolTip(tip)
            button.setObjectName(f"theme.{key}")
            button.clicked.connect(lambda _c=False, k=key: self._emit(k))
            buttons.addWidget(button)
            self.buttons[key] = button
        column.addLayout(buttons)

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
        selected = self.selected()
        self.list.blockSignals(True)
        self.list.clear()
        for name in paths.theme_names(folder):
            item = QListWidgetItem(name, self.list)
            item.setToolTip(str(folder / name))
        self.list.blockSignals(False)
        self._mark()
        self.select(selected or self._open)

    def set_open(self, folder: Path | None) -> None:
        """The theme open in the editor: bold, if it is one of these."""
        name = None
        if folder is not None:
            try:
                if folder.resolve().parent == paths.themes_dir().resolve():
                    name = folder.name
            except OSError:
                pass
        if name != self._open:
            self._open = name
            self._mark()
            if name is not None:
                self.select(name)

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
        name = self.selected()
        for button in self.buttons.values():
            button.setEnabled(name is not None)
        if name is not None:
            self.buttons["edit"].setEnabled(name != self._open)

    def selected(self) -> str | None:
        item = self.list.currentItem()
        return item.text() if item is not None else None

    def select(self, name: str | None) -> None:
        for row in range(self.list.count()):
            if self.list.item(row).text() == name:
                self.list.setCurrentRow(row)
                return

    def names(self) -> list[str]:
        return [self.list.item(row).text() for row in range(self.list.count())]

    # -- asking --

    def _emit(self, key: str) -> None:
        name = self.selected()
        if name is not None:
            getattr(self, key).emit(name)

    def _menu(self, pos: QPoint) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        self.list.setCurrentItem(item)
        name = item.text()
        menu = QMenu(self)
        edit = menu.addAction("Edit")
        edit.setEnabled(name != self._open)
        apply = menu.addAction("Apply to panel")
        duplicate = menu.addAction("Duplicate…")
        menu.addSeparator()
        rename = menu.addAction("Rename…")
        delete = menu.addAction("Delete…")
        if name == self._panel:
            for action in (rename, delete):
                action.setEnabled(False)
                action.setToolTip("On the panel: switch it to another theme first")
        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        signal = {edit: self.edit, apply: self.apply, duplicate: self.duplicate,
                  rename: self.rename, delete: self.delete}.get(chosen)
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
