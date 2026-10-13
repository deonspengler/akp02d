"""Input controls for theme values.

Each control edits one TOML value and can also stand for "not set", so
the theme's default applies -- which is most options, most of the time.
A control shows the default as placeholder text when unset, and emits
`changed(value)` with the new value, or None to remove the key.

Values are committed when editing finishes (Enter, focus out, a picked
colour), not per keystroke, so one change is one undo step and a
half-typed number never reaches the preview.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from PIL import ImageColor
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QHBoxLayout, QLineEdit, QMenu,
    QSizePolicy, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from .document import toml_value
from . import style


def rgb(value: Any) -> QColor | None:
    """A Pillow colour string (or RGB tuple) as a QColor, or None."""
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return QColor(*value[:3])
    if not isinstance(value, str) or not value:
        return None
    try:
        return QColor(*ImageColor.getrgb(value)[:3])
    except (ValueError, AttributeError):
        return None


def hex_of(colour: QColor) -> str:
    return colour.name(QColor.NameFormat.HexRgb)


class Swatch(QToolButton):
    """A colour chip that opens a picker."""

    picked = Signal(str)

    def __init__(self, parent: QWidget | None = None, size: int = 22) -> None:
        super().__init__(parent)
        self._colour: QColor | None = None
        self.setFixedSize(QSize(size, size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # The text beside a swatch takes the same colour from the keyboard.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.clicked.connect(self._pick)
        self.setAccessibleName("Pick colour")

    def set_colour(self, colour: QColor | None) -> None:
        self._colour = colour
        self.update()

    def _pick(self) -> None:
        start = self._colour or QColor("#ffffff")
        chosen = QColorDialog.getColor(start, self, "Choose colour")
        if chosen.isValid():
            self.picked.emit(hex_of(chosen))

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.25)   # set aside: still there, plainly not in use
        box = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._colour is None:
            painter.setPen(QPen(QColor(style.FAINT), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        else:
            painter.setPen(QPen(QColor(style.BORDER), 1))
            painter.setBrush(self._colour)
        painter.drawRoundedRect(box, 4, 4)


class ColorField(QWidget):
    """Swatch plus text: any colour Pillow reads ("#rrggbb", "white")."""

    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.swatch = Swatch(self)
        self.edit = QLineEdit(self)
        self.edit.setMaximumWidth(110)
        layout.addWidget(self.swatch)
        layout.addWidget(self.edit)
        layout.addStretch(1)
        self.setFocusProxy(self.edit)
        self.swatch.picked.connect(self._picked)
        self.edit.editingFinished.connect(self._typed)
        self._value: str | None = None
        self._default: str | None = None

    def set_value(self, value: Any, default: Any = None) -> None:
        self._value = value if isinstance(value, str) else None
        self._default = default if isinstance(default, str) else None
        self.edit.setText(self._value or "")
        self.edit.setPlaceholderText(self._default or "none")
        self.swatch.set_colour(rgb(self._value or self._default))
        self.edit.setProperty("invalid", bool(self._value) and rgb(self._value) is None)
        self.edit.style().polish(self.edit)

    def _picked(self, colour: str) -> None:
        self.edit.setText(colour)
        self._typed()

    def _typed(self) -> None:
        text = self.edit.text().strip() or None
        if text == self._value:
            return
        self._value = text
        self.swatch.set_colour(rgb(text or self._default))
        self.changed.emit(text)


class GradientBar(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.stops: list[QColor] = []
        self.setMinimumHeight(18)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.stops:
            ramp = QLinearGradient(box.topLeft(), box.topRight())
            count = len(self.stops)
            for index, colour in enumerate(self.stops):
                ramp.setColorAt(index / max(count - 1, 1), colour)
            painter.setBrush(ramp)
        painter.setPen(QPen(QColor(style.BORDER), 1))
        painter.drawRoundedRect(box, 4, 4)


class GradientField(QWidget):
    """A colour, or several: a gradient is a colour with more stops.

    One stop looks and works like a colour field -- swatch and text --
    with a + beside it. Adding a stop turns it into a gradient: the ramp
    it makes, one chip per stop (click to change, right-click to
    remove). Removing back down to one stop makes it a colour again.
    One stop is written as a plain colour, more as a list.
    """

    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self.bar = GradientBar(self)
        outer.addWidget(self.bar)
        self.row = QHBoxLayout()
        self.row.setSpacing(4)
        outer.addLayout(self.row)
        self.edit: QLineEdit | None = None
        self._stops: list[str] = []

    @property
    def is_gradient(self) -> bool:
        return len(self._stops) > 1

    def set_value(self, value: Any, default: Sequence[str] | str | None = None) -> None:
        self._stops = _stops(value) or _stops(default)
        self._rebuild()

    def _rebuild(self) -> None:
        while self.row.count():
            item = self.row.takeAt(0)
            old = item.widget()
            if old is not None:
                # Hidden and deleted where it is: detaching it first would
                # make it a window of its own for a moment.
                old.hide()
                old.deleteLater()
        self.edit = None
        self.bar.setVisible(self.is_gradient)
        self.bar.stops = [rgb(s) or QColor("#000000") for s in self._stops]
        self.bar.update()
        if self.is_gradient:
            for index, stop in enumerate(self._stops):
                chip = Swatch(self, 20)
                chip.set_colour(rgb(stop))
                chip.setToolTip(f"{stop} -- click to change, right-click to remove")
                chip.picked.connect(lambda colour, i=index: self._set(i, colour))
                chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                chip.customContextMenuRequested.connect(
                    lambda pos, i=index, c=chip: self._menu(i, c, pos))
                self.row.addWidget(chip)
        else:
            stop = self._stops[0] if self._stops else None
            chip = Swatch(self)
            chip.set_colour(rgb(stop))
            chip.picked.connect(lambda colour: self._set(0, colour))
            self.row.addWidget(chip)
            self.edit = QLineEdit(self)
            self.edit.setMaximumWidth(110)
            self.edit.setText(stop or "")
            self.edit.setPlaceholderText("none")
            self.edit.setProperty("invalid", bool(stop) and rgb(stop) is None)
            self.edit.editingFinished.connect(self._typed)
            self.row.addWidget(self.edit)
            self.setFocusProxy(self.edit)
        add = QToolButton(self)
        add.setText("+")
        add.setToolTip("Add a stop" if self.is_gradient
                       else "Add a second colour, making a gradient")
        add.setFixedSize(QSize(22, 22))
        # The theme's button padding would leave no room for the +.
        add.setStyleSheet("padding: 0; font-size: 15px;")
        add.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        add.clicked.connect(self._add)
        self.row.addWidget(add)
        self.row.addStretch(1)

    def _typed(self) -> None:
        assert self.edit is not None
        text = self.edit.text().strip()
        current = self._stops[0] if self._stops else ""
        if text == current:
            return
        if not text:
            self._stops = []
            self.changed.emit(None)     # back to the default
            self._rebuild()
            return
        self._set(0, text)

    def _menu(self, index: int, chip: QWidget, pos) -> None:
        menu = QMenu(self)
        remove = menu.addAction("Remove stop")
        remove.setEnabled(len(self._stops) > 1)
        if menu.exec(chip.mapToGlobal(pos)) is remove:
            del self._stops[index]
            self._emit()

    def _set(self, index: int, colour: str) -> None:
        if index < len(self._stops):
            self._stops[index] = colour
        else:
            self._stops.append(colour)
        self._emit()

    def _add(self) -> None:
        self._stops.append(self._stops[-1] if self._stops else "#ffffff")
        if len(self._stops) == 1:
            self._stops.append(self._stops[0])
        self._emit()

    def _emit(self) -> None:
        stops = list(self._stops)
        # Rebuilt after the signal's handlers run: the edit may be the
        # widget that sent us here.
        QTimer.singleShot(0, self, self._rebuild)
        self.changed.emit(stops[0] if len(stops) == 1 else stops)


def _stops(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


class TextField(QLineEdit):
    """A string; empty means not set."""

    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None, mono: bool = False) -> None:
        super().__init__(parent)
        if mono:
            font = self.font()
            font.setFamily("monospace")
            font.setStyleHint(QFont.StyleHint.Monospace)
            self.setFont(font)
        self._value: str | None = None
        self.editingFinished.connect(self._commit)

    def set_value(self, value: Any, placeholder: str = "") -> None:
        self._value = value if isinstance(value, str) else None
        self.setText(self._value or "")
        self.setPlaceholderText(placeholder)

    def _commit(self) -> None:
        text = self.text()
        value = text if text != "" else None
        if value != self._value:
            self._value = value
            self.changed.emit(value)


class NumberField(QWidget):
    """An int or float, with a slider when the range is small."""

    changed = Signal(object)

    def __init__(self, integer: bool, low: float | None, high: float | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.integer = integer
        self.low, self.high = low, high
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.edit = QLineEdit(self)
        self.edit.setMaximumWidth(90)
        self.setFocusProxy(self.edit)
        layout.addWidget(self.edit)
        self.slider: QSlider | None = None
        if low is not None and high is not None and not integer and high - low <= 10:
            self.slider = QSlider(Qt.Orientation.Horizontal, self)
            self.slider.setRange(0, 1000)
            self.slider.sliderReleased.connect(self._slid)
            self.slider.valueChanged.connect(self._sliding)
            layout.addWidget(self.slider, 1)
        else:
            layout.addStretch(1)
        self.edit.editingFinished.connect(self._commit)
        self._value: float | int | None = None
        self._default: Any = None

    def set_value(self, value: Any, default: Any = None, placeholder: str = "") -> None:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        self._value = value if ok else None
        self._default = default
        self.edit.setText(_number_text(self._value) if self._value is not None else "")
        self.edit.setPlaceholderText(placeholder or (
            _number_text(default) if isinstance(default, (int, float)) else ""))
        self.edit.setProperty("invalid", False)
        self.edit.style().polish(self.edit)
        if self.slider is not None:
            shown = self._value if self._value is not None else default
            self.slider.blockSignals(True)
            if isinstance(shown, (int, float)):
                self.slider.setValue(self._to_slider(shown))
            self.slider.blockSignals(False)

    def _to_slider(self, value: float) -> int:
        assert self.low is not None and self.high is not None
        span = self.high - self.low or 1
        return round((value - self.low) / span * 1000)

    def _from_slider(self) -> float:
        assert self.slider is not None and self.low is not None and self.high is not None
        return round(self.low + self.slider.value() / 1000 * (self.high - self.low), 3)

    def _sliding(self) -> None:
        if self.slider is not None and self.slider.isSliderDown():
            self.edit.setText(_number_text(self._from_slider()))
        elif self.slider is not None:
            self._slid()

    def _slid(self) -> None:
        self.edit.setText(_number_text(self._from_slider()))
        self._commit()

    def _commit(self) -> None:
        text = self.edit.text().strip()
        if not text:
            value = None
        else:
            try:
                number = float(text)
            except ValueError:
                self.edit.setProperty("invalid", True)
                self.edit.style().polish(self.edit)
                return
            value = int(number) if self.integer and number.is_integer() else number
            if self.integer and not float(number).is_integer():
                self.edit.setProperty("invalid", True)
                self.edit.style().polish(self.edit)
                return
        self.edit.setProperty("invalid", False)
        self.edit.style().polish(self.edit)
        if value != self._value:
            self._value = value
            self.changed.emit(value)


def _number_text(value: Any) -> str:
    if isinstance(value, float):
        return toml_value(value)
    return str(value)


def _narrow(combo: QComboBox) -> None:
    """Size a combo by a short minimum, not its longest item, so a long
    font list cannot push the settings panel wider than its column."""
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(8)
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class ChoiceField(QComboBox):
    """One of a fixed set.

    Only the choices are listed. While the theme leaves the key out, the
    one in effect -- the default, or what `auto` works out -- is shown
    selected; the row's faint name and hidden × already say it is not
    set, and × is how to go back to it. A list entry for "the default"
    would only repeat one of the choices.
    """

    changed = Signal(object)

    def __init__(self, choices: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _narrow(self)
        self._choices = list(choices)
        self._value: str | None = None
        self.activated.connect(self._picked)

    def set_value(self, value: Any, default_label: str) -> None:
        self.blockSignals(True)
        self.clear()
        self._value = value if isinstance(value, str) else None
        in_effect = default_label.removeprefix("auto: ")
        if self._value is None and in_effect not in self._choices:
            # No default among the choices ("none", "required"): say so.
            self.addItem(default_label, None)
        for choice in self._choices:
            self.addItem(choice, choice)
        if self._value is not None and self._value not in self._choices:
            self.addItem(f"{self._value} (invalid)", self._value)
        shown = self.findData(self._value if self._value is not None else in_effect)
        self.setCurrentIndex(max(0, shown))
        self.blockSignals(False)

    def _picked(self, index: int) -> None:
        value = self.itemData(index)
        if value != self._value:
            self._value = value
            self.changed.emit(value)


class SourceField(ChoiceField):
    """A data source, by name."""

    def __init__(self, names: Sequence[str], required: bool,
                 parent: QWidget | None = None) -> None:
        super().__init__(names, parent)
        self._required = required

    def set_value(self, value: Any, default_label: str = "") -> None:  # type: ignore[override]
        self.blockSignals(True)
        self.clear()
        if not self._required or not value:
            self.addItem("choose a source…" if self._required else "none (fixed text)", None)
        for choice in self._choices:
            self.addItem(choice, choice)
        self._value = value if isinstance(value, str) else None
        if self._value is not None and self._value not in self._choices:
            self.addItem(f"{self._value} (unknown)", self._value)
        found = self.findData(self._value)
        self.setCurrentIndex(found if found >= 0 else 0)
        self.blockSignals(False)


class BoolField(QCheckBox):
    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.toggled.connect(lambda on: self.changed.emit(bool(on)))

    def set_value(self, value: Any, default: Any = False) -> None:
        self.blockSignals(True)
        self.setChecked(bool(value) if isinstance(value, bool) else bool(default))
        self.blockSignals(False)


class FontField(QComboBox):
    """A theme-relative font file or a system font name."""

    changed = Signal(object)

    def __init__(self, theme_dir: Path | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _narrow(self)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._theme_dir = theme_dir
        self._value: str | None = None
        self.lineEdit().editingFinished.connect(self._commit)
        self.activated.connect(lambda _i: self._commit())

    def set_value(self, value: Any, placeholder: str = "theme font") -> None:
        self.blockSignals(True)
        self.clear()
        self.addItem("")
        for name in font_names(self._theme_dir):
            self.addItem(name)
        self._value = value if isinstance(value, str) else None
        self.setEditText(self._value or "")
        self.lineEdit().setPlaceholderText(placeholder)
        self.blockSignals(False)

    def _commit(self) -> None:
        text = self.currentText().strip() or None
        if text != self._value:
            self._value = text
            self.changed.emit(text)


class SuggestField(QComboBox):
    """A string, typed or picked from what this machine offers.

    Each suggestion shows a description (a sensor's reading, a disk's
    size) but stores its plain value. Typing anything else is allowed:
    a theme may name a sensor or mount only the panel's machine has.
    """

    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None, mono: bool = False) -> None:
        super().__init__(parent)
        _narrow(self)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if mono:
            font = self.font()
            font.setFamily("monospace")
            font.setStyleHint(QFont.StyleHint.Monospace)
            self.setFont(font)
        self._value: str | None = None
        self.lineEdit().editingFinished.connect(self._commit)
        self.activated.connect(self._picked)

    def set_value(self, value: Any, suggestions: Sequence[tuple[str, str]],
                  placeholder: str = "") -> None:
        self.blockSignals(True)
        self.clear()
        self.addItem("", None)
        for item, description in suggestions:
            self.addItem(description, item)
        self._value = value if isinstance(value, str) else None
        self.setEditText(self._value or "")
        self.lineEdit().setPlaceholderText(placeholder)
        if not suggestions:
            self.setToolTip("Nothing found on this machine; type a value")
        self.blockSignals(False)

    def _picked(self, index: int) -> None:
        value = self.itemData(index)
        # Show the plain value in the field, not the description.
        self.setEditText(value or "")
        self._set(value)

    def _commit(self) -> None:
        text = self.currentText().strip()
        # A typed (or still-shown) description maps back to its value.
        for index in range(1, self.count()):
            if self.itemText(index) == text:
                text = self.itemData(index)
                break
        self._set(text or None)

    def _set(self, value: str | None) -> None:
        if value != self._value:
            self._value = value
            self.changed.emit(value)


_SYSTEM_FONTS: list[str] | None = None


def font_names(theme_dir: Path | None) -> list[str]:
    """Font files a theme can name: its own first, then the system's.

    Pillow finds a bare file name in the system font directories, which
    is what the daemon passes it, so a file name is the right thing to
    offer -- a family name would not load.
    """
    global _SYSTEM_FONTS
    own: list[str] = []
    if theme_dir is not None and theme_dir.is_dir():
        own = sorted(p.name for p in theme_dir.iterdir()
                     if p.suffix.lower() in (".ttf", ".otf", ".ttc"))
    if _SYSTEM_FONTS is None:
        found: set[str] = set()
        roots = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                 Path.home() / ".local/share/fonts", Path.home() / ".fonts"]
        for root in roots:
            if not root.is_dir():
                continue
            for _dirpath, _dirs, files in os.walk(root):
                found.update(f for f in files
                             if f.lower().endswith((".ttf", ".otf", ".ttc")))
        _SYSTEM_FONTS = sorted(found, key=str.lower)
    return own + [name for name in _SYSTEM_FONTS if name not in own]


class SectionsField(QLineEdit):
    """A table of tables as inline TOML: { general = { autosens = 1 } }."""

    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("{ general = { autosens = 1 } }")
        self._value: Any = None
        self.editingFinished.connect(self._commit)

    def set_value(self, value: Any) -> None:
        self._value = value if isinstance(value, dict) else None
        self.setText(toml_value(self._value) if self._value else "")
        self.setProperty("invalid", False)
        self.style().polish(self)

    def _commit(self) -> None:
        text = self.text().strip()
        if not text:
            value = None
        else:
            try:
                value = tomllib.loads(f"v = {text}")["v"]
            except tomllib.TOMLDecodeError:
                self.setProperty("invalid", True)
                self.style().polish(self)
                return
            if not isinstance(value, dict):
                self.setProperty("invalid", True)
                self.style().polish(self)
                return
        self.setProperty("invalid", False)
        self.style().polish(self)
        if value != self._value:
            self._value = value
            self.changed.emit(value)


class Segmented(QWidget):
    """A row of mutually exclusive toggle buttons."""

    changed = Signal(str)

    def __init__(self, options: Sequence[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.buttons: dict[str, QToolButton] = {}
        for index, (key, label) in enumerate(options):
            button = QToolButton(self)
            button.setText(label)
            button.setCheckable(True)
            button.setAutoRaise(False)
            if len(options) == 1:
                pass
            elif index == 0:
                button.setObjectName("segFirst")
            elif index == len(options) - 1:
                button.setObjectName("segLast")
            else:
                button.setObjectName("seg")
            button.clicked.connect(lambda _c=False, k=key: self._clicked(k))
            layout.addWidget(button)
            self.buttons[key] = button
        self._value = options[0][0] if options else ""

    def value(self) -> str:
        return self._value

    def set_value(self, key: str) -> None:
        self._value = key
        for name, button in self.buttons.items():
            button.setChecked(name == key)

    def _clicked(self, key: str) -> None:
        if key == self._value:
            self.buttons[key].setChecked(True)
            return
        self.set_value(key)
        self.changed.emit(key)
