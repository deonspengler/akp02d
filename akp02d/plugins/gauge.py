"""gauge: a scalar as a swept arc, with optional text in the middle.

    [[widget.gauge]]
    x = 1280; y = 142; width = 160; height = 160
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
from .base import PluginError, Widget, WidgetSpec, register

if TYPE_CHECKING:
    from ..theme import Theme

# Classic gauge: a gap at the bottom, sweeping clockwise from lower left.
START, SPAN = 135.0, 270.0


@register("gauge")
class GaugeWidget(Widget):
    """Options: source, gradient, color_by, track, thickness, label,
    label_color, label_size, format, size, color, font, min, max."""

    default_fps = 1.0

    __slots__ = ("_box", "_by_level", "_draw", "_fill", "_font", "_format",
                 "_label", "_label_fill", "_label_font", "_label_xy", "_last",
                 "_lut", "_range", "_source", "_thickness", "_track",
                 "_value_xy")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        self._source = sources.resolve(options, spec.id, (sources.SCALAR,))
        if self._source is None:
            raise sources.SourceError(f"widget {spec.id!r}: gauge needs a source")
        by = options.string("color_by", "level")
        if by not in ("position", "level"):
            raise options.fail("color_by", "position or level", by)
        self._by_level = by == "level"
        self._lut = gradient.lut(
            options.colors("gradient", ("#22dd66", "#ffd022", "#ff3b30"))
        )
        self._track = options.color("track", "#0a3c46")
        self._thickness = options.integer("thickness", 12, 1, 128)
        low = options.number("min", 0.0)
        high = options.number("max", 1.0)
        if high <= low:
            raise options.fail("max", f"greater than min ({low})", high)
        self._range = (low, high - low)

        self._label = options.string("label", None)
        self._format = options.string("format", None)
        self._label_fill = options.color("label_color", "#c0c4d0")
        self._fill = options.color("color", "#ffffff")
        font = options.string("font", None)
        self._label_font = theme.font(font, options.integer("label_size", 20, 1, 512))
        self._font = theme.font(font, options.integer("size", 14, 1, 512))
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
