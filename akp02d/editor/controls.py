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
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QMenu,
    QPushButton,
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


class ColourDialog(QDialog):
    """Qt's colour picker, with a Remove colour button for a gradient's
    stops -- so taking one out needs no right-click."""

    REMOVE = "remove"

    def __init__(self, start: QColor, removable: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose colour")
        column = QVBoxLayout(self)
        self.picker = QColorDialog(start, self)
        self.picker.setOptions(QColorDialog.ColorDialogOption.NoButtons
                               | QColorDialog.ColorDialogOption.DontUseNativeDialog)
        # A child widget, not a window of its own.
        self.picker.setWindowFlags(Qt.WindowType.Widget)
        column.addWidget(self.picker)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.removed = False
        row = QHBoxLayout()
        if removable:
            # On the left, apart from OK and Cancel, where it is not hit
            # by mistake.
            remove = QPushButton("Remove colour", self)
            remove.setObjectName("remove")
            remove.setToolTip("Take this stop out of the gradient")
            remove.setAutoDefault(False)
            remove.clicked.connect(self._remove)
            row.addWidget(remove)
        row.addStretch(1)
        row.addWidget(buttons)
        column.addLayout(row)

    def _remove(self) -> None:
        self.removed = True
        self.accept()

    @classmethod
    def ask(cls, start: QColor, removable: bool, parent: QWidget | None) -> str | None:
        """The chosen colour as "#rrggbb", REMOVE, or None if cancelled."""
        dialog = cls(start, removable, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        if dialog.removed:
            return cls.REMOVE
        chosen = dialog.picker.currentColor()
        return hex_of(chosen) if chosen.isValid() else None


class Swatch(QToolButton):
    """A colour chip that opens a picker (whose own hex box takes a typed
    value). Its tooltip names the colour; Space or Enter opens it from
    the keyboard. A removable one -- a stop of a gradient with others --
    offers Remove colour in the picker too."""

    picked = Signal(str)
    removed = Signal()

    def __init__(self, parent: QWidget | None = None, size: int = 22,
                 focusable: bool = False, removable: bool = False) -> None:
        super().__init__(parent)
        self._colour: QColor | None = None
        self._invalid = False
        self.removable = removable
        self.setFixedSize(QSize(size, size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # A field's own swatch takes Tab; a gradient's many chips do not,
        # so Tab still goes field to field.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if focusable
                            else Qt.FocusPolicy.NoFocus)
        self.clicked.connect(self._pick)
        self.setAccessibleName("Pick colour")

    def set_colour(self, colour: QColor | None, invalid: bool = False) -> None:
        self._colour = colour
        self._invalid = invalid
        self.update()

    def _pick(self) -> None:
        start = self._colour or QColor("#ffffff")
        chosen = ColourDialog.ask(start, self.removable, self)
        if chosen == ColourDialog.REMOVE:
            self.removed.emit()
        elif chosen is not None:
            self.picked.emit(chosen)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.25)   # set aside: still there, plainly not in use
        box = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self._invalid:
            # A colour the theme names that cannot be read.
            painter.setPen(QPen(QColor(style.BAD), 1.5, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        elif self._colour is None:
            painter.setPen(QPen(QColor(style.FAINT), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
        else:
            painter.setPen(QPen(QColor(style.ACCENT if self.hasFocus() else style.BORDER), 1))
            painter.setBrush(self._colour)
        painter.drawRoundedRect(box, 4, 4)


def _describe(value: str | None, default: str | None) -> str:
    if value:
        return value if rgb(value) is not None else f"{value} -- not a colour Pillow reads"
    return f"{default} (default)" if default else "none"


class ColorField(QWidget):
    """A colour, as a swatch: click it to pick. Any colour Pillow reads
    ("#rrggbb", "white") is accepted from the theme file."""

    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.swatch = Swatch(self, focusable=True)
        layout.addWidget(self.swatch)
        layout.addStretch(1)
        self.setFocusProxy(self.swatch)
        self.swatch.picked.connect(self._picked)
        self._value: str | None = None
        self._default: str | None = None

    @property
    def value(self) -> str | None:
        return self._value

    def set_value(self, value: Any, default: Any = None) -> None:
        self._value = value if isinstance(value, str) else None
        self._default = default if isinstance(default, str) else None
        self._show()

    def _show(self) -> None:
        invalid = bool(self._value) and rgb(self._value) is None
        self.swatch.set_colour(rgb(self._value or self._default), invalid)
        self.swatch.setToolTip(_describe(self._value, self._default))

    def _picked(self, colour: str) -> None:
        if colour == self._value:
            return
        self._value = colour
        self._show()
        self.changed.emit(colour)


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
        self._stops: list[str] = []
        self._explicit = False

    @property
    def is_gradient(self) -> bool:
        return len(self._stops) > 1

    def set_value(self, value: Any, default: Sequence[str] | str | None = None) -> None:
        self._explicit = bool(_stops(value))
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
        self.bar.setVisible(self.is_gradient)
        self.bar.stops = [rgb(s) or QColor("#000000") for s in self._stops]
        self.bar.update()
        if self.is_gradient:
            for index, stop in enumerate(self._stops):
                chip = Swatch(self, 20, removable=True)
                chip.set_colour(rgb(stop))
                chip.setToolTip(f"{stop} -- click to change or remove")
                chip.picked.connect(lambda colour, i=index: self._set(i, colour))
                chip.removed.connect(lambda i=index: self._remove(i))
                chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                chip.customContextMenuRequested.connect(
                    lambda pos, i=index, c=chip: self._menu(i, c, pos))
                self.row.addWidget(chip)
        else:
            stop = self._stops[0] if self._stops else None
            chip = Swatch(self, focusable=True)
            chip.set_colour(rgb(stop), bool(stop) and rgb(stop) is None)
            chip.setToolTip(_describe(stop if self._explicit else None,
                                      None if self._explicit else stop))
            chip.picked.connect(lambda colour: self._set(0, colour))
            self.row.addWidget(chip)
            self.setFocusProxy(chip)
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

    def _menu(self, index: int, chip: QWidget, pos) -> None:
        menu = QMenu(self)
        remove = menu.addAction("Remove stop")
        remove.setEnabled(len(self._stops) > 1)
        if menu.exec(chip.mapToGlobal(pos)) is remove:
            self._remove(index)

    def _remove(self, index: int) -> None:
        if len(self._stops) > 1:
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
        self._explicit = True
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


class AngleField(QWidget):
    """A gradient's angle: the usual four as arrows, any other typed.

    Arrows point the way the colours run, first stop to last: down (180),
    right (90), down-right (135) and down-left (225). The one matching
    the angle in effect is shown pressed.
    """

    changed = Signal(object)
    PRESETS = (("↓", 180.0, "Top to bottom"), ("→", 90.0, "Left to right"),
               ("↘", 135.0, "Top left to bottom right"),
               ("↙", 225.0, "Top right to bottom left"))

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.edit = QLineEdit(self)
        self.edit.setFixedWidth(56)
        self.edit.editingFinished.connect(self._typed)
        layout.addWidget(self.edit)
        degrees = QLabel("°", self)
        layout.addWidget(degrees)
        self.buttons: list[QToolButton] = []
        for arrow, angle, tip in self.PRESETS:
            button = QToolButton(self)
            button.setText(arrow)
            button.setToolTip(f"{tip} ({angle:g}°)")
            button.setCheckable(True)
            button.setFixedSize(QSize(26, 24))
            button.setStyleSheet("padding: 0; font-size: 14px;")
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(lambda _c=False, a=angle: self._pick(a))
            layout.addWidget(button)
            self.buttons.append(button)
        layout.addStretch(1)
        self.setFocusProxy(self.edit)
        self._value: float | None = None
        self._default = 180.0

    def set_value(self, value: Any, default: Any = 180.0) -> None:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        self._value = float(value) if ok else None
        self._default = float(default) if isinstance(default, (int, float)) else 180.0
        self.edit.setText(_degrees(self._value) if self._value is not None else "")
        self.edit.setPlaceholderText(_degrees(self._default))
        self._mark()

    def _mark(self) -> None:
        shown = self._value if self._value is not None else self._default
        for button, (_, angle, _) in zip(self.buttons, self.PRESETS):
            button.setChecked(abs(shown - angle) < 1e-6)

    def _pick(self, angle: float) -> None:
        self._commit(angle)

    def _typed(self) -> None:
        text = self.edit.text().strip().rstrip("°")
        if not text:
            if self._value is not None:
                self._value = None
                self._mark()
                self.changed.emit(None)
            return
        try:
            angle = float(text) % 360.0
        except ValueError:
            self.edit.setText(_degrees(self._value) if self._value is not None else "")
            return
        self._commit(angle)

    def _commit(self, angle: float) -> None:
        self.edit.setText(_degrees(angle))
        if angle != self._value:
            self._value = angle
            self._mark()
            self.changed.emit(int(angle) if angle.is_integer() else angle)
        else:
            self._mark()


def _degrees(angle: float) -> str:
    return f"{angle:g}"


class ZonesField(QWidget):
    """Ranges of the reading, each with a colour: from, to, colour, ×.

    "Add zone" proposes the top fifth of the range in red -- the usual
    first zone -- and later ones just below the lowest so far.
    """

    changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows = QVBoxLayout(self)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(4)
        self._zones: list[list[Any]] = []
        self._range = (0.0, 1.0)

    def set_value(self, value: Any, span: tuple[float, float] = (0.0, 1.0)) -> None:
        self._range = span
        self._zones = []
        if isinstance(value, (list, tuple)):
            for zone in value:
                if isinstance(zone, (list, tuple)) and len(zone) == 3:
                    self._zones.append(list(zone))
        self._rebuild()

    def _rebuild(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            old = item.widget()
            if old is not None:
                old.hide()
                old.deleteLater()
        for index, (low, high, colour) in enumerate(self._zones):
            row = QWidget(self)
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(4)
            for position, number in ((0, low), (1, high)):
                edit = QLineEdit(row)
                edit.setFixedWidth(56)
                edit.setText(_number_text(number) if isinstance(number, (int, float)) else "")
                edit.setToolTip("from" if position == 0 else "to")
                edit.editingFinished.connect(
                    lambda e=edit, i=index, p=position: self._typed(i, p, e.text()))
                line.addWidget(edit)
                if position == 0:
                    dash = QLabel("–", row)
                    line.addWidget(dash)
            chip = Swatch(row, 20)
            chip.set_colour(rgb(colour) if isinstance(colour, str) else None)
            chip.setToolTip(f"{colour} -- click to change")
            chip.picked.connect(lambda c, i=index: self._set(i, 2, c))
            line.addWidget(chip)
            remove = QToolButton(row)
            remove.setObjectName("flat")
            remove.setText("×")
            remove.setToolTip("Remove this zone")
            remove.setStyleSheet(f"padding: 0; color: {style.FAINT}; font-size: 15px;")
            remove.setFixedWidth(22)
            remove.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            remove.clicked.connect(lambda _c=False, i=index: self._remove(i))
            line.addWidget(remove)
            line.addStretch(1)
            self.rows.addWidget(row)
        add = QPushButton("Add zone", self)
        add.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        add.clicked.connect(self._add)
        holder = QWidget(self)
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(add)
        line.addStretch(1)
        self.rows.addWidget(holder)

    def _typed(self, index: int, position: int, text: str) -> None:
        try:
            number: float | int = float(text)
        except ValueError:
            self._rebuild()          # not a number: show what it was
            return
        if number.is_integer():
            number = int(number)
        if self._zones[index][position] != number:
            self._set(index, position, number)

    def _set(self, index: int, position: int, value: Any) -> None:
        self._zones[index][position] = value
        self._emit()

    def _remove(self, index: int) -> None:
        del self._zones[index]
        self._emit()

    def _add(self) -> None:
        low, high = self._range
        span = high - low
        if self._zones:
            top = min(z[0] for z in self._zones if isinstance(z[0], (int, float)))
            zone = [top - span * 0.2, top, "#ffb020"]
        else:
            zone = [low + span * 0.8, high, "#e63232"]
        self._zones.append([_tidy(zone[0]), _tidy(zone[1]), zone[2]])
        self._emit()

    def _emit(self) -> None:
        QTimer.singleShot(0, self, self._rebuild)
        self.changed.emit([list(z) for z in self._zones] or None)


def _tidy(number: float) -> float | int:
    """A proposed bound, rounded to what a person would type."""
    rounded = round(number, 2)
    return int(rounded) if float(rounded).is_integer() else rounded


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
                 parent: QWidget | None = None, *, none_text: str = "none (fixed text)",
                 labels: dict[str, str] | None = None) -> None:
        super().__init__(names, parent)
        self._required = required
        self._none_text = none_text
        # Shown instead of a bare name, e.g. what an image source shows.
        self._labels = labels or {}

    def set_value(self, value: Any, default_label: str = "") -> None:  # type: ignore[override]
        self.blockSignals(True)
        self.clear()
        if not self._required or not value:
            self.addItem("choose a source…" if self._required else self._none_text, None)
        for choice in self._choices:
            self.addItem(self._labels.get(choice, choice), choice)
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
