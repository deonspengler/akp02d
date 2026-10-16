"""graph: a source's recent history, as a scrolling trace.

One point is added per render, so `fps` sets both the update rate and
the time span. A pair (network) is drawn about a centre line, one
direction each side, on a log scale by default: rates span orders of
magnitude. The line is coloured by height, so a spike changes colour.
The change test compares pixel rows, so a flat trace costs nothing even
though a scrolling one redraws every tick that moves it.
"""

from __future__ import annotations

import math
from collections import deque
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from .. import gradient, sources
from ..schema import Auto, Schema, choice, color, colors, integer, number, source
from ..textfx import READING_SHAPES, gradient_options
from .base import Widget, WidgetSpec, register


def _pair(values) -> bool:
    found = values.get("source")
    return getattr(found, "kind", None) == sources.PAIR

if TYPE_CHECKING:
    from ..theme import Theme


@register("graph")
class GraphWidget(Widget):
    """A source's recent history, as a filled trace."""

    default_fps = 1.0
    summary = "Recent history as a scrolling trace; a pair (network) mirrors"
    schema = Schema(
        source(accepts=(sources.SCALAR, sources.PAIR), required=True),
        colors("line_color", ("#11e3f5", "#66f0ff"), group="Line",
               doc="One colour, or several for a gradient; for a pair, the "
                   "upper (up) trace"),
        colors("line_color_down", ("#fd44dd", "#ff99ee"), group="Line", when=_pair,
               doc="The lower (down) trace of a pair, laid out like line_color"),
        *gradient_options("line_color", group="Line", subject="the graph",
                          shapes=READING_SHAPES, default_shape="scale"),
        colors("fill", None, group="Fill",
               doc="Colour under the line, or several for a gradient; empty "
                   "for none"),
        *gradient_options("fill", group="Fill", subject="the fill"),
        number("fill_alpha", 1.0, 0.0, 1.0, group="Fill",
               when=lambda v: v.get("fill") is not None,
               doc="Opacity of the fill, so the wallpaper shows through"),
        integer("line_width", 2, 1, 16, group="Line"),
        integer("points", Auto(lambda v, spec: spec.rect.width,
                               "one per pixel of width"),
                2, 4096, group="Data",
                doc="How many readings fit across; with fps this sets the "
                    "time span"),
        color("origin", None, group="Line",
              doc="The baseline (or centre line for a pair); empty for none"),
        choice("scale", ("linear", "log"),
               Auto(lambda v, spec: "log" if _pair(v) else "linear",
                    "log for a pair, else linear"),
               group="Data"),
        number("min", Auto(lambda v, spec: 1024.0 if v.scale == "log"
                           else getattr(v.get("source"), "range", (0.0, 1.0))[0],
                           "1024 on a log scale, else the source's usual low"),
               group="Data",
               doc="Bottom of the range; on a log scale, what counts as "
                   "nothing"),
        number("max", Auto(lambda v, spec: 125_000_000.0 if _pair(v)
                           else getattr(v.get("source"), "range", (0.0, 1.0))[1],
                           "a gigabit/s for a pair, else the source's usual high"),
               group="Data", doc="Top of the range"),
    )

    __slots__ = ("_colours", "_fill", "_fill_alpha", "_history", "_inset",
                 "_last", "_line", "_line_draw", "_line_width", "_log",
                 "_mirrored", "_range", "_second",
                 "_source")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        o = self.schema.read(options, spec)
        self._source = o.source
        self._mirrored = self._source.kind == sources.PAIR
        stops = o.line_color
        down_stops = o.line_color_down
        # Painted once: the trace only changes which part of it shows.
        self._fill = (None if o.fill is None else
                      gradient.paint(tuple(o.fill), spec.rect.size,
                                     o.fill_angle, o.fill_shape).copy())
        # The fill is decoration over whatever is behind it, so it is the
        # part that wants to be see-through -- a solid block hides the
        # wallpaper the widget is sitting on. The line stays opaque: it
        # is the data.
        self._fill_alpha = o.fill_alpha
        self._line_width = o.line_width
        width, height = spec.rect.size
        points = o.points
        origin = o.origin
        if origin is not None:
            # Into the backdrop, so it costs nothing per frame and is
            # still there when the trace is flat. A mirrored graph needs
            # it most: without a line the two directions have no visible
            # axis to grow from.
            row = height // 2 if self._mirrored else height - 1 - self._line_width
            ImageDraw.Draw(self.backdrop).line((0, row, width - 1, row), fill=origin)
            self.surface.paste(self.backdrop)
        # A source reporting outside 0..1 -- a rate, a temperature --
        # needs the range it is drawn against; that is a display
        # decision, so it belongs on the widget rather than the source.
        self._log = o.scale == "log"
        # For a log scale `min` is the floor -- what counts as nothing --
        # rather than the bottom of a range, so it cannot be zero.
        low, high = o.min, o.max
        if high <= low:
            raise options.fail("max", f"greater than min ({low})", high)
        if self._log and low <= 0:
            raise options.fail("min", "greater than zero on a log scale", low)
        options.reject_unknown()
        self._range = (low, high - low)

        self._history: deque[float] = deque(maxlen=points)
        self._second: deque[float] = deque(maxlen=points) if self._mirrored else None
        self._inset = self._line_width  # keep the stroke inside the box
        # Each trace's colours, made once: by height (from the centre line
        # out, for a pair), or across the box. By level there is no image;
        # the line takes the colour of its newest reading.
        shape = o.line_color_shape if len(stops) > 1 or len(down_stops) > 1 else "scale"
        where = "centre" if self._mirrored else "up"

        def colouring(trace_stops) -> tuple[Image.Image | None, tuple]:
            if shape == "scale":
                return gradient.image(trace_stops, (width, height), where), ()
            if shape in gradient.SHAPES:
                return gradient.paint(tuple(trace_stops), (width, height),
                                      o.line_color_angle, shape), ()
            return None, gradient.lut(trace_stops)

        self._colours = [colouring(stops)]
        if self._mirrored:
            self._colours.append(colouring(down_stops))
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

    def _append(self, value, fields) -> None:
        if self._mirrored:
            self._history.append(fields.get("up", 0.0))
            self._second.append(fields.get("down", 0.0))
        else:
            self._history.append(value if value is not None else 0.0)

    def seed(self, now: float, period: float) -> None:
        """Fill the history by reading the source at earlier moments.

        For tools that can read any moment -- an editor previewing with
        simulated data -- so a new graph shows a full trace at once
        rather than one point a second. The daemon never calls it: a
        real source only knows now, and delta sources would be wrong.
        """
        count = self._history.maxlen or 0
        self._history.clear()
        if self._second is not None:
            self._second.clear()
        for step in range(count - 1, 0, -1):
            self._append(*self._source.read(now - step * period))

    def render(self, now: float) -> bool:
        value, fields = self._source.read(now)
        width, height = self.surface.size
        self._append(value, fields)
        if self._mirrored:
            middle = height // 2
            span = middle - self._inset
            series = [
                (self._rows(self._history, middle, span, -1), self._history),
                (self._rows(self._second, middle, span, 1), self._second),
            ]
            baseline = middle
        else:
            span = height - 1 - self._inset * 2
            series = [
                (self._rows(self._history, height - 1 - self._inset, span, -1),
                 self._history)
            ]
            baseline = height - 1

        state = tuple(rows for rows, _ in series)
        if state == self._last:
            return False
        self._last = state

        self.clear()
        for (rows, history), (image, lut) in zip(series, self._colours):
            step = (width - 1) / max(len(rows) - 1, 1)
            points = [(index * step, row) for index, row in enumerate(rows)]
            if self._fill is not None and len(points) > 1:
                mask = _area((width, height), points, baseline)
                if self._fill_alpha < 1.0:
                    # A mask is a per-pixel blend, not just a stencil, so
                    # scaling it down blends the fill with the backdrop.
                    mask = mask.point(lambda value, k=self._fill_alpha: int(value * k))
                self.surface.paste(self._fill, (0, 0), mask)
            self._line_draw.rectangle((0, 0, width - 1, height - 1), fill=0)
            if len(points) > 1:
                self._line_draw.line(points, fill=255, width=self._line_width)
            else:
                self._line_draw.point(points, fill=255)
            if image is None:
                image = gradient.colour_at(lut, self._fraction(history[-1]) if history else 0.0)
            self.surface.paste(image, (0, 0), self._line)
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
