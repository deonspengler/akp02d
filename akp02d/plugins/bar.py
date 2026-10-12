"""bar: a reading as a filling bar, with an optional label and value.

    [[widget.bar]]
    x = 56
    y = 400
    width = 344
    height = 48
    source = "memory"
    label = "MEM"
    format = "{percent:.0f}%"
    gradient = ["#22dd66", "#ffd022", "#ff3b30"]

The gauge's straight cousin: a stack of these reads well on a tall
panel where round gauges would waste the sides. Horizontal fills left
to right, vertical bottom to top; which one is picked from the box's
shape unless `direction` says.

`color_by = "position"` (the default here) fixes the gradient along the
bar, so a fuller bar reaches further into it; "level" paints the whole
fill one colour picked by the value, so a nearly-full bar is red at a
glance. `segments` splits the bar into separate cells, like an LED
meter, and a value then moves in whole cells -- which also means a
reading wobbling inside one cell costs no redraw at all.

The track (the empty part) is drawn into the backdrop once, so a redraw
is a paste of the fill through a mask, plus the text if it changed.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from .. import gradient, sources
from ..schema import (
    Auto, Schema, choice, color, colors, font, integer, number, source, string,
    text_format,
)
from .base import PluginError, Widget, WidgetSpec, register

if TYPE_CHECKING:
    from ..theme import Theme


def _range(values) -> tuple[float, float]:
    return getattr(values.get("source"), "range", (0.0, 1.0))


def _has_text(values) -> bool:
    return bool(values.get("label") or values.get("format"))


@register("bar")
class BarWidget(Widget):
    """A scalar as a filling bar."""

    default_fps = 1.0
    summary = "A reading as a filling bar, horizontal or vertical"
    schema = Schema(
        source(accepts=(sources.SCALAR,), required=True),
        number("min", Auto(lambda v, spec: _range(v)[0], "the source's usual low"),
               group="Data", doc="Value drawn as empty"),
        number("max", Auto(lambda v, spec: _range(v)[1], "the source's usual high"),
               group="Data", doc="Value drawn as full"),
        text_format("format", None, group="Data",
                    doc="Value text, filled from the source's fields"),
        choice("direction", ("horizontal", "vertical"),
               Auto(lambda v, spec: "horizontal" if spec.rect.width >= spec.rect.height
                    else "vertical", "horizontal for a wide box, vertical for a tall one"),
               group="Bar"),
        choice("color_by", ("position", "level"), "position", group="Bar",
               doc="position: the gradient is fixed along the bar; level: the "
                   "fill takes one colour by how full it is"),
        colors("gradient", ("#22dd66", "#ffd022", "#ff3b30"), group="Bar"),
        color("track", "#0a3c46", group="Bar", doc="The empty part of the bar"),
        integer("radius", 0, 0, 256, group="Bar",
                doc="Corner rounding in pixels; half the bar's thickness "
                    "gives round ends"),
        integer("segments", 0, 0, 200, group="Bar",
                doc="Split into this many cells, like an LED meter; 0 for a "
                    "continuous bar"),
        integer("segment_gap", 2, 0, 64, group="Bar",
                when=lambda v: bool(v.get("segments"))),
        string("label", None, group="Text", doc="Caption beside the value"),
        choice("text", ("above", "inside"), "above", group="Text", when=_has_text,
               doc="above: a line over the bar (label left, value right); "
                   "inside: centred on the bar"),
        color("label_color", "#c0c4d0", group="Text"),
        color("color", "#ffffff", group="Text", doc="Colour of the value text"),
        font(group="Text"),
        integer("size", 16, 1, 512, group="Text"),
    )

    __slots__ = ("_bar", "_by_level", "_cells", "_draw", "_fill_image", "_font",
                 "_format", "_label", "_label_fill", "_last", "_length", "_lut",
                 "_mask", "_mask_draw", "_radius", "_range", "_source", "_text_at",
                 "_text_fill", "_text_mode", "_vertical")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        o = self.schema.read(options, spec)
        self._source = o.source
        low, high = o.min, o.max
        if high <= low:
            raise options.fail("max", f"greater than min ({low})", high)
        self._range = (low, high - low)
        self._format = o.format
        self._label = o.label
        self._vertical = o.direction == "vertical"
        self._by_level = o.color_by == "level"
        self._lut = gradient.lut(o.gradient)
        self._label_fill = o.label_color
        self._text_fill = o.color
        self._font = theme.font(o.font, o.size)
        self._radius = o.radius
        options.reject_unknown()

        width, height = spec.rect.size
        has_text = bool(self._label or self._format)
        self._text_mode = o.text if has_text else None
        ascent, descent = self._font.getmetrics()
        line = ascent + descent
        # Where the bar goes, and where its text goes.
        top, bottom = 0, height
        self._text_at: dict[str, tuple[tuple[int, int], str]] = {}
        if self._text_mode == "above":
            if self._vertical:
                # A tall bar has no room beside it: value on top, label under.
                top = line + 4
                self._text_at["value"] = ((width // 2, ascent), "ms")
                if self._label:
                    bottom = height - line - 4
                    self._text_at["label"] = ((width // 2, height - descent), "ms")
            else:
                top = line + 4
                self._text_at["label"] = ((0, ascent), "ls")
                self._text_at["value"] = ((width, ascent), "rs")
        elif self._text_mode == "inside":
            self._text_at["inside"] = ((width // 2, height // 2), "mm")
        if bottom - top < 2:
            raise PluginError(
                f"widget {spec.id!r}: no room for the bar under its text; "
                f"make the box taller or the text smaller")
        self._bar = (0, top, width, bottom)
        bar_w, bar_h = width, bottom - top
        self._length = bar_h if self._vertical else bar_w

        # The cells: one for a continuous bar, `segments` otherwise.
        count = o.segments or 0
        gap = o.segment_gap
        if count:
            cell = (self._length - gap * (count - 1)) / count
            if cell < 1:
                raise PluginError(
                    f"widget {spec.id!r}: {count} segments with a gap of {gap} do "
                    f"not fit in {self._length} pixels")
            spans = [(round(i * (cell + gap)), round(i * (cell + gap) + cell))
                     for i in range(count)]
        else:
            spans = [(0, self._length)]
        self._cells = [self._cell_box(start, end) for start, end in spans]

        # The empty bar, drawn once into the backdrop.
        track_draw = ImageDraw.Draw(self.backdrop)
        for box in self._cells:
            self._shape(track_draw, box, o.track)
        self.surface.paste(self.backdrop)

        # The fill colour field: the gradient along the bar, built once.
        if not self._by_level:
            strip_len = self._length
            positions = [min(255, i * 255 // max(strip_len - 1, 1)) for i in range(strip_len)]
            if self._vertical:
                strip = Image.new("L", (1, strip_len))
                strip.putdata(positions[::-1])  # bottom is the start
            else:
                strip = Image.new("L", (strip_len, 1))
                strip.putdata(positions)
            field = strip.resize((bar_w, bar_h), Image.Resampling.NEAREST)
            ramp = Image.merge("RGB", tuple(field.point(t) for t in self._lut))
            self._fill_image = Image.new("RGB", (width, height))
            self._fill_image.paste(ramp, (0, top))
        else:
            self._fill_image = None
        self._mask = Image.new("L", (width, height), 0)
        self._mask_draw = ImageDraw.Draw(self._mask)
        self._draw = ImageDraw.Draw(self.surface)
        self._last: tuple | None = None
        # Format against a real sample now, so one that does not fit its
        # source fails at load rather than on the first frame.
        self._text(self._source.read(time.monotonic())[1])

    def _cell_box(self, start: int, end: int) -> tuple[int, int, int, int]:
        """A cell's box, `start`..`end` along the bar from its empty end."""
        x0, y0, x1, y1 = self._bar
        if self._vertical:
            return (x0, y1 - end, x1 - 1, y1 - start - 1)
        return (x0 + start, y0, x0 + end - 1, y1 - 1)

    def _shape(self, draw: ImageDraw.ImageDraw, box, fill) -> None:
        x0, y0, x1, y1 = box
        if x1 < x0 or y1 < y0:
            return
        radius = min(self._radius, (x1 - x0 + 1) // 2, (y1 - y0 + 1) // 2)
        if radius:
            draw.rounded_rectangle(box, radius, fill=fill)
        else:
            draw.rectangle(box, fill=fill)

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
        if len(self._cells) > 1:
            filled = round(fraction * len(self._cells))  # whole cells
        else:
            filled = round(fraction * self._length)      # whole pixels
        text = self._text(fields)
        state = (filled, text)
        if state == self._last:
            return False
        self._last = state

        self.clear()
        mask_draw = self._mask_draw
        mask_draw.rectangle((0, 0, *self._mask.size), fill=0)
        if len(self._cells) > 1:
            for box in self._cells[:filled]:
                self._shape(mask_draw, box, 255)
        elif filled:
            # The whole bar's shape, cut back to the fill: the start keeps
            # its rounded end, and the end rounds too once it is full.
            self._shape(mask_draw, self._cells[0], 255)
            x0, y0, x1, y1 = self._bar
            if self._vertical:
                mask_draw.rectangle((x0, y0, x1, y1 - filled - 1), fill=0)
            else:
                mask_draw.rectangle((x0 + filled, y0, x1, y1), fill=0)
        if filled:
            if self._fill_image is not None:
                self.surface.paste(self._fill_image, (0, 0), self._mask)
            else:
                colour = gradient.colour_at(self._lut, fraction)
                self.surface.paste(colour, (0, 0), self._mask)

        draw, font = self._draw, self._font
        if "inside" in self._text_at:
            xy, anchor = self._text_at["inside"]
            words = " ".join(part for part in (self._label, text) if part)
            draw.text(xy, words, font=font, fill=self._text_fill, anchor=anchor)
        else:
            if self._label and "label" in self._text_at:
                xy, anchor = self._text_at["label"]
                draw.text(xy, self._label, font=font, fill=self._label_fill, anchor=anchor)
            if text and "value" in self._text_at:
                xy, anchor = self._text_at["value"]
                draw.text(xy, text, font=font, fill=self._text_fill, anchor=anchor)
        return True
