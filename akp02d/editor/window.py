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
from PySide6.QtCore import QSize, Qt, QTime, QTimer
from PySide6.QtGui import (
    QAction, QCloseEvent, QColor, QIcon, QImage, QKeySequence, QUndoCommand,
    QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QSlider, QSplitter, QTimeEdit,
    QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import akp02d
from akp02 import Orientation

from .. import config as config_module, sources
from ..layout import Rect
from ..plugins import available, lookup
from ..plugins import cava
from ..schema import SOURCE
from .canvas import Box, Canvas, Overview
from .controls import Segmented
from .document import DocumentError, Ref, ThemeDocument
from .engine import ERROR, Encoding, Engine, Problem, as_sent_image, snap
from .inspector import Inspector
from . import simulate, style

log = logging.getLogger(__name__)

THEME = "theme"   # selection sentinel: the [theme] table

DEFAULT_SIZES = {
    "label": (240, 48), "gauge": (160, 160), "graph": (320, 120), "spectrum": (320, 200),
}
NEW_OPTIONS: dict[str, dict[str, Any]] = {
    "label": {"format": "New label", "size": 32},
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


class FrozenDialog(QDialog):
    """Hand-set readings for Frozen data."""

    def __init__(self, settings: simulate.Settings, on_change: Callable[[], None],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Frozen values")
        self.settings = settings
        self.on_change = on_change
        form = QFormLayout(self)
        frozen = settings.frozen
        for key, label in (("cpu", "cpu"), ("memory", "memory"), ("disk", "disk")):
            form.addRow(label, self._percent(key, getattr(frozen, key)))
        form.addRow("network ↓", self._rate("down", frozen.down))
        form.addRow("network ↑", self._rate("up", frozen.up))
        clock = QTimeEdit(QTime(frozen.clock.hour, frozen.clock.minute, frozen.clock.second))
        clock.setDisplayFormat("HH:mm:ss")
        clock.timeChanged.connect(self._clock)
        form.addRow("time", clock)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        form.addRow(buttons)

    def _percent(self, key: str, value: float) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(round(value * 100))
        shown = QLabel(f"{round(value * 100)}%")
        shown.setMinimumWidth(40)

        def moved(v: int) -> None:
            setattr(self.settings.frozen, key, v / 100)
            shown.setText(f"{v}%")
            self.on_change()

        slider.valueChanged.connect(moved)
        row.addWidget(slider, 1)
        row.addWidget(shown)
        return holder

    def _rate(self, key: str, value: float) -> QWidget:
        import math

        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 80)   # 10^0 .. 10^8 bytes/s, in tenths of a decade
        slider.setValue(round(math.log10(max(value, 1)) * 10))
        shown = QLabel(_rate_text(value))
        shown.setMinimumWidth(80)

        def moved(v: int) -> None:
            rate = 10 ** (v / 10)
            setattr(self.settings.frozen, key, rate)
            shown.setText(_rate_text(rate))
            self.on_change()

        slider.valueChanged.connect(moved)
        row.addWidget(slider, 1)
        row.addWidget(shown)
        return holder

    def _clock(self, value: QTime) -> None:
        frozen = self.settings.frozen
        frozen.clock = frozen.clock.replace(hour=value.hour(), minute=value.minute(),
                                            second=value.second())
        self.on_change()


def _rate_text(rate: float) -> str:
    for unit, size in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
        if rate >= size:
            return f"{rate / size:.1f} {unit}"
    return f"{rate:.0f} B/s"


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
        self.settings = simulate.Settings()
        self.engine = Engine(
            inverted=cfg.device.inverted,
            encoding=Encoding(cfg.device.jpeg_quality, cfg.device.jpeg_subsampling),
            settings=self.settings,
        )
        self.undo = QUndoStack(self)
        self.undo.cleanChanged.connect(lambda _c: self._update_title())
        self.selection: Ref | str | None = None
        self.as_sent = False
        self.problems: list[Problem] = []
        self._last_sent = 0.0
        self._frame_bytes = 0
        self._frozen_dialog: FrozenDialog | None = None

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
        bar.setIconSize(QSize(18, 18))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(bar)
        self.toolbar = bar

        def action(name: str, icon: str, shortcut: Any, slot: Callable[[], None],
                   tip: str) -> QAction:
            act = QAction(QIcon.fromTheme(icon), name, self)
            if act.icon().isNull():
                bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            act.setShortcut(shortcut)
            act.setToolTip(f"{tip} ({QKeySequence(shortcut).toString()})" if shortcut else tip)
            act.triggered.connect(slot)
            bar.addAction(act)
            return act

        self.act_new = action("New", "document-new", QKeySequence.StandardKey.New,
                              self.new_theme, "New theme")
        self.act_open = action("Open", "document-open", QKeySequence.StandardKey.Open,
                               self.open_theme, "Open a theme")
        self.act_save = action("Save", "document-save", QKeySequence.StandardKey.Save,
                               self.save, "Save")
        # Fixed labels, with what they would undo in the tooltip: the
        # stack's own actions rename themselves ("Undo Add spectrum") and
        # push the rest of the toolbar around as they do.
        self.act_undo = action("Undo", "edit-undo", QKeySequence.StandardKey.Undo,
                               self.undo.undo, "Undo")
        self.act_redo = action("Redo", "edit-redo", QKeySequence.StandardKey.Redo,
                               self.undo.redo, "Redo")
        self.act_undo.setEnabled(False)
        self.act_redo.setEnabled(False)
        self.undo.canUndoChanged.connect(self.act_undo.setEnabled)
        self.undo.canRedoChanged.connect(self.act_redo.setEnabled)
        self.undo.undoTextChanged.connect(
            lambda text: self.act_undo.setToolTip(f"Undo {text}" if text else "Undo"))
        self.undo.redoTextChanged.connect(
            lambda text: self.act_redo.setToolTip(f"Redo {text}" if text else "Redo"))
        bar.addSeparator()

        def labelled(text: str, widget: QWidget) -> None:
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(6, 0, 6, 0)
            row.setSpacing(8)
            caption = QLabel(text)
            caption.setStyleSheet(f"color: {style.MUTED}; font-size: 12px;")
            row.addWidget(caption)
            row.addWidget(widget)
            bar.addWidget(holder)

        self.orientation_seg = Segmented((("portrait", "Portrait"), ("landscape", "Landscape")))
        self.orientation_seg.changed.connect(
            lambda v: self.edit_theme("orientation", v, "Change orientation"))
        labelled("Panel", self.orientation_seg)

        self.data_seg = Segmented(((simulate.LIVE, "Live"), (simulate.SIMULATED, "Simulated"),
                                   (simulate.FROZEN, "Frozen")))
        self.data_seg.set_value(self.settings.data)
        self.data_seg.changed.connect(self._data_mode)
        self.frozen_button = QToolButton()
        self.frozen_button.setText("Set…")
        self.frozen_button.setToolTip("Set the frozen readings")
        self.frozen_button.clicked.connect(self._open_frozen)
        self.frozen_button.setEnabled(False)
        data_holder = QWidget()
        data_row = QHBoxLayout(data_holder)
        data_row.setContentsMargins(0, 0, 0, 0)
        data_row.setSpacing(6)
        data_row.addWidget(self.data_seg)
        data_row.addWidget(self.frozen_button)
        labelled("Data", data_holder)

        self.audio_combo = QComboBox()
        for key, text in ((simulate.MUSIC, "Music-like"), (simulate.SWEEP, "Sweep"),
                          (simulate.SILENCE, "Silence"), (simulate.CAVA, "cava (real)")):
            self.audio_combo.addItem(text, key)
        if not cava.installed():
            model = self.audio_combo.model()
            model.item(3).setEnabled(False)
            self.audio_combo.setItemData(3, "cava is not installed", Qt.ItemDataRole.ToolTipRole)
        self.audio_combo.activated.connect(self._audio_mode)
        labelled("Audio", self.audio_combo)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        export = QPushButton("Export PNG")
        export.clicked.connect(self.export_png)
        bar.addWidget(export)
        self.panel_button = QPushButton("Show on panel")
        self.panel_button.setObjectName("primary")
        self.panel_button.setToolTip(
            "Send the current frame to the panel once, without saving. If the "
            "daemon is running, just save instead: it reloads the theme.")
        self.panel_button.clicked.connect(self.show_on_panel)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(8, 0, 0, 0)
        row.addWidget(self.panel_button)
        bar.addWidget(holder)

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
        heading = QLabel("THEME")
        heading.setObjectName("heading")
        heading.setContentsMargins(14, 0, 14, 0)
        column.addWidget(heading)
        self.theme_name = QLabel()
        self.theme_name.setObjectName("title")
        self.theme_name.setContentsMargins(14, 4, 14, 0)
        column.addWidget(self.theme_name)
        self.theme_path = QLabel()
        self.theme_path.setObjectName("path")
        self.theme_path.setContentsMargins(14, 2, 14, 10)
        self.theme_path.setWordWrap(True)
        column.addWidget(self.theme_path)
        heading = QLabel("LAYERS")
        heading.setObjectName("heading")
        heading.setContentsMargins(14, 6, 14, 6)
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

        # Middle: overview and canvas.
        middle = QSplitter(Qt.Orientation.Horizontal)
        middle.setHandleWidth(1)
        self.middle = middle
        self.canvas = Canvas()
        self.overview = Overview(self.canvas)
        self.overview.setMinimumWidth(150)
        canvas_column = QWidget()
        canvas_layout = QVBoxLayout(canvas_column)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)
        strip = QFrame()
        strip.setStyleSheet(f"QFrame {{ background: {style.CHROME}; border-bottom: 1px solid #2b303a; }}")
        strip_row = QHBoxLayout(strip)
        strip_row.setContentsMargins(10, 4, 10, 4)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("mono")
        strip_row.addWidget(QLabel("Canvas"))
        strip_row.addWidget(self.zoom_label)
        for text, slot in (("Fit", self.canvas.fit), ("1:1", lambda: self.canvas.set_zoom(1.0))):
            button = QToolButton()
            button.setText(text)
            button.setObjectName("flat")
            button.clicked.connect(slot)
            strip_row.addWidget(button)
        strip_row.addSpacing(12)
        self.preview_seg = Segmented((("exact", "Exact"), ("sent", "As sent")))
        self.preview_seg.set_value("exact")
        self.preview_seg.setToolTip(
            "As sent: the frame after the JPEG encode the panel receives, at the "
            "quality and subsampling set in akp02d.toml")
        self.preview_seg.changed.connect(self._preview_mode)
        strip_row.addWidget(self.preview_seg)
        self.quality_label = QLabel(self._encoding_text())
        self.quality_label.setObjectName("hint")
        self.quality_label.setToolTip("JPEG quality and chroma subsampling from akp02d.toml")
        strip_row.addWidget(self.quality_label)
        strip_row.addStretch(1)
        self.snap_box = QCheckBox("Snap")
        self.snap_box.setChecked(True)
        self.snap_box.setToolTip("Snap to colour-safe offsets: keep widgets where the panel "
                                 "renders them in true colour")
        self.snap_box.toggled.connect(self._snap_toggled)
        strip_row.addWidget(self.snap_box)
        self.boxes_box = QCheckBox("Outlines")
        self.boxes_box.setChecked(True)
        self.boxes_box.toggled.connect(self._boxes_toggled)
        strip_row.addWidget(self.boxes_box)
        canvas_layout.addWidget(strip)
        canvas_layout.addWidget(self.canvas, 1)
        middle.addWidget(self.overview)
        middle.addWidget(canvas_column)
        middle.setStretchFactor(0, 0)
        middle.setStretchFactor(1, 1)
        middle.setSizes([200, 700])
        splitter.addWidget(middle)

        self.canvas.selected.connect(self._canvas_picked)
        self.canvas.geometry_committed.connect(self._geometry_from_canvas)
        self.canvas.delete_requested.connect(self.delete_widget)
        self.canvas.duplicate_requested.connect(self.duplicate_widget)
        self.canvas.status.connect(lambda text: self.statusBar().showMessage(text, 4000))
        self.canvas.view_changed.connect(
            lambda: self.zoom_label.setText(f"{round(self.canvas.zoom * 100)}%"))

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
        self.status_audio = QLabel()
        self.status_bytes = QLabel()
        self.status_dirty = QLabel()
        for label in (self.status_shape, self.status_mount, self.status_audio):
            bar.addWidget(label)
        bar.addPermanentWidget(self.status_bytes)
        bar.addPermanentWidget(self.status_dirty)

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
        # The overview sits beside a tall panel and above a wide one.
        wanted = (Qt.Orientation.Horizontal if orientation is Orientation.PORTRAIT
                  else Qt.Orientation.Vertical)
        if self.middle.orientation() != wanted:
            self.middle.setOrientation(wanted)
            total = sum(self.middle.sizes()) or 800
            first = 200 if wanted == Qt.Orientation.Horizontal else 130
            self.middle.setSizes([first, max(total - first, 100)])
        self.canvas.set_rules(orientation, self.config.device.inverted)
        self.orientation_seg.set_value(
            "portrait" if orientation is Orientation.PORTRAIT else "landscape")
        self._update_boxes()
        self._update_layers()
        self._update_inspector()
        self._update_frame(force=True)
        self._update_title()
        width, height = self.engine.size
        self.status_shape.setText(
            f"{'Portrait' if orientation is Orientation.PORTRAIT else 'Landscape'} · "
            f"{width} × {height}")
        self.status_audio.setText(self._audio_text())

    def _update_title(self) -> None:
        doc = self.document
        name = doc.directory.name if doc.directory else "untitled"
        dirty = not self.undo.isClean()
        self.setWindowTitle(f"{name}{' •' if dirty else ''} — akp02d Theme Editor")
        self.theme_name.setText(name)
        self.theme_path.setText(str(doc.path) if doc.path else "not saved yet")
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
        return "wallpaper" if theme.get("wallpaper") else str(theme.get("background", ""))

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
        if self.as_sent:
            now = time.monotonic()
            if not force and now - self._last_sent < 0.1:
                return
            self._last_sent = now
            image, size = as_sent_image(self.engine.screen, self.engine.orientation,
                                        self.config.device.inverted, self.engine.encoding)
            self._frame_bytes = size
        else:
            image = self.engine.screen
        qimage = to_qimage(image)
        self.canvas.set_frame(qimage)
        self.overview.set_frame(qimage)
        if force or self._frame_bytes == 0:
            _, self._frame_bytes = as_sent_image(
                self.engine.screen, self.engine.orientation, self.config.device.inverted,
                self.engine.encoding)
        self.status_bytes.setText(
            f"Full frame ≈ {self._frame_bytes / 1024:.0f} KiB as JPEG {self._encoding_text()}")

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
        self.overview.update()

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
        self.apply(f"Change {widget_id} {key}",
                   lambda doc: doc.set(ref, key, value), merge=f"{ref}.{key}")

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
                             if (c := sources.lookup(n)) and c.kind in option.accepts]
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

    # -- preview modes --

    def _data_mode(self, mode: str) -> None:
        self.settings.data = mode
        self.engine.reseed()
        self.frozen_button.setEnabled(mode == simulate.FROZEN)
        if mode == simulate.FROZEN:
            self._open_frozen()
        self.status_audio.setText(self._audio_text())

    def _open_frozen(self) -> None:
        if self._frozen_dialog is None:
            self._frozen_dialog = FrozenDialog(self.settings, lambda: None, self)
        self._frozen_dialog.show()
        self._frozen_dialog.raise_()

    def _audio_mode(self, index: int) -> None:
        mode = self.audio_combo.itemData(index)
        was_cava = self.settings.audio == simulate.CAVA
        self.settings.audio = mode
        if was_cava != (mode == simulate.CAVA):
            self.refresh()
        self.status_audio.setText(self._audio_text())

    def _audio_text(self) -> str:
        has_spectrum = any(slot.plugin == "spectrum" for slot in self.engine.slots)
        data = {"live": "live data", "simulated": "simulated data",
                "frozen": "frozen data"}[self.settings.data]
        if not has_spectrum:
            return f"Sources: {data}"
        audio = {"music": "simulated music", "sweep": "frequency sweep",
                 "silence": "silence", "cava": "cava"}[self.settings.audio]
        return f"Sources: {data} · Spectrum: {audio}"

    def _preview_mode(self, mode: str) -> None:
        self.as_sent = mode == "sent"
        self._update_frame(force=True)

    def _encoding_text(self) -> str:
        names = {-1: "auto", 0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}
        enc = self.engine.encoding
        return f"q{enc.quality} · {names.get(enc.subsampling, enc.subsampling)}"

    def _snap_toggled(self, on: bool) -> None:
        self.canvas.snapping = on

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
        self.engine = Engine(inverted=self.config.device.inverted,
                             encoding=self.engine.encoding, settings=self.settings)
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
        self.engine.frame(as_sent=self.as_sent).save(path)
        self.statusBar().showMessage(f"Exported {path}", 4000)

    def show_on_panel(self) -> None:
        """Send this frame to the panel once, without saving anything."""
        from akp02 import AKP02, DeviceNotFoundError

        if self.engine.screen is None:
            return
        frame = self.engine.frame()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            device = self.config.device
            panel = AKP02(jpeg_quality=device.jpeg_quality,
                          jpeg_subsampling=device.jpeg_subsampling,
                          keepalive_interval=None)
            with panel:
                if self.engine.orientation is not Orientation.LANDSCAPE:
                    panel.orientation(self.engine.orientation)
                panel.inverted = device.inverted
                panel.set_brightness(device.brightness)
                panel.screen_on()
                panel.show(frame)
        except (DeviceNotFoundError, ImportError) as exc:
            QMessageBox.information(self, "Panel not found", f"{exc}")
        except OSError as exc:
            QMessageBox.warning(
                self, "Panel busy",
                f"Could not write to the panel: {exc}\n\nIf akp02d is running, it has "
                f"the panel; save the theme instead and it will reload.")
        else:
            self.statusBar().showMessage("Frame sent to the panel", 4000)
        finally:
            QApplication.restoreOverrideCursor()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._confirm_discard():
            event.ignore()
            return
        self.timer.stop()
        self.engine.close()
        event.accept()


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
