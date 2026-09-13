"""graph: a source's recent history, as a filled trace.

    [[widget.graph]]
    x = 32; y = 300; width = 400; height = 110
    fps = 1
    source = "cpu"
    gradient = ["#11e3f5", "#fd44dd"]
    fill = "#18243c"
    fill_alpha = 0.5

A source that produces a pair -- network, with a rate in each direction
-- is drawn about a centre line instead, one direction above and the
other below:

    [[widget.graph]]
    source = "network"
    scale = "log"
    max = 125000000                # a gigabit, in bytes per second

Rates like those span orders of magnitude: idle traffic against a large
download is a factor of a thousand, and on a linear scale the idle half
of the day is a flat line. A log scale keeps every phase legible at
once, at the cost of understating how much bigger the big one is, so it
is the default for a pair and linear is the default for a scalar.

The line is coloured by height, so a spike reads as a different colour
without anyone having to look at an axis -- the same reasoning that made
the gauge and the radial spectrum colour by value. Colour by horizontal
position was tried and rejected: it encodes time, which the x-axis
already shows, and makes equal readings look like different ones.

One point is appended per render, so `fps` sets both the update rate and
the time axis: a 400-point graph at fps = 1 is about seven minutes.

This is the one widget the change test cannot make free. A scrolling
trace moves every pixel, so it redraws on every tick that shifts it --
though a flat line quantises to the same pixel rows and still costs
nothing, which is why the comparison is on rows rather than on values.
"""

from __future__ import annotations

import math
from collections import deque
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from .. import gradient, sources
from ..table import parse_color
from .base import Widget, WidgetSpec, register

if TYPE_CHECKING:
    from ..theme import Theme


@register("graph")
class GraphWidget(Widget):
    """Options: source, gradient, gradient_down, scale, origin, fill,
    fill_alpha, line_width, points, min, max."""

    default_fps = 1.0

    __slots__ = ("_fill", "_fill_alpha", "_gradient", "_history", "_inset",
                 "_last", "_line", "_line_draw", "_line_width", "_log",
                 "_mirrored", "_range", "_second", "_second_gradient",
                 "_source")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        self._source = sources.resolve(
            options, spec.id, (sources.SCALAR, sources.PAIR)
        )
        if self._source is None:
            raise sources.SourceError(f"widget {spec.id!r}: graph needs a source")
        self._mirrored = self._source.kind == sources.PAIR
        stops = options.colors("gradient", ("#11e3f5", "#66f0ff"))
        down_stops = options.colors("gradient_down", ("#fd44dd", "#ff99ee"))
        fill = options.string("fill", None)
        self._fill = options.color("fill", "#18243c") if fill else None
        # The fill is decoration over whatever is behind it, so it is the
        # part that wants to be see-through -- a solid block hides the
        # wallpaper the widget is sitting on. The line stays opaque: it
        # is the data.
        self._fill_alpha = options.number("fill_alpha", 1.0, 0.0, 1.0)
        self._line_width = options.integer("line_width", 2, 1, 16)
        width, height = spec.rect.size
        points = options.integer("points", width, 2, 4096)
        origin = options.string("origin", None)
        if origin is not None:
            # Into the backdrop, so it costs nothing per frame and is
            # still there when the trace is flat. A mirrored graph needs
            # it most: without a line the two directions have no visible
            # axis to grow from.
            row = height // 2 if self._mirrored else height - 1 - self._line_width
            ImageDraw.Draw(self.backdrop).line(
                (0, row, width - 1, row), fill=parse_color(origin, options.path("origin"))
            )
            self.surface.paste(self.backdrop)
        # A source reporting outside 0..1 -- a rate, a temperature --
        # needs the range it is drawn against; that is a display
        # decision, so it belongs on the widget rather than the source.
        scale = options.string("scale", "log" if self._mirrored else "linear")
        if scale not in ("linear", "log"):
            raise options.fail("scale", "linear or log", scale)
        self._log = scale == "log"
        # For a log scale `min` is the floor -- what counts as nothing --
        # rather than the bottom of a range, so it cannot be zero.
        low = options.number("min", 1024.0 if self._log else 0.0)
        high = options.number("max", 125_000_000.0 if self._mirrored else 1.0)
        if high <= low:
            raise options.fail("max", f"greater than min ({low})", high)
        if self._log and low <= 0:
            raise options.fail("min", "greater than zero on a log scale", low)
        options.reject_unknown()
        self._range = (low, high - low)

        self._history: deque[float] = deque(maxlen=points)
        self._second: deque[float] = deque(maxlen=points) if self._mirrored else None
        self._inset = self._line_width  # keep the stroke inside the box
        # Mirrored traces grow away from the middle, so their ramp runs
        # from the centre line outward rather than bottom to top.
        where = "centre" if self._mirrored else "up"
        self._gradient = gradient.image(stops, (width, height), where)
        self._second_gradient = (
            gradient.image(down_stops, (width, height), where)
            if self._mirrored else None
        )
        # One mask and one draw handle for the widget's lifetime.
        self._line = Image.new("L", (width, height), 0)
        self._line_draw = ImageDraw.Draw(self._line)
        self._last: tuple[int, ...] | None = None

    def _fraction(self, value: float) -> float:
        low, size = self._range
        if self._log:
            if value <= low:
                return 0.0
            return min(1.0, math.log(value / low) / math.log((low + size) / low))
        return min(1.0, max(0.0, (value - low) / size))

    def _rows(self, history, baseline: int, span: int, sign: int) -> tuple[int, ...]:
        """History as pixel rows -- the unit the change test counts."""
        return tuple(
            baseline + sign * int(self._fraction(value) * span) for value in history
        )

    def render(self, now: float) -> bool:
        value, fields = self._source.read(now)
        width, height = self.surface.size
        if self._mirrored:
            self._history.append(fields.get("up", 0.0))
            self._second.append(fields.get("down", 0.0))
            middle = height // 2
            span = middle - self._inset
            series = [
                (self._rows(self._history, middle, span, -1), self._gradient),
                (self._rows(self._second, middle, span, 1), self._second_gradient),
            ]
            baseline = middle
        else:
            self._history.append(value if value is not None else 0.0)
            span = height - 1 - self._inset * 2
            series = [
                (self._rows(self._history, height - 1 - self._inset, span, -1),
                 self._gradient)
            ]
            baseline = height - 1

        state = tuple(rows for rows, _ in series)
        if state == self._last:
            return False
        self._last = state

        self.clear()
        for rows, ramp in series:
            step = (width - 1) / max(len(rows) - 1, 1)
            points = [(index * step, row) for index, row in enumerate(rows)]
            if self._fill is not None and len(points) > 1:
                mask = _area((width, height), points, baseline)
                if self._fill_alpha < 1.0:
                    # A mask is a per-pixel blend, not just a stencil, so
                    # scaling it down blends the fill with the backdrop.
                    scale = self._fill_alpha
                    mask = mask.point(lambda value: int(value * scale))
                self.surface.paste(
                    Image.new("RGB", (width, height), self._fill), (0, 0), mask
                )
            self._line_draw.rectangle((0, 0, width - 1, height - 1), fill=0)
            if len(points) > 1:
                self._line_draw.line(points, fill=255, width=self._line_width)
            else:
                self._line_draw.point(points, fill=255)
            self.surface.paste(ramp, (0, 0), self._line)
        return True


def _area(
    size: tuple[int, int], points: list[tuple[float, float]], baseline: int
) -> Image.Image:
    """A mask of everything between the trace and its baseline.

    Full widget size, because it is pasted against the widget: a
    mirrored trace fills only half of it, above or below the centre.
    """
    width, _ = size
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(
        [(0, baseline)] + points + [(width - 1, baseline)], fill=255
    )
    return mask
