"""The editor window: layers, canvas, settings, and the loop that ticks.

Every change goes through one path: an edit to the ThemeDocument's text,
recorded as an undo step, followed by `refresh()`, which brings the
preview engine, the canvas, the layers list and the settings panel in
line with the document. The document is the only state; everything on
screen is derived from it, so undo is just "put the old text back".
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
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
    QMenu, QMessageBox, QPushButton, QSizePolicy, QSplitter, QToolBar, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import akp02d
from akp02 import Orientation

from .. import config as config_module, control, paths, sources
from ..layout import Rect
from ..plugins import available, lookup
from ..schema import SOURCE
from .canvas import Box, Canvas
from .controls import Segmented
from .document import DocumentError, Ref, ThemeDocument
from .engine import ERROR, Engine, Problem, snap
from .inspector import Inspector
from .themes import ThemeList, ask_name, heading_button
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
# Written beside theme.toml on every save; see _write_preview.
PREVIEW = "preview.png"


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
        # What the running daemon shows (None: not running), for the list.
        self.panel_theme: str | None = None

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
        self.theme_list.set_open(document.directory)
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
        # Apply: the running daemon shows this theme, and starts with it
        # from now on -- what is on the panel is what comes back.
        apply = QPushButton("Apply to panel")
        apply.setToolTip("Show this theme on the panel, and start with it from now on "
                         "(saves it first)")
        apply.clicked.connect(lambda _c=False: self.apply_to_panel())
        apply.setObjectName("apply")
        bar.addWidget(apply)
        self.apply_button = apply

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
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        # Themes above layers, the divider between them draggable; the
        # list starts short, since the layers are where the work is.
        stack = QSplitter(Qt.Orientation.Vertical, left)
        stack.setHandleWidth(1)
        stack.setChildrenCollapsible(False)
        self.theme_list = ThemeList()
        self.theme_list.edit.connect(self.edit_named)
        self.theme_list.duplicate.connect(self.duplicate_named)
        self.theme_list.rename.connect(self.rename_named)
        self.theme_list.delete.connect(self.delete_named)
        stack.addWidget(self.theme_list)
        layers_box = QWidget()
        layers_column = QVBoxLayout(layers_box)
        layers_column.setContentsMargins(0, 12, 0, 0)
        layers_column.setSpacing(0)
        # The heading, with duplicate and delete for the selected widget.
        layers_top = QHBoxLayout()
        layers_top.setContentsMargins(14, 0, 8, 4)
        layers_top.setSpacing(2)
        heading = QLabel("LAYERS")
        heading.setObjectName("heading")
        layers_top.addWidget(heading)
        layers_top.addStretch(1)
        self.layer_buttons: dict[str, QToolButton] = {}
        for key, tip, act in (
            ("duplicate", "Duplicate this widget (Ctrl+J)",
             lambda: self.duplicate_widget(self._id_of(self.selection))),
            ("delete", "Delete this widget (Delete; Ctrl+Z brings it back)",
             lambda: self.delete_widget(self._id_of(self.selection))),
        ):
            button = heading_button(key, tip)
            button.setObjectName(f"layer.{key}")
            button.clicked.connect(lambda _c=False, a=act: isinstance(self.selection, Ref) and a())
            button.setEnabled(False)
            layers_top.addWidget(button)
            self.layer_buttons[key] = button
        layers_column.addLayout(layers_top)
        self.layers = QTreeWidget()
        self.layers.setHeaderHidden(True)
        self.layers.setColumnCount(2)
        self.layers.setRootIsDecorated(False)
        self.layers.setIndentation(6)
        self.layers.setIconSize(QSize(18, 18))
        self.layers.setStyleSheet(
            f"QTreeWidget {{ background: {style.PANEL}; border: none; outline: none; }}"
            f"QTreeWidget::item {{ height: 30px; }}"
            f"QTreeWidget::item:selected {{ background: #24425a; color: {style.TEXT}; }}")
        self.layers.itemSelectionChanged.connect(self._layer_picked)
        layers_column.addWidget(self.layers, 1)
        stack.addWidget(layers_box)
        stack.setSizes([210, 400])
        column.addWidget(stack, 1)
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
        left.setMinimumWidth(250)
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
        splitter.setSizes([280, 790, 360])

    def _build_status(self) -> None:
        bar = self.statusBar()
        self.status_shape = QLabel()
        self.status_mount = QLabel("Mount: inverted" if self.config.device.inverted
                                   else "Mount: upright")
        self.status_dirty = QLabel()
        # What the running daemon is showing, asked every few seconds.
        self.status_panel = QLabel()
        self.status_panel.setToolTip("The akp02d daemon, through its control socket")
        for label in (self.status_panel, self.status_shape, self.status_mount,
                      self.status_dirty):
            bar.addPermanentWidget(label)
        self._panel_timer = QTimer(self)
        self._panel_timer.timeout.connect(self.refresh_panel_status)
        self._panel_timer.start(3000)
        QTimer.singleShot(0, self.refresh_panel_status)

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
        theme_item.setFont(0, style.list_font())
        theme_item.setFont(1, style.list_font(style.LIST_DETAIL_PX))
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
            item.setFont(0, style.list_font(mono=True))
            item.setFont(1, style.list_font(style.LIST_DETAIL_PX))
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
        widget_selected = isinstance(self.selection, Ref)
        for button in self.layer_buttons.values():
            button.setEnabled(widget_selected)

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
        # Keys an older theme used for what this option now says go with
        # it, in the same step (spectrum's mono_option, with channels).
        schema = getattr(lookup(ref.plugin), "schema", None)
        if schema is not None and key in schema:
            entry = self.document.entry(ref)
            for old_key in schema[key].replaces:
                if old_key in entry:
                    changes[old_key] = None
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
        """A new theme: named first, and made in the themes folder."""
        if not self._confirm_discard():
            return
        name = ask_name(self, "New theme", "Name the new theme:")
        if name is None:
            return
        folder = _themes_folder() / name
        document = ThemeDocument.new(self.document.theme.get("orientation", "portrait"),
                                     path=folder / "theme.toml")
        try:
            folder.mkdir(parents=True)
            document.save()
        except OSError as exc:
            QMessageBox.warning(self, "Cannot create", str(exc))
            return
        self._replace(document)
        self._write_preview()
        self.theme_list.reload()

    def _replace(self, document: ThemeDocument) -> None:
        self.engine.close()
        self.engine = Engine(inverted=self.config.device.inverted)
        self.document = document
        self.selection = None
        self.undo.clear()
        self.refresh()
        self.theme_list.set_open(document.directory)
        QTimer.singleShot(0, self.canvas.fit)

    def save(self, force_choose: bool = False) -> bool:
        """Save the theme where it is. A theme with no home yet -- a new
        one, or one that ships inside akp02d -- asks only for a name and
        goes into the themes folder; so does Save As, as a copy."""
        doc = self.document
        target = doc.path
        bundled = target is not None and _inside(target, PACKAGE_DIR)
        if target is None or force_choose or bundled:
            if bundled and not force_choose:
                label = ("This theme ships inside akp02d, where an update would overwrite "
                         "it. It will be saved as your own theme.\n\nName it:")
            elif force_choose:
                label = "Save a copy of this theme as:"
            else:
                label = "Name the theme:"
            suggested = (doc.directory.name + "-copy") if doc.directory else ""
            name = ask_name(self, "Save theme", label, suggested)
            if name is None:
                return False
            new = _themes_folder() / name / "theme.toml"
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
            self.theme_list.reload()
            self.theme_list.set_open(doc.directory)
        else:
            try:
                doc.save()
            except OSError as exc:
                QMessageBox.warning(self, "Cannot save", str(exc))
                return False
        self.undo.setClean()
        self._update_title()
        self._write_preview()
        self.statusBar().showMessage(f"Saved {doc.path}", 4000)
        return True

    def _write_preview(self) -> None:
        """preview.png beside theme.toml: the preview as it stands, at the
        panel's size, with its simulated readings. The daemon never reads
        it. A failure to write it is said, but does not undo the save."""
        folder = self.document.directory
        if folder is None or self.engine.screen is None:
            return
        # Anything rebuilt since the last frame draws first.
        self.engine.tick()
        try:
            self.engine.frame().save(folder / PREVIEW)
        except OSError as exc:
            self.statusBar().showMessage(f"Saved, but no preview: {exc}", 6000)

    # -- the themes list --

    def edit_named(self, name: str) -> None:
        """Open a theme from the list (one click). Kept unsaved changes
        -- Cancel -- leave the open theme selected, as it still is."""
        folder = _themes_folder() / name
        if self.document.directory is not None and _same(self.document.directory, folder):
            return
        if not self._confirm_discard():
            self.theme_list.show_open()
            return
        try:
            document = ThemeDocument.open(folder / "theme.toml")
        except (OSError, DocumentError) as exc:
            QMessageBox.warning(self, "Cannot open", str(exc))
            self.theme_list.show_open()
            return
        self._replace(document)

    def duplicate_named(self, name: str) -> None:
        if not self._confirm_discard():
            return
        copy = ask_name(self, "Duplicate theme", f"Name the copy of {name}:", f"{name}-copy")
        if copy is None:
            return
        try:
            shutil.copytree(_themes_folder() / name, _themes_folder() / copy)
            document = ThemeDocument.open(_themes_folder() / copy / "theme.toml")
        except (OSError, DocumentError) as exc:
            QMessageBox.warning(self, "Cannot duplicate", str(exc))
            return
        self._replace(document)
        self.theme_list.reload()
        self.statusBar().showMessage(f"Duplicated {name} as {copy}", 4000)

    def rename_named(self, name: str) -> None:
        if name == self.panel_theme:
            QMessageBox.information(self, "On the panel",
                                    f"{name} is on the panel. Switch the panel to another "
                                    f"theme first, then rename it.")
            return
        folder = _themes_folder() / name
        is_open = self.document.directory is not None and _same(self.document.directory, folder)
        if is_open and not self.undo.isClean() and not self.save():
            return
        new = ask_name(self, "Rename theme", f"New name for {name}:", name)
        if new is None:
            return
        try:
            folder.rename(_themes_folder() / new)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot rename", str(exc))
            return
        if self.config.theme == name:
            # akp02d starts with it: keep it starting with it.
            try:
                config_module.save_theme(new, self.config.source)
                self.config = dataclasses.replace(self.config, theme=new)
            except OSError as exc:
                QMessageBox.warning(self, "Renamed, but", f"akp02d.toml still names "
                                    f"{name!r}: {exc}")
        if is_open:
            self._replace(ThemeDocument.open(_themes_folder() / new / "theme.toml"))
        self.theme_list.reload()
        self.statusBar().showMessage(f"Renamed {name} to {new}", 4000)

    def delete_named(self, name: str) -> None:
        if name == self.panel_theme:
            QMessageBox.information(self, "On the panel",
                                    f"{name} is on the panel. Switch the panel to another "
                                    f"theme first, then delete it.")
            return
        if name == self.config.theme:
            QMessageBox.information(self, "akp02d starts with it",
                                    f"akp02d.toml starts the panel with {name}. Make "
                                    f"another theme the one it starts with first "
                                    f"(Apply and start with it).")
            return
        answer = QMessageBox.question(
            self, "Delete theme", f"Move {name} to the trash?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        folder = _themes_folder() / name
        is_open = self.document.directory is not None and _same(self.document.directory, folder)
        try:
            paths.trash(folder)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot delete",
                                f"{folder} could not be moved to the trash: {exc}")
            return
        if is_open:
            # Nothing to edit any more: the first theme left, or a new one.
            left = paths.theme_names()
            if left:
                self._replace(ThemeDocument.open(_themes_folder() / left[0] / "theme.toml"))
            else:
                self._replace(ThemeDocument.new())
        self.theme_list.reload()
        self.statusBar().showMessage(f"Moved {name} to the trash", 4000)

    # -- the running daemon --

    def refresh_panel_status(self) -> None:
        try:
            reply = control.request({"cmd": "status"}, timeout=0.5)
        except control.ControlError:
            self.status_panel.setText("Panel: not running")
            self.status_panel.setStyleSheet(f"color: {style.FAINT};")
            self.panel_theme = None
            self.theme_list.set_panel(None)
            return
        self.panel_theme = reply.get("theme")
        self.status_panel.setText(f"Panel: {self.panel_theme or '?'}")
        self.status_panel.setStyleSheet(f"color: {style.ON_PANEL};")
        # By name only for a theme from the themes folder.
        in_folder = _inside(Path(reply.get("path", "")), _themes_folder())
        self.theme_list.set_panel(self.panel_theme if in_folder else None)

    def apply_to_panel(self, save: bool = True) -> bool:
        """Show this theme on the panel: saved first, then the daemon is
        asked to switch. A theme with a mistake is refused by the daemon,
        which keeps showing what it was; the reason comes back here."""
        try:
            control.request({"cmd": "status"}, timeout=0.5)
        except control.ControlError:
            QMessageBox.information(
                self, "Panel not running",
                "akp02d is not running, so there is nothing to apply the theme to.\n\n"
                "Start it (for example systemctl --user start akp02d), then try again.")
            return False
        if self.document.path is None or not self.undo.isClean():
            if not self.save():
                return False
        folder = self.document.directory
        assert folder is not None
        # By name when it lives in the themes folder -- the name is what
        # akp02d.toml would hold -- else by its path.
        name = folder.name if _inside(folder, _themes_folder()) else str(folder)
        return self._send_theme(name, save)

    def _send_theme(self, name: str, save: bool) -> bool:
        try:
            reply = control.request({"cmd": "theme", "name": name, "save": save})
        except control.ControlError as exc:
            QMessageBox.warning(self, "Panel", str(exc))
            return False
        if not reply.get("ok"):
            QMessageBox.warning(self, "Panel kept its theme", reply.get("error", "refused"))
            return False
        note = " (akp02d will start with it)" if reply.get("saved") else ""
        if reply.get("saved"):
            self.config = dataclasses.replace(self.config, theme=name)
        self.statusBar().showMessage(f"Showing {reply.get('theme')} on the panel{note}", 5000)
        self.refresh_panel_status()
        return True

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


def _themes_folder() -> Path:
    return paths.themes_dir()


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
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
