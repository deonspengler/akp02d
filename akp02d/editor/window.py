"""The editor window: layers, canvas, settings, and the loop that ticks.

Every change goes through one path: an edit to the ThemeDocument's text,
recorded as an undo step, followed by `refresh()`, which brings the
preview engine, the canvas, the layers list and the settings panel in
line with the document. The document is the only state; everything on
screen is derived from it, so undo is just "put the old text back".
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction, QCloseEvent, QColor, QImage, QKeySequence, QUndoCommand, QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QSplitter, QToolBar,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import akp02d
from akp02 import Orientation

from .. import config as config_module, sources
from ..layout import Rect
from ..plugins import available, lookup
from ..schema import SOURCE
from .canvas import Box, Canvas
from .controls import Segmented
from .document import DocumentError, Ref, ThemeDocument
from .engine import ERROR, Engine, Problem, snap
from .inspector import Inspector
from . import style

log = logging.getLogger(__name__)

THEME = "theme"   # selection sentinel: the [theme] table

DEFAULT_SIZES = {
    "label": (240, 48), "gauge": (160, 160), "graph": (320, 120), "spectrum": (320, 200),
    "bar": (320, 48), "image": (160, 160),
}
NEW_OPTIONS: dict[str, dict[str, Any]] = {
    "label": {"format": "New label", "size": 32},
    "bar": {"label": "CPU", "format": "{percent:.0f}%"},
}

PACKAGE_DIR = Path(akp02d.__file__).resolve().parent


class _Edit(QUndoCommand):
    """One change to the document's text. Merges with the next edit of
    the same key, so dragging a slider is one undo step."""

    def __init__(self, window: EditorWindow, before: str, after: str, text: str,
                 merge: str | None) -> None:
        super().__init__(text)
        self.window, self.before, self.after = window, before, after
        self.merge = merge
        self.at = time.monotonic()
        self._first = True

    def id(self) -> int:  # noqa: A003
        return 7 if self.merge else -1

    def mergeWith(self, other: QUndoCommand) -> bool:  # noqa: N802
        # Only edits of the same key in quick succession -- a slider being
        # dragged, a number nudged -- become one step.
        if (not isinstance(other, _Edit) or not self.merge or other.merge != self.merge
                or other.at - self.at > 1.5):
            return False
        self.after = other.after
        self.at = other.at
        return True

    def redo(self) -> None:
        if self._first:
            self._first = False
            return
        self.window._restore(self.after)

    def undo(self) -> None:
        self.window._restore(self.before)


def to_qimage(image: Image.Image) -> QImage:
    image = image if image.mode == "RGB" else image.convert("RGB")
    data = image.tobytes("raw", "RGB")
    return QImage(data, image.width, image.height, image.width * 3,
                  QImage.Format.Format_RGB888).copy()


class EditorWindow(QMainWindow):
    def __init__(self, document: ThemeDocument, cfg: config_module.Config) -> None:
        super().__init__()
        self.config = cfg
        self.document = document
        self.engine = Engine(inverted=cfg.device.inverted)
        self.undo = QUndoStack(self)
        self.undo.cleanChanged.connect(lambda _c: self._update_title())
        self.selection: Ref | str | None = None
        self.problems: list[Problem] = []

        self.setWindowTitle("akp02d Theme Editor")
        self.resize(1440, 960)
        self._build_toolbar()
        self._build_body()
        self._build_status()
        self._build_actions()

        self._inspector_timer = QTimer(self)
        self._inspector_timer.setSingleShot(True)
        self._inspector_timer.timeout.connect(self._rebuild_inspector)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.timer.start(round(1000 / Engine.LOOP_FPS))
        self.refresh(select_first=False)
        QTimer.singleShot(0, self.canvas.fit)

    # -- construction --

    def _build_toolbar(self) -> None:
        bar = QToolBar("Main", self)
        bar.setMovable(False)
        bar.setIconSize(QSize(20, 20))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(bar)
        self.toolbar = bar

        def action(name: str, icon: str, shortcut: Any, slot: Callable[[], None],
                   tip: str) -> QAction:
            # Drawn rather than taken from the icon theme, so they look the
            # same -- and are legible on the dark toolbar -- on any desktop.
            act = QAction(style.action_icon(icon), name, self)
            act.setShortcut(shortcut)
            act.setToolTip(f"{tip} ({QKeySequence(shortcut).toString()})" if shortcut else tip)
            act.triggered.connect(slot)
            bar.addAction(act)
            return act

        self.act_new = action("New", "new", QKeySequence.StandardKey.New,
                              self.new_theme, "New theme")
        self.act_open = action("Open", "open", QKeySequence.StandardKey.Open,
                               self.open_theme, "Open a theme")
        self.act_save = action("Save", "save", QKeySequence.StandardKey.Save,
                               self.save, "Save")
        # Fixed labels, with what they would undo in the tooltip: the
        # stack's own actions rename themselves ("Undo Add spectrum") and
        # push the rest of the toolbar around as they do.
        self.act_undo = action("Undo", "undo", QKeySequence.StandardKey.Undo,
                               self.undo.undo, "Undo")
        self.act_redo = action("Redo", "redo", QKeySequence.StandardKey.Redo,
                               self.undo.redo, "Redo")
        self.act_undo.setEnabled(False)
        self.act_redo.setEnabled(False)
        self.undo.canUndoChanged.connect(self.act_undo.setEnabled)
        self.undo.canRedoChanged.connect(self.act_redo.setEnabled)
        self.undo.undoTextChanged.connect(
            lambda text: self.act_undo.setToolTip(f"Undo {text}" if text else "Undo"))
        self.undo.redoTextChanged.connect(
            lambda text: self.act_redo.setToolTip(f"Redo {text}" if text else "Redo"))
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        export = QPushButton("Export PNG")
        export.clicked.connect(self.export_png)
        bar.addWidget(export)

    def _build_body(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        # Left: theme, layers, add.
        left = QWidget()
        left.setObjectName("side")
        left.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        column = QVBoxLayout(left)
        column.setContentsMargins(0, 12, 0, 0)
        column.setSpacing(0)
        heading = QLabel("LAYERS")
        heading.setObjectName("heading")
        heading.setContentsMargins(14, 0, 14, 6)
        column.addWidget(heading)
        self.layers = QTreeWidget()
        self.layers.setHeaderHidden(True)
        self.layers.setColumnCount(2)
        self.layers.setRootIsDecorated(False)
        self.layers.setIndentation(6)
        self.layers.setIconSize(QSize(16, 16))
        self.layers.setStyleSheet(
            f"QTreeWidget {{ background: {style.PANEL}; border: none; outline: none; }}"
            f"QTreeWidget::item {{ height: 30px; }}"
            f"QTreeWidget::item:selected {{ background: #24425a; color: {style.TEXT}; }}")
        self.layers.itemSelectionChanged.connect(self._layer_picked)
        column.addWidget(self.layers, 1)
        add = QFrame()
        add.setStyleSheet("QFrame { border-top: 1px solid #2b303a; }")
        add_column = QVBoxLayout(add)
        add_column.setContentsMargins(14, 12, 14, 14)
        heading = QLabel("ADD WIDGET")
        heading.setObjectName("heading")
        heading.setStyleSheet("border: none;")
        add_column.addWidget(heading)
        grid = QGridLayout()
        grid.setSpacing(6)
        for index, plugin in enumerate(available()):
            button = QPushButton(style.plugin_icon(plugin, style.TEXT), plugin.capitalize())
            button.setMinimumHeight(40)
            cls = lookup(plugin)
            if cls is not None and cls.summary:
                button.setToolTip(cls.summary)
            button.clicked.connect(lambda _c=False, p=plugin: self.add_widget(p))
            grid.addWidget(button, index // 2, index % 2)
        add_column.addLayout(grid)
        column.addWidget(add)
        left.setMinimumWidth(230)
        splitter.addWidget(left)

        # Middle: the canvas.
        self.canvas = Canvas()
        canvas_column = QWidget()
        canvas_layout = QVBoxLayout(canvas_column)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)
        strip = QFrame()
        strip.setStyleSheet(f"QFrame {{ background: {style.CHROME}; border-bottom: 1px solid #2b303a; }}")
        strip_row = QHBoxLayout(strip)
        strip_row.setContentsMargins(10, 4, 10, 4)
        # One or the other: fitted to the view, or actual pixels. Neither
        # is lit after a Ctrl+wheel zoom -- the level shows beside them
        # then -- and clicking one goes back to it.
        self.zoom_seg = Segmented((("fit", "Fit"), ("100", "100%")))
        self.zoom_seg.setToolTip("Fit the whole panel in view, or show it pixel for pixel. "
                                 "Ctrl+wheel zooms in between.")
        self.zoom_seg.set_value("fit")
        self.zoom_seg.changed.connect(
            lambda mode: self.canvas.fit() if mode == "fit" else self.canvas.set_zoom(1.0))
        strip_row.addWidget(self.zoom_seg)
        self.zoom_label = QLabel()
        self.zoom_label.setObjectName("mono")
        self.zoom_label.hide()
        strip_row.addWidget(self.zoom_label)
        strip_row.addStretch(1)
        self.boxes_box = QCheckBox("Outlines")
        self.boxes_box.setChecked(True)
        self.boxes_box.toggled.connect(self._boxes_toggled)
        strip_row.addWidget(self.boxes_box)
        canvas_layout.addWidget(strip)
        canvas_layout.addWidget(self.canvas, 1)
        splitter.addWidget(canvas_column)

        self.canvas.selected.connect(self._canvas_picked)
        self.canvas.geometry_committed.connect(self._geometry_from_canvas)
        self.canvas.delete_requested.connect(self.delete_widget)
        self.canvas.duplicate_requested.connect(self.duplicate_widget)
        self.canvas.status.connect(lambda text: self.statusBar().showMessage(text, 4000))
        self.canvas.view_changed.connect(self._zoom_changed)
        self.canvas.hovered.connect(self._hovered)

        # Right: settings.
        self.inspector = Inspector()
        self.inspector.setMinimumWidth(330)
        self.inspector.widget_edited.connect(self._widget_edited)
        self.inspector.geometry_edited.connect(self._geometry_edited)
        self.inspector.theme_edited.connect(
            lambda key, value: self.edit_theme(key, value, f"Change {key}"))
        self.inspector.problem_clicked.connect(self._select_id)
        splitter.addWidget(self.inspector)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([250, 820, 360])

    def _build_status(self) -> None:
        bar = self.statusBar()
        self.status_shape = QLabel()
        self.status_mount = QLabel("Mount: inverted" if self.config.device.inverted
                                   else "Mount: upright")
        self.status_dirty = QLabel()
        # Left: the pixel under the mouse. Hidden while a message (a drag
        # readout, "Saved") is showing; the right-hand facts never are.
        self.status_hover = QLabel()
        self.status_hover.setTextFormat(Qt.TextFormat.RichText)
        bar.addWidget(self.status_hover)
        for label in (self.status_shape, self.status_mount, self.status_dirty):
            bar.addPermanentWidget(label)

    def _build_actions(self) -> None:
        save_as = QAction("Save As…", self)
        save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        save_as.triggered.connect(lambda: self.save(force_choose=True))
        self.addAction(save_as)
        deselect = QAction("Deselect", self)
        deselect.setShortcut(QKeySequence("Ctrl+D"))
        deselect.triggered.connect(lambda: self._set_selection(None))
        self.addAction(deselect)
        duplicate = QAction("Duplicate", self)
        duplicate.setShortcut(QKeySequence("Ctrl+J"))
        duplicate.triggered.connect(lambda: isinstance(self.selection, Ref) and
                                    self.duplicate_widget(self._id_of(self.selection)))
        self.addAction(duplicate)

    # -- the document --

    def apply(self, description: str, change: Callable[[ThemeDocument], Any],
              merge: str | None = None) -> Any:
        """Make one undoable change to the document."""
        before = self.document.text
        try:
            result = change(self.document)
        except DocumentError as exc:
            self.document._set_text(before)
            QMessageBox.warning(self, "Cannot apply", str(exc))
            return None
        after = self.document.text
        if after != before:
            self.undo.push(_Edit(self, before, after, description, merge))
            self.refresh()
        return result

    def _restore(self, text: str) -> None:
        self.document._set_text(text)
        self.refresh()

    def refresh(self, select_first: bool = False) -> None:
        """Bring everything on screen in line with the document."""
        doc = self.document
        self.engine.load(doc)
        self.problems = self.engine.problems()
        if isinstance(self.selection, Ref) and self.selection not in doc.refs():
            self.selection = None
        orientation = self.engine.orientation
        self.canvas.set_rules(orientation, self.config.device.inverted)
        self._update_boxes()
        self._update_layers()
        self._update_inspector()
        self._update_frame(force=True)
        self._update_title()
        width, height = self.engine.size
        self.status_shape.setText(
            f"{'Portrait' if orientation is Orientation.PORTRAIT else 'Landscape'} · "
            f"{width} × {height}")

    def _update_title(self) -> None:
        doc = self.document
        name = doc.directory.name if doc.directory else "untitled"
        dirty = not self.undo.isClean()
        self.setWindowTitle(f"{name}{' •' if dirty else ''} — akp02d Theme Editor")
        self.status_dirty.setText(
            f"<span style='color:{style.WARN}'>● Unsaved changes</span>" if dirty else "Saved")

    def _update_boxes(self) -> None:
        states: dict[str, str] = {}
        for problem in self.problems:
            if problem.widget is None:
                continue
            level = "error" if problem.level == ERROR else "warning"
            if states.get(problem.widget) != "error":
                states[problem.widget] = level
        boxes = [Box(slot.id, slot.plugin, slot.rect, states.get(slot.id, "ok"))
                 for slot in self.engine.slots if slot.rect is not None]
        self.canvas.set_boxes(boxes)
        self.canvas.select(self._id_of(self.selection) if isinstance(self.selection, Ref) else None)

    def _update_layers(self) -> None:
        self.layers.blockSignals(True)
        self.layers.clear()
        theme_item = QTreeWidgetItem(["Theme settings", self._theme_detail()])
        theme_item.setIcon(0, style.plugin_icon("theme"))
        theme_item.setData(0, Qt.ItemDataRole.UserRole, THEME)
        self.layers.addTopLevelItem(theme_item)
        bad = {p.widget for p in self.problems if p.widget and p.level == ERROR}
        warned = {p.widget for p in self.problems if p.widget and p.level != ERROR}
        for ref in self.document.file_order():
            widget_id = self.document.widget_id(ref)
            item = QTreeWidgetItem([widget_id, self._detail(ref)])
            item.setIcon(0, style.plugin_icon(ref.plugin))
            item.setData(0, Qt.ItemDataRole.UserRole, ref)
            item.setForeground(1, QColor(style.FAINT))
            font = item.font(0)
            font.setFamily("monospace")
            item.setFont(0, font)
            if widget_id in bad:
                item.setForeground(0, QColor(style.BAD))
            elif widget_id in warned:
                item.setForeground(0, QColor(style.WARN))
            self.layers.addTopLevelItem(item)
            if ref == self.selection:
                item.setSelected(True)
        if self.selection == THEME:
            theme_item.setSelected(True)
        self.layers.resizeColumnToContents(0)
        self.layers.blockSignals(False)

    def _theme_detail(self) -> str:
        theme = self.document.theme
        if theme.get("wallpaper"):
            return "wallpaper"
        background = theme.get("background", "")
        if isinstance(background, list):
            return "gradient" if len(background) > 1 else str(background[0] if background else "")
        return str(background)

    def _detail(self, ref: Ref) -> str:
        entry = self.document.entry(ref)
        parts = []
        if ref.plugin == "label" and not entry.get("source") and entry.get("format"):
            text = str(entry["format"])
            parts.append(f'"{text[:14]}…"' if len(text) > 15 else f'"{text}"')
        elif isinstance(entry.get("source"), str) and ref.plugin != "spectrum":
            parts.append(entry["source"])
        elif ref.plugin == "spectrum":
            parts.append(str(entry.get("type", "bars")))
        if "fps" in entry:
            parts.append(f"{controls_number(entry['fps'])} fps")
        return " · ".join(parts)

    def _update_inspector(self) -> None:
        # Deferred to the event loop: an edit usually arrives from inside
        # a field's focus-out, and Qt has not yet moved focus to the next
        # field. Rebuilding after it has lets the form put focus back
        # where Tab sent it. Several requests in one pass become one.
        self._inspector_timer.start(0)

    def _rebuild_inspector(self) -> None:
        count = len(self.engine.slots)
        if self.selection == THEME:
            self.inspector.show_theme(self.document, self.problems, count)
        elif isinstance(self.selection, Ref):
            slot = next((s for s in self.engine.slots if s.ref == self.selection), None)
            self.inspector.show_widget(
                self.document, self.selection, slot, self.problems, count,
                self.engine.orientation, self.config.device.inverted, self.engine.size)
        else:
            self.inspector.show_summary(self.document, self.problems, count)

    def _update_frame(self, force: bool = False) -> None:
        if self.engine.screen is None:
            return
        self.canvas.set_frame(to_qimage(self.engine.screen))

    def _tick(self) -> None:
        if self.engine.tick():
            self._update_frame()
        # A widget that failed mid-run is a new problem to show.
        if any(slot.error and slot.widget is None and slot.rect is not None
               and not any(p.widget == slot.id and p.level == ERROR for p in self.problems)
               for slot in self.engine.slots):
            self.problems = self.engine.problems()
            self._update_boxes()
            self._update_layers()

    # -- selection --

    def _id_of(self, ref: Ref) -> str:
        try:
            return self.document.widget_id(ref)
        except DocumentError:
            return ref.default_id

    def _set_selection(self, selection: Ref | str | None) -> None:
        if selection == self.selection:
            return
        self.selection = selection
        self.canvas.select(self._id_of(selection) if isinstance(selection, Ref) else None)
        self._update_layers()
        self._update_inspector()

    def _select_id(self, widget_id: str) -> None:
        ref = self.document.find(widget_id)
        if ref is not None:
            self._set_selection(ref)
            self.canvas.reveal(widget_id)

    def _layer_picked(self) -> None:
        items = self.layers.selectedItems()
        if not items:
            return
        value = items[0].data(0, Qt.ItemDataRole.UserRole)
        self._set_selection(value)
        if isinstance(value, Ref):
            self.canvas.reveal(self._id_of(value))

    def _canvas_picked(self, widget_id: str | None) -> None:
        self._set_selection(self.document.find(widget_id) if widget_id else None)

    # -- edits --

    def _widget_edited(self, ref: Ref, key: str, value: Any) -> None:
        widget_id = self._id_of(ref)
        changes: dict[str, Any] = {key: value}
        if key == "source":
            # A format written for the old source -- fields the new one
            # lacks, or fixed text that ignores it -- would show an error
            # or hide the reading, so it goes, and the new source's own
            # default applies. In the same undo step.
            old = self.document.entry(ref).get("format")
            new_cls = sources.lookup(value) if isinstance(value, str) else None
            if isinstance(old, str) and new_cls is not None:
                used = sources.format_fields(old)
                if not used or not used <= set(new_cls.fields):
                    changes["format"] = None
        self.apply(f"Change {widget_id} {key}",
                   lambda doc: doc.update(ref, changes), merge=f"{ref}.{key}")

    def _geometry_edited(self, ref: Ref, rect: Rect) -> None:
        self.apply(f"Move {self._id_of(ref)}",
                   lambda doc: doc.update(ref, dict(zip(("x", "y", "width", "height"), rect))))

    def _geometry_from_canvas(self, widget_id: str, rect: Rect) -> None:
        ref = self.document.find(widget_id)
        if ref is not None:
            self._geometry_edited(ref, rect)

    def edit_theme(self, key: str, value: Any, description: str) -> None:
        self.apply(description, lambda doc: doc.set_theme(key, value), merge=f"theme.{key}")

    def add_widget(self, plugin: str) -> None:
        width, height = DEFAULT_SIZES.get(plugin, (200, 100))
        rect = self._free_spot(width, height)
        if rect is None:
            QMessageBox.information(self, "No room",
                                    "There is no free space for a widget that size. "
                                    "Make room first, or shrink a widget.")
            return
        values: dict[str, Any] = dict(zip(("x", "y", "width", "height"), rect))
        cls = lookup(plugin)
        if cls is not None:
            for option in cls.schema:
                if option.kind == SOURCE and option.required:
                    names = [n for n in sources.available()
                             if (c := sources.lookup(n)) and sources.fits(c, option.accepts)]
                    if names:
                        values[option.key] = "cpu" if "cpu" in names else names[0]
        values.update(NEW_OPTIONS.get(plugin, {}))
        ref = self.apply(f"Add {plugin}", lambda doc: doc.add_widget(plugin, values))
        if ref is not None:
            self._set_selection(ref)
            self.canvas.reveal(self._id_of(ref))

    def _free_spot(self, width: int, height: int) -> Rect | None:
        """A colour-safe place for a new box, near the middle of the view."""
        screen_w, screen_h = self.engine.size
        width, height = min(width, screen_w), min(height, screen_h)
        taken = [slot.rect for slot in self.engine.slots if slot.rect is not None]
        view = self.canvas.visible_scene_rect()
        cx = int(min(max(view.center().x(), 0), screen_w))
        cy = int(min(max(view.center().y(), 0), screen_h))
        candidates = []
        for y in range(0, screen_h - height + 1, 8):
            for x in range(0, screen_w - width + 1, 8):
                candidates.append((abs(x + width / 2 - cx) + abs(y + height / 2 - cy), x, y))
        candidates.sort()
        for _, x, y in candidates:
            rect = snap(Rect(x, y, width, height), self.engine.orientation,
                        self.config.device.inverted, (screen_w, screen_h))
            if not any(rect.overlaps(other) for other in taken):
                return rect
        return None

    def delete_widget(self, widget_id: str) -> None:
        ref = self.document.find(widget_id)
        if ref is None:
            return
        self.selection = None
        self.apply(f"Delete {widget_id}", lambda doc: doc.remove_widget(ref))

    def duplicate_widget(self, widget_id: str) -> None:
        ref = self.document.find(widget_id)
        if ref is None:
            return
        slot = next((s for s in self.engine.slots if s.ref == ref), None)
        rect = slot.rect if slot and slot.rect else None
        if rect is None:
            return
        spot = self._free_spot(rect.width, rect.height)
        if spot is None:
            QMessageBox.information(self, "No room", "There is no free space for a copy.")
            return
        new = self.apply(f"Duplicate {widget_id}", lambda doc: doc.duplicate_widget(
            ref, dict(zip(("x", "y", "width", "height"), spot))))
        if new is not None:
            self._set_selection(new)
            self.canvas.reveal(self._id_of(new))

    def _zoom_changed(self) -> None:
        zoom = self.canvas.zoom
        mode = "fit" if self.canvas.fitted else "100" if abs(zoom - 1.0) < 1e-9 else ""
        self.zoom_seg.set_value(mode)
        self.zoom_label.setText(f"{round(zoom * 100)}%")
        self.zoom_label.setVisible(mode == "")

    def _hovered(self, point: tuple[int, int] | None) -> None:
        """Where the mouse is on the panel, and the colour there -- for
        lining things up, and for taking a colour off the wallpaper."""
        screen = self.engine.screen
        if point is None or screen is None:
            self.status_hover.clear()
            return
        x, y = point
        try:
            r, g, b = screen.getpixel((x, y))[:3]
        except IndexError:
            self.status_hover.clear()
            return
        colour = f"#{r:02x}{g:02x}{b:02x}"
        self.status_hover.setText(
            f"x {x}, y {y} &nbsp;&nbsp;<span style='background-color:{colour}'>"
            f"&nbsp;&nbsp;&nbsp;&nbsp;</span>&nbsp;{colour}")

    def _boxes_toggled(self, on: bool) -> None:
        self.canvas.show_boxes = on
        self.canvas.viewport().update()

    # -- files --

    def _confirm_discard(self) -> bool:
        if self.undo.isClean():
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save changes to this theme first?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def new_theme(self) -> None:
        if not self._confirm_discard():
            return
        self._replace(ThemeDocument.new(self.document.theme.get("orientation", "portrait")))

    def open_theme(self) -> None:
        if not self._confirm_discard():
            return
        start = str(self.document.directory or Path.home())
        name, _ = QFileDialog.getOpenFileName(self, "Open theme", start, "Theme (theme.toml)")
        if not name:
            return
        try:
            document = ThemeDocument.open(Path(name))
        except (OSError, DocumentError) as exc:
            QMessageBox.warning(self, "Cannot open", str(exc))
            return
        self._replace(document)

    def _replace(self, document: ThemeDocument) -> None:
        self.engine.close()
        self.engine = Engine(inverted=self.config.device.inverted)
        self.document = document
        self.selection = None
        self.undo.clear()
        self.refresh()
        QTimer.singleShot(0, self.canvas.fit)

    def save(self, force_choose: bool = False) -> bool:
        doc = self.document
        target = doc.path
        bundled = target is not None and _inside(target, PACKAGE_DIR)
        if target is None or force_choose or bundled:
            if bundled and not force_choose:
                QMessageBox.information(
                    self, "Save a copy",
                    "This theme ships inside akp02d, where an update would overwrite "
                    "it. Save it as your own theme instead; then set theme = \"<name>\" "
                    "in akp02d.toml.")
            suggested = Path.home() / ".config" / "akp02d" / "themes" / (
                (doc.directory.name if doc.directory else "my-theme") +
                ("-copy" if bundled else "")) / "theme.toml"
            name, _ = QFileDialog.getSaveFileName(self, "Save theme", str(suggested),
                                                  "Theme (theme.toml)")
            if not name:
                return False
            new = Path(name)
            if new.name != "theme.toml":
                new = new / "theme.toml" if not new.suffix else new.with_name("theme.toml")
            old_dir = doc.directory
            try:
                new.parent.mkdir(parents=True, exist_ok=True)
                if old_dir is not None and old_dir.resolve() != new.parent.resolve():
                    _copy_assets(doc, old_dir, new.parent)
                doc.save(new)
            except OSError as exc:
                QMessageBox.warning(self, "Cannot save", str(exc))
                return False
            self.engine._theme_key = None   # theme folder moved: rebuild
            self.refresh()
        else:
            try:
                doc.save()
            except OSError as exc:
                QMessageBox.warning(self, "Cannot save", str(exc))
                return False
        self.undo.setClean()
        self._update_title()
        self.statusBar().showMessage(f"Saved {doc.path}", 4000)
        return True

    def export_png(self) -> None:
        if self.engine.screen is None:
            return
        name = (self.document.directory.name if self.document.directory else "theme")
        start = str((self.document.directory or Path.home()) / f"{name}-preview.png")
        path, _ = QFileDialog.getSaveFileName(self, "Export PNG", start, "PNG image (*.png)")
        if not path:
            return
        self.engine.frame().save(path)
        self.statusBar().showMessage(f"Exported {path}", 4000)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._confirm_discard():
            event.ignore()
            return
        self.timer.stop()
        self._inspector_timer.stop()
        # Qt deletes the undo stack with the window, and clearing it on
        # the way out announces a change of "clean" state -- to handlers
        # that would ask the half-deleted stack about itself. Nothing is
        # listening for anything any more, so say nothing.
        self.undo.blockSignals(True)
        self.engine.close()
        event.accept()
        # Closing the editor ends it. Qt would otherwise keep running while
        # any other window it thinks is visible remains -- one a window
        # manager has kept a tile for, say -- and the tile with it.
        QApplication.instance().quit()


def controls_number(value: Any) -> str:
    from .controls import _number_text

    return _number_text(value)


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder)
    except ValueError:
        return False
    return True


def _copy_assets(doc: ThemeDocument, old: Path, new: Path) -> None:
    """Bring the files a theme names along when it is saved elsewhere."""
    import shutil

    names: set[str] = set()
    theme = doc.theme
    for key in ("wallpaper", "font"):
        if isinstance(theme.get(key), str):
            names.add(theme[key])
    for ref in doc.refs():
        font = doc.entry(ref).get("font")
        if isinstance(font, str):
            names.add(font)
    for name in names:
        source = old / name
        if source.is_file():
            target = new / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
