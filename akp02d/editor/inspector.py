"""The settings panel: a form built from the selected plugin's schema.

Nothing here knows what a gauge is. The rows come from the plugin's
declared options, then its source's (a disk gauge gains `path`), then
the background options every widget has; options whose `when` says
they do nothing right now are left out, and come back when they
matter. A plugin added later gets a form with no change here.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QToolButton,
    QVBoxLayout, QWidget,
)

from akp02 import AKP02

from .. import layout, sources
from ..layout import Rect
from ..plugins import BASE, lookup
from ..schema import (
    BOOLEAN, CHOICE, COLOR, COLORS, FONT, FORMAT, IMAGE, INTEGER, NUMBER, SECTIONS,
    SOURCE, STRING, Auto, Option, Schema, Values,
)
from ..table import REQUIRED
from . import controls, style
from .document import Ref, ThemeDocument
from .engine import ERROR, Problem, Slot, snap

RESERVED = ("id", "x", "y", "width", "height", "fps")


class _SpecLike:
    """What an Auto default may ask of a spec: its box, id and plugin."""

    def __init__(self, ref: Ref, entry: dict[str, Any], slot: Slot | None) -> None:
        self.id = slot.id if slot else ref.default_id
        self.plugin = ref.plugin
        rect = slot.rect if slot and slot.rect else None
        self.rect = rect or Rect(0, 0, int(entry.get("width", 0) or 0),
                                 int(entry.get("height", 0) or 0))


def options_for(plugin: str, entry: dict[str, Any]) -> list[Option]:
    """Every option a widget's form shows, in order."""
    cls = lookup(plugin)
    own = list(cls.schema) if cls is not None else []
    out: list[Option] = []
    for option in own:
        out.append(option)
        if option.kind == SOURCE:
            source_cls = sources.lookup(entry.get(option.key, "")) if isinstance(
                entry.get(option.key), str) else None
            if source_cls is not None:
                out += list(source_cls.schema)
    return out + list(BASE)


def default_label(option: Option, values: Values, spec: Any) -> str:
    if isinstance(option.default, Auto):
        try:
            computed = option.default.compute(values, spec)
        except Exception:  # noqa: BLE001
            return "auto"
        shown = controls._number_text(computed) if isinstance(computed, (int, float)) \
            else str(computed)
        return f"auto: {shown}"
    if option.default is REQUIRED:
        return "required"
    if option.default is None:
        return "none"
    if isinstance(option.default, (int, float)) and not isinstance(option.default, bool):
        return controls._number_text(option.default)
    return str(option.default)


IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"


def pick_image(parent: QWidget, document: ThemeDocument, title: str) -> str | None:
    """Ask for a picture; return its name relative to the theme's folder.

    A picture from elsewhere is copied in beside theme.toml, so the theme
    stays self-contained -- it can be moved or shared as one folder.
    """
    folder = document.directory
    if folder is None:
        QMessageBox.information(parent, "Save first",
                                "Save the theme to a folder first; pictures are "
                                "stored beside theme.toml.")
        return None
    name, _ = QFileDialog.getOpenFileName(parent, title, str(folder), IMAGE_FILTER)
    if not name:
        return None
    chosen = Path(name)
    try:
        relative = chosen.resolve().relative_to(folder.resolve())
    except ValueError:
        target = folder / chosen.name
        if target.exists() and target.resolve() != chosen.resolve():
            answer = QMessageBox.question(
                parent, "Replace file?",
                f"{target.name} already exists in the theme folder. Replace it?")
            if answer != QMessageBox.StandardButton.Yes:
                return None
        shutil.copy2(chosen, target)
        relative = Path(chosen.name)
    return relative.as_posix()


class Inspector(QWidget):
    widget_edited = Signal(object, str, object)   # Ref, key, value (None removes)
    geometry_edited = Signal(object, object)      # Ref, Rect
    theme_edited = Signal(str, object)
    problem_clicked = Signal(object)              # widget id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("side")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll, 1)
        self.footer = QFrame(self)
        self.footer.setObjectName("footer")
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(16, 10, 16, 10)
        self.footer_label = QLabel(self.footer)
        self.footer_label.setWordWrap(True)
        footer_layout.addWidget(self.footer_label)
        outer.addWidget(self.footer)
        self._samples: list[tuple[QLabel, Slot, str]] = []
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_samples)
        self._timer.start(500)
        self._context: tuple = ()

    # -- building --

    def _reset(self) -> tuple[QWidget, QVBoxLayout]:
        # Remember which field has focus -- by the name of the control it
        # belongs to -- so Tab still lands in the next field after the
        # form is rebuilt around the value just committed.
        self._focus_key = ""
        focused = self.focusWidget()
        while focused is not None and focused is not self:
            name = focused.objectName()
            if name.startswith(("opt.", "geometry.", "theme.")):
                self._focus_key = name
                break
            focused = focused.parentWidget()
        self._scroll_value = self.scroll.verticalScrollBar().value()
        body = QWidget()
        column = QVBoxLayout(body)
        column.setContentsMargins(16, 12, 16, 16)
        column.setSpacing(10)
        self._samples = []
        return body, column

    def _finish(self, body: QWidget, column: QVBoxLayout) -> None:
        column.addStretch(1)
        # setWidget() would delete the old form at once -- and the edit
        # that triggered this rebuild is usually still inside one of its
        # fields' event handlers (focus-out, Return), so that is a crash.
        # Detach it and let the event loop delete it afterwards.
        old = self.scroll.takeWidget()
        if old is not None:
            old.hide()
            old.deleteLater()
        self.scroll.setWidget(body)
        self.scroll.verticalScrollBar().setValue(getattr(self, "_scroll_value", 0))
        key = getattr(self, "_focus_key", "")
        if key:
            target = body.findChild(QWidget, key)
            if target is not None:
                QTimer.singleShot(0, target.setFocus)

    def _problem_boxes(self, column: QVBoxLayout, problems: list[Problem],
                       clickable: bool = False) -> None:
        for problem in problems:
            frame = QFrame()
            frame.setObjectName("problem" if problem.level == ERROR else "warning")
            row = QHBoxLayout(frame)
            row.setContentsMargins(10, 8, 10, 8)
            text = problem.message
            if clickable and problem.widget:
                text = f"<b>{problem.widget}</b> — {text}"
            label = QLabel(text)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.RichText if clickable else Qt.TextFormat.PlainText)
            row.addWidget(label, 1)
            if clickable and problem.widget:
                go = QToolButton()
                go.setText("Show")
                go.clicked.connect(lambda _c=False, w=problem.widget: self.problem_clicked.emit(w))
                row.addWidget(go)
            column.addWidget(frame)

    def _set_footer(self, problems: list[Problem], count: int) -> None:
        errors = [p for p in problems if p.level == ERROR]
        warnings = [p for p in problems if p.level != ERROR]
        if errors:
            self.footer_label.setText(
                f"<span style='color:{style.BAD}'>●</span> {len(errors)} problem"
                f"{'s' if len(errors) != 1 else ''} the daemon would refuse"
                + (f" · {len(warnings)} warning{'s' if len(warnings) != 1 else ''}"
                   if warnings else ""))
        else:
            self.footer_label.setText(
                f"<span style='color:{style.OK}'>✓</span> Theme valid · {count} widget"
                f"{'s' if count != 1 else ''}"
                + (f" · <span style='color:{style.WARN}'>{len(warnings)} warning"
                   f"{'s' if len(warnings) != 1 else ''}</span>" if warnings else ""))

    # -- nothing selected --

    def show_summary(self, document: ThemeDocument, problems: list[Problem],
                     count: int) -> None:
        body, column = self._reset()
        hint = QLabel("Select a widget on the canvas or in the layers list to edit it. "
                      "Drag to move, drag a handle to resize; arrow keys nudge, "
                      "Delete removes. Ctrl+wheel zooms.\n\n"
                      "Readings in the preview are simulated -- CPU, memory, disk and "
                      "network move, and spectrums play made-up music -- so every "
                      "widget looks alive on any machine. The clock is real, and so "
                      "is a temperature sensor this machine has.\n\n"
                      "Hover over the panel to see the pixel position and colour in "
                      "the status bar.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        column.addWidget(hint)
        if problems:
            heading = QLabel("Problems")
            heading.setObjectName("heading")
            column.addWidget(heading)
            self._problem_boxes(column, problems, clickable=True)
        self._finish(body, column)
        self._set_footer(problems, count)

    # -- theme settings --

    def show_theme(self, document: ThemeDocument, problems: list[Problem],
                   count: int) -> None:
        body, column = self._reset()
        self._problem_boxes(column, [p for p in problems if p.widget is None])
        theme = document.theme
        form = self._group(column, "Panel")
        orientation = controls.ChoiceField(("portrait", "landscape"))
        orientation.setObjectName("theme.orientation")
        orientation.set_value(theme.get("orientation"), "landscape")
        orientation.changed.connect(lambda v: self.theme_edited.emit("orientation", v))
        self._row(form, "orientation", orientation, "theme" in document.data and
                  "orientation" in theme, lambda: self.theme_edited.emit("orientation", None),
                  "Which way up the panel is used; a theme's coordinates are drawn "
                  "for one shape")
        form = self._group(column, "Background")
        colour = controls.ColorField()
        colour.setObjectName("theme.background")
        colour.set_value(theme.get("background"), "#000000")
        colour.changed.connect(lambda v: self.theme_edited.emit("background", v))
        self._row(form, "background", colour, "background" in theme,
                  lambda: self.theme_edited.emit("background", None),
                  "A colour under everything; the wallpaper is drawn over it")
        wall = QWidget()
        wall_row = QHBoxLayout(wall)
        wall_row.setContentsMargins(0, 0, 0, 0)
        wall_edit = controls.TextField()
        wall_edit.setObjectName("theme.wallpaper")
        wall_edit.set_value(theme.get("wallpaper"), "none")
        wall_edit.changed.connect(lambda v: self.theme_edited.emit("wallpaper", v))
        browse = QPushButton("Choose…")
        browse.clicked.connect(lambda: self._choose_wallpaper(document))
        wall_row.addWidget(wall_edit, 1)
        wall_row.addWidget(browse)
        self._row(form, "wallpaper", wall, "wallpaper" in theme,
                  lambda: self.theme_edited.emit("wallpaper", None),
                  "An image in the theme's folder, scaled to fill the panel")
        form = self._group(column, "Text")
        font = controls.FontField(document.directory)
        font.setObjectName("theme.font")
        font.set_value(theme.get("font"), "Pillow's built-in font")
        font.changed.connect(lambda v: self.theme_edited.emit("font", v))
        self._row(form, "font", font, "font" in theme,
                  lambda: self.theme_edited.emit("font", None),
                  "Default font for every widget: a file in the theme's folder, "
                  "or a system font file name")
        self._finish(body, column)
        self._set_footer(problems, count)

    def _choose_wallpaper(self, document: ThemeDocument) -> None:
        name = pick_image(self, document, "Choose wallpaper")
        if name is not None:
            self.theme_edited.emit("wallpaper", name)

    # -- a widget --

    def show_widget(self, document: ThemeDocument, ref: Ref, slot: Slot | None,
                    problems: list[Problem], count: int, orientation: Any,
                    inverted: bool, screen: tuple[int, int]) -> None:
        body, column = self._reset()
        entry = document.entry(ref)
        widget_id = document.widget_id(ref)
        cls = lookup(ref.plugin)
        if cls is None:
            problems = [Problem(ERROR, f"no plugin called {ref.plugin!r} is installed",
                                widget_id)] + problems
        self._problem_boxes(column, [p for p in problems if p.widget == widget_id])

        # Geometry: the theme's keys, not the plugin's.
        form = self._group(column, "Geometry")
        grid = QWidget()
        grid_row = QHBoxLayout(grid)
        grid_row.setContentsMargins(0, 0, 0, 0)
        grid_row.setSpacing(6)
        width, height = screen
        current = Rect(*(int(entry.get(k, 0) or 0) if isinstance(entry.get(k), int) else 0
                         for k in ("x", "y", "width", "height")))
        spins: dict[str, QSpinBox] = {}
        for key, top in (("x", width - 1), ("y", height - 1), ("width", width),
                         ("height", height)):
            box = QVBoxLayout()
            box.setSpacing(2)
            caption = QLabel(key)
            caption.setObjectName("hint")
            spin = QSpinBox()
            spin.setObjectName(f"geometry.{key}")
            spin.setRange(0 if key in ("x", "y") else 1, max(top, 1))
            spin.setKeyboardTracking(False)
            # Share the row rather than insist on the hint's width, so four
            # of them never push the form wider than its column.
            spin.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            spin.setMinimumWidth(48)
            spin.setValue(getattr(current, key))
            spins[key] = spin
            box.addWidget(caption)
            box.addWidget(spin)
            grid_row.addLayout(box)

        def geometry_changed(_value: int = 0) -> None:
            rect = Rect(*(spins[k].value() for k in ("x", "y", "width", "height")))
            if rect != current:
                self.geometry_edited.emit(ref, rect)

        aligned = layout.is_aligned(current, orientation, inverted)
        axis, value, extent, reflected = layout.bound_axis(current, orientation, inverted)
        # Step a colour-safe slot at a time on the bound axis, so the
        # arrows keep an aligned box aligned: moving by a whole slot keeps
        # either edge's residue, and when the far edge is the one bound
        # (a reflected axis), resizing must step whole slots too.
        modulus = AKP02.SHORT_AXIS_ALIGN_MODULUS
        spins[axis].setSingleStep(modulus)
        if reflected:
            spins["width" if axis == "x" else "height"].setSingleStep(modulus)
        for spin in spins.values():
            spin.valueChanged.connect(geometry_changed)
        form.addRow(grid)
        # Only a box that breaks the rule says anything: a safe one is the
        # normal case, and snapping keeps it so.
        if not aligned:
            status_row = QWidget()
            status = QHBoxLayout(status_row)
            status.setContentsMargins(0, 0, 0, 0)
            nearest = layout.nearest_aligned(current, orientation, inverted)
            note = QLabel(f"{axis}={value} is not colour-safe"
                          + (f"; nearest {' or '.join(map(str, nearest))}" if nearest else ""))
            note.setObjectName("bad")
            note.setWordWrap(True)
            status.addWidget(note, 1)
            fix = QPushButton("Snap")
            fix.clicked.connect(lambda: self.geometry_edited.emit(
                ref, snap(current, orientation, inverted, screen)))
            status.addWidget(fix)
            form.addRow(status_row)

        fps = controls.NumberField(False, 0.001, 240.0)
        fps.setObjectName("opt.fps")
        default_fps = cls.default_fps if cls is not None else 1.0
        shown = f"{default_fps:g} (default)"
        built = slot.widget if slot is not None else None
        if built is not None and getattr(built, "fps_auto", False):
            # An animation picks its own rate from the file.
            default_fps = built.fps
            shown = f"{built.fps:.3g} (from the file)"
        fps.set_value(entry.get("fps"), default_fps, shown)
        if fps.slider is not None:
            fps.slider.hide()
        fps.changed.connect(lambda v: self.widget_edited.emit(ref, "fps", v))
        self._row(form, "fps", fps, "fps" in entry,
                  lambda: self.widget_edited.emit(ref, "fps", None),
                  "Updates per second: how often it reads its source and redraws")
        ident = controls.TextField()
        ident.setObjectName("opt.id")
        ident.set_value(entry.get("id"), ref.default_id)
        ident.changed.connect(lambda v: self.widget_edited.emit(ref, "id", v))
        self._row(form, "id", ident, "id" in entry,
                  lambda: self.widget_edited.emit(ref, "id", None),
                  "A name for log and error messages; defaults to plugin#n")

        # The plugin's own options, then its source's, then the base's.
        spec = _SpecLike(ref, entry, slot)
        options = options_for(ref.plugin, entry)
        values = Schema(*options).effective(entry, spec, resolve=sources.lookup)
        groups: dict[str, QFormLayout] = {}
        for option in options:
            if not option.applies(values):
                continue
            form = groups.get(option.group)
            if form is None:
                form = groups[option.group] = self._group(column, option.group)
            control = self._control(document, ref, option, entry, values, spec, slot)
            explicit = option.key in entry
            doc = option.doc
            if isinstance(option.default, Auto):
                doc = (doc + "\n\n" if doc else "") + f"Default: {option.default.describe}."
            self._row(form, option.key, control, explicit,
                      # clicked passes `checked` first: take it, so it never
                      # lands in `k` -- which would reset a key named False.
                      lambda _checked=False, k=option.key: self.widget_edited.emit(ref, k, None),
                      doc, required=option.required,
                      inactive=option.inactive_reason(values))

        known = set(RESERVED) | {o.key for o in options}
        unknown = [k for k in entry if k not in known]
        if unknown:
            form = self._group(column, "Not recognised")
            for key in unknown:
                text = QLabel(f"{key} = {entry[key]!r}")
                text.setObjectName("bad")
                text.setWordWrap(True)
                remove = QToolButton()
                remove.setText("Remove")
                remove.clicked.connect(lambda _c=False, k=key: self.widget_edited.emit(ref, k, None))
                holder = QWidget()
                row = QHBoxLayout(holder)
                row.setContentsMargins(0, 0, 0, 0)
                row.addWidget(text, 1)
                row.addWidget(remove)
                form.addRow(holder)
        self._finish(body, column)
        self._set_footer(problems, count)

    def _control(self, document: ThemeDocument, ref: Ref, option: Option,
                 entry: dict[str, Any], values: Values, spec: Any,
                 slot: Slot | None) -> QWidget:
        key, kind = option.key, option.kind
        value = entry.get(key)
        label = default_label(option, values, spec)

        def emit(new: Any) -> None:
            self.widget_edited.emit(ref, key, new)

        control: QWidget
        if kind == SOURCE:
            names = [name for name in sources.available()
                     if (cls := sources.lookup(name)) is not None
                     and (not option.accepts or cls.kind in option.accepts)]
            field = controls.SourceField(names, option.required)
            field.set_value(value)
            for index in range(field.count()):
                cls = sources.lookup(field.itemData(index) or "")
                if cls is not None and cls.summary:
                    field.setItemData(index, cls.summary, Qt.ItemDataRole.ToolTipRole)
            field.changed.connect(emit)
            control = field
        elif kind == FORMAT:
            holder = QWidget()
            column = QVBoxLayout(holder)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(3)
            source_name = entry.get("source")
            source_cls = sources.lookup(source_name) if isinstance(source_name, str) else None
            live = self._fields(slot)
            presets = []
            for preset in (source_cls.formats if source_cls is not None else ()):
                shown = _format(preset, live)
                presets.append((preset, f"{preset}    → {shown}" if shown else preset))
            field = controls.SuggestField(mono=True)
            field.setObjectName(f"opt.{key}")
            holder.setFocusProxy(field)
            default_format = values.get(key) if isinstance(option.default, Auto) else None
            field.set_value(value, presets,
                            default_format or ("" if label in ("none", "") else label))
            field.changed.connect(emit)
            column.addWidget(field)
            if source_cls is not None:
                sample = QLabel()
                sample.setObjectName("hint")
                sample.setWordWrap(True)
                sample.setTextFormat(Qt.TextFormat.RichText)
                column.addWidget(sample)
                sample.setProperty("fields", ", ".join(source_cls.fields))
                # What is drawn: the typed format, else the default one.
                current = lambda f=field, d=default_format: f.currentText().strip() or d or ""
                self._samples.append((sample, slot, current))
                self._sample(sample, slot, current())
            control = holder
        elif kind == STRING and option.suggest is not None:
            field = controls.SuggestField()
            field.set_value(value, option.suggestions(values),
                            "" if label == "none" else label)
            field.changed.connect(emit)
            control = field
        elif kind in (STRING,):
            field = controls.TextField()
            field.set_value(value, "" if label == "none" else label)
            field.changed.connect(emit)
            control = field
        elif kind == IMAGE:
            holder = QWidget()
            row = QHBoxLayout(holder)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            field = controls.TextField()
            field.setObjectName(f"opt.{key}")
            field.set_value(value, label)
            field.changed.connect(emit)
            browse = QPushButton("Choose…")
            browse.clicked.connect(lambda _c=False: (
                name := pick_image(self, document, "Choose picture")) and emit(name))
            row.addWidget(field, 1)
            row.addWidget(browse)
            holder.setFocusProxy(field)
            holder.setObjectName(f"opt.{key}.row")
            control = holder
        elif kind == FONT:
            field = controls.FontField(document.directory)
            field.set_value(value, "theme font")
            field.changed.connect(emit)
            control = field
        elif kind in (INTEGER, NUMBER):
            field = controls.NumberField(kind == INTEGER, option.low, option.high)
            default = values.get(key) if isinstance(option.default, Auto) else option.default
            field.set_value(value, default if isinstance(default, (int, float)) else None,
                            label if isinstance(option.default, Auto) or option.default is None
                            else "")
            field.changed.connect(emit)
            control = field
        elif kind == BOOLEAN:
            field = controls.BoolField()
            field.set_value(value, option.default)
            field.changed.connect(emit)
            control = field
        elif kind == CHOICE:
            field = controls.ChoiceField(option.choices)
            field.set_value(value, label)
            field.changed.connect(emit)
            control = field
        elif kind == COLOR:
            field = controls.ColorField()
            field.set_value(value, option.default if isinstance(option.default, str) else None)
            field.changed.connect(emit)
            control = field
        elif kind == COLORS:
            field = controls.GradientField()
            field.set_value(value, option.default)
            field.changed.connect(emit)
            control = field
        elif kind == SECTIONS:
            field = controls.SectionsField()
            field.set_value(value)
            field.changed.connect(emit)
            control = field
        else:
            control = QLabel(repr(value))
        if not control.objectName():
            control.setObjectName(f"opt.{key}")
        return control

    @staticmethod
    def _fields(slot: Slot | None) -> dict[str, Any] | None:
        source = getattr(slot.widget, "_source", None) if slot is not None and slot.widget else None
        if source is None:
            return None
        try:
            return source.read(time.monotonic())[1]
        except Exception:  # noqa: BLE001
            return None

    def _sample(self, label: QLabel, slot: Slot | None, fmt: str) -> None:
        fields_text = label.property("fields") or ""
        shown = ""
        fields = self._fields(slot)
        if fields is not None and fmt:
            try:
                shown = fmt.format(**fields)
            except Exception as exc:  # noqa: BLE001
                shown = f"<span style='color:{style.BAD}'>{type(exc).__name__}: {exc}</span>"
            else:
                shown = f"→ <span style='color:{style.TEXT}'>{_escape(shown)}</span>"
        label.setText(f"{shown}<br>fields: {fields_text}" if shown else f"fields: {fields_text}")

    def _refresh_samples(self) -> None:
        if not self.isVisible():
            return
        for label, slot, current in list(self._samples):
            try:
                self._sample(label, slot, current())
            except RuntimeError:  # deleted with a rebuilt form
                self._samples = []
                return

    # -- layout helpers --

    def _group(self, column: QVBoxLayout, title: str) -> QFormLayout:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setContentsMargins(0, 4, 0, 0)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        column.addWidget(box)
        return form

    def _row(self, form: QFormLayout, key: str, control: QWidget, explicit: bool,
             reset, doc: str = "", required: bool = False,
             inactive: str | None = None) -> None:
        name = QLabel(key + (" *" if required else ""))
        name.setMinimumWidth(84)
        name.setStyleSheet(f"color: {style.MUTED if explicit and not inactive else style.FAINT};")
        if inactive:
            # Set aside by another option: greyed, not hidden, so it is
            # plain which of the two is in effect. The × stays usable.
            doc = inactive + (f"\n\n{doc}" if doc else "")
            control.setEnabled(False)
        if doc:
            name.setToolTip(doc)
            control.setToolTip(doc)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(control, 1)
        # Parented from the start. A widget with no parent that is made
        # visible becomes a window of its own, if only until it is put in
        # a layout -- a flash a tiling window manager tiles.
        undo = QToolButton(holder)
        undo.setObjectName("flat")
        undo.setText("×")
        undo.setToolTip("Remove from the theme, so the default applies")
        undo.setFixedWidth(24)
        undo.setStyleSheet(f"padding: 0; color: {style.FAINT}; font-size: 15px;")
        # A mouse shortcut; from the keyboard, emptying the field does
        # the same. Out of the Tab order, so Tab goes field to field.
        undo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        undo.clicked.connect(reset)
        sizer = undo.sizePolicy()
        sizer.setRetainSizeWhenHidden(True)
        undo.setSizePolicy(sizer)
        row.addWidget(undo)
        undo.setVisible(explicit and not required)
        form.addRow(name, holder)


def _format(text: str, fields: dict[str, Any] | None) -> str | None:
    if fields is None:
        return None
    try:
        return text.format(**fields)
    except Exception:  # noqa: BLE001
        return None


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
