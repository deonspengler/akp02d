"""gauge: a scalar as a swept arc, with optional text in the middle.

    [[widget.gauge]]
    x = 1280
    y = 142
    width = 160
    height = 160
    fps = 1
    source = "memory"
    label = "MEM"
    format = "{used_gb:.0f}/{total_gb:.0f}G"
    gradient = ["#22dd66", "#ffd022", "#ff3b30"]

The arc takes one colour, picked from the gradient by the value it is
showing, so a full gauge is red at a glance without anyone reading the
sweep. `color_by = "position"` instead runs the ramp around the arc,
which looks better but tells you less: at low values only the first stop
is ever visible.

The arc is drawn at widget size, so its ends are stepped. Supersampling
would smooth them, and encode slightly smaller into the bargain, but it
costs a resize per redraw; at the rate a gauge changes that would be
affordable, and it is simply not done yet.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PIL import ImageDraw

from .. import gradient, sources
from ..schema import (
    Auto, Schema, choice, color, colors, font, integer, number, source, string,
    text_format,
)
from .base import PluginError, Widget, WidgetSpec, register

if TYPE_CHECKING:
    from ..theme import Theme

def _range(values) -> tuple[float, float]:
    """The source's own usual range: 0..1 for fractions, degrees for a
    temperature."""
    return getattr(values.get("source"), "range", (0.0, 1.0))


# Classic gauge: a gap at the bottom, sweeping clockwise from lower left.
START, SPAN = 135.0, 270.0


@register("gauge")
class GaugeWidget(Widget):
    """A scalar as a swept arc, with optional text in the middle."""

    default_fps = 1.0
    summary = "A reading as a swept arc, with a label and value inside"
    schema = Schema(
        source(accepts=(sources.SCALAR,), required=True),
        choice("color_by", ("position", "level"), "level", group="Arc",
               doc="level: one colour picked by the value; position: the "
                   "ramp runs round the arc"),
        colors("gradient", ("#22dd66", "#ffd022", "#ff3b30"), group="Arc"),
        color("track", "#0a3c46", group="Arc",
              doc="The unfilled part of the arc"),
        integer("thickness", 12, 1, 128, group="Arc"),
        number("min", Auto(lambda v, spec: _range(v)[0], "the source's usual low"),
               group="Data", doc="Value drawn as empty"),
        number("max", Auto(lambda v, spec: _range(v)[1], "the source's usual high"),
               group="Data", doc="Value drawn as full"),
        string("label", None, group="Text", doc="Caption above the middle"),
        text_format("format", None, group="Data",
                    doc="Value text below the middle, filled from the "
                        "source's fields"),
        color("label_color", "#c0c4d0", group="Text"),
        color("color", "#ffffff", group="Text", doc="Colour of the value text"),
        font(group="Text"),
        integer("label_size", 20, 1, 512, group="Text"),
        integer("size", 14, 1, 512, group="Text", doc="Size of the value text"),
    )

    __slots__ = ("_box", "_by_level", "_draw", "_fill", "_font", "_format",
                 "_label", "_label_fill", "_label_font", "_label_xy", "_last",
                 "_lut", "_range", "_source", "_thickness", "_track",
                 "_value_xy")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        o = self.schema.read(options, spec)
        self._source = o.source
        self._by_level = o.color_by == "level"
        self._lut = gradient.lut(o.gradient)
        self._track = o.track
        self._thickness = o.thickness
        low, high = o.min, o.max
        if high <= low:
            raise options.fail("max", f"greater than min ({low})", high)
        self._range = (low, high - low)

        self._label = o.label
        self._format = o.format
        self._label_fill = o.label_color
        self._fill = o.color
        self._label_font = theme.font(o.font, o.label_size)
        self._font = theme.font(o.font, o.size)
        options.reject_unknown()

        width, height = spec.rect.size
        pad = self._thickness // 2 + 1
        self._box = (pad, pad, width - pad - 1, height - pad - 1)
        # The two lines sit either side of the middle, so the pair reads
        # as centred even though neither line is.
        self._label_xy = (width // 2, height // 2 - 2)
        self._value_xy = (width // 2, height // 2 + 2)
        self._draw = ImageDraw.Draw(self.surface)
        self._last: tuple | None = None
        # Format against a real sample now, so one that does not fit its
        # source fails at load rather than on the first frame.
        self._text(self._source.read(time.monotonic())[1])

    def _text(self, fields) -> str | None:
        if not self._format:
            return None
        try:
            return self._format.format(**fields)
        except (KeyError, ValueError, TypeError) as exc:
            raise PluginError(
                f"widget {self.id!r}: format {self._format!r} does not fit "
                f"this source ({exc}); it offers {', '.join(sorted(fields))}"
            ) from exc

    def render(self, now: float) -> bool:
        value, fields = self._source.read(now)
        low, size = self._range
        fraction = min(1.0, max(0.0, ((value or 0.0) - low) / size))
        # Whole degrees: the arc cannot show finer than that, so a value
        # wobbling below one degree redraws nothing.
        degrees = round(SPAN * fraction)
        text = self._text(fields)
        state = (degrees, text)
        if state == self._last:
            return False
        self._last = state

        self.clear()
        self._draw.arc(self._box, START, START + SPAN, fill=self._track,
                       width=self._thickness)
        if degrees:
            if self._by_level:
                self._draw.arc(self._box, START, START + degrees,
                               fill=gradient.colour_at(self._lut, fraction),
                               width=self._thickness)
            else:
                # Two degrees at a time, coloured by angle, so the ramp
                # runs round the arc rather than picking one colour.
                for step in range(0, degrees, 2):
                    self._draw.arc(
                        self._box, START + step, START + min(step + 2.6, degrees),
                        fill=gradient.colour_at(self._lut, step / SPAN),
                        width=self._thickness,
                    )
        if self._label:
            self._draw.text(self._label_xy, self._label, font=self._label_font,
                            fill=self._label_fill, anchor="md")
        if text:
            self._draw.text(self._value_xy, text, font=self._font,
                            fill=self._fill, anchor="ma")
        return True
