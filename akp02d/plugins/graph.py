"""graph: a source's recent history, as a scrolling trace.

One point is added per render, so `fps` sets both the update rate and
the time span. A source with two directions (network) can be drawn as
two traces: mirrored about a centre line, one direction each side, or
overlaid on one axis. The line is coloured by height, so a spike changes
colour.
The change test compares pixel rows, so a flat trace costs nothing even
though a scrolling one redraws every tick that moves it.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from .. import gradient, scale, sources
from ..schema import Auto, Schema, choice, color, colors, integer, number, source
from ..textfx import READING_SHAPES, gradient_options
from .base import Widget, WidgetSpec, register

STYLES = ("line", "mirrored", "overlaid")


def _has_pair(values) -> bool:
    return getattr(values.get("source"), "pair", None) is not None


def _two(values) -> bool:
    return values.get("style") in ("mirrored", "overlaid")


if TYPE_CHECKING:
    from ..theme import Theme


@register("graph")
class GraphWidget(Widget):
    """A source's recent history, as a filled trace."""

    default_fps = 1.0
    summary = "Recent history as a scrolling trace, or two for up and down"
    schema = Schema(
        source(accepts=(sources.SCALAR,), required=True),
        choice("style", STYLES,
               Auto(lambda v, spec: "mirrored" if _has_pair(v) else "line",
                    "mirrored for up and down (network), else line"),
               group="Line",
               doc="line: one trace. mirrored: up above a centre line, down "
                   "below. overlaid: up and down on one axis. The last two "
                   "need a source with two directions (network)"),
        colors("line_color", ("#11e3f5", "#66f0ff"), group="Line",
               doc="One colour, or several for a gradient; mirrored or "
                   "overlaid, the up trace"),
        colors("line_color_down", ("#fd44dd", "#ff99ee"), group="Line", when=_two,
               doc="The down trace, laid out like line_color"),
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
              doc="The baseline (the centre line when mirrored); empty for none"),
        *scale.options(),
    )

    __slots__ = ("_colours", "_fill", "_fill_alpha", "_fill_draw",
                 "_fill_intensity", "_fill_mask", "_history", "_inset",
                 "_last", "_line", "_line_draw", "_line_width",
                 "_mirrored", "_pair", "_scale", "_second", "_source")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        o = self.schema.read(options, spec)
        self._source = o.source
        pair = getattr(self._source, "pair", None)
        if o.style != "line" and pair is None:
            raise options.fail("style", "line for a source with one reading; mirrored "
                               "and overlaid need two directions (network)", o.style)
        self._pair = None if o.style == "line" else pair
        self._mirrored = o.style == "mirrored"
        stops = o.line_color
        down_stops = o.line_color_down
        # Painted once: the trace only changes which part of it shows.
        self._fill = (None if o.fill is None else
                      gradient.paint(tuple(o.fill), spec.rect.size,
                                     o.fill_angle, o.fill_shape).copy())
        # A mask of the area under the trace, drawn into once per frame:
        # the box is re-cleared and the polygon redrawn in place, so the
        # image itself outlives every redraw.
        self._fill_mask = (None if self._fill is None
                           else Image.new("L", spec.rect.size, 0))
        self._fill_draw = (None if self._fill_mask is None
                           else ImageDraw.Draw(self._fill_mask))
        # The fill is decoration over whatever is behind it, so it is the
        # part that wants to be see-through -- a solid block hides the
        # wallpaper the widget is sitting on. The line stays opaque: it
        # is the data. The opacity is pre-multiplied into the mask's
        # polygon, which is binary: painting it at the scaled intensity is
        # the same per-pixel result a point() pass would give, without a
        # second image.
        self._fill_alpha = o.fill_alpha
        self._fill_intensity = int(255 * self._fill_alpha)
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
        self._scale = scale.Scale.read(o, options)
        options.reject_unknown()

        self._history: deque[float] = deque(maxlen=points)
        self._second: deque[float] | None = (
            deque(maxlen=points) if self._pair is not None else None)
        self._inset = self._line_width  # keep the stroke inside the box
        # Each trace's colours, made once: by height (from the centre line
        # out, when mirrored), or across the box. By level there is no image;
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
        if self._pair is not None:
            self._colours.append(colouring(down_stops))
        # One mask and one draw handle for the widget's lifetime.
        self._line = Image.new("L", (width, height), 0)
        self._line_draw = ImageDraw.Draw(self._line)
        self._last: tuple[int, ...] | None = None

    def _rows(self, history, baseline: int, span: int, sign: int) -> tuple[int, ...]:
        """History as pixel rows -- the unit the change test counts."""
        fraction = self._scale.fraction
        return tuple(baseline + sign * int(fraction(value) * span) for value in history)

    def _append(self, value, fields) -> None:
        if self._pair is not None:
            upper, lower = self._pair
            self._history.append(fields.get(upper, 0.0))
            self._second.append(fields.get(lower, 0.0))
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
            bottom = height - 1 - self._inset
            series = [(self._rows(self._history, bottom, span, -1), self._history)]
            if self._second is not None:      # overlaid: both on one axis
                series.append((self._rows(self._second, bottom, span, -1), self._second))
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
                mask, draw = self._fill_mask, self._fill_draw
                # Cleared and redrawn in place; the opacity is in the
                # polygon's value (see __init__).
                draw.rectangle((0, 0, width - 1, height - 1), fill=0)
                draw.polygon([(0, baseline)] + points + [(width - 1, baseline)],
                             fill=self._fill_intensity)
                self.surface.paste(self._fill, (0, 0), mask)
            self._line_draw.rectangle((0, 0, width - 1, height - 1), fill=0)
            if len(points) > 1:
                self._line_draw.line(points, fill=255, width=self._line_width)
            else:
                self._line_draw.point(points, fill=255)
            if image is None:
                image = gradient.colour_at(
                    lut, self._scale.fraction(history[-1]) if history else 0.0)
            self.surface.paste(image, (0, 0), self._line)
        return True
