"""gauge: a reading as a swept arc, a needle dial, or an analog meter.

Everything that does not move -- face, rim, track, zones, ticks,
numbers -- is drawn once, supersampled, into the backdrop. The arc's
fill is drawn once too, with a map of how far round each pixel lies, so
showing a value is one clamped map of that image rather than a redraw.
The needle is supersampled over its own bounding box. With `glide` the
widget is visited at ANIM_FPS while the reading is still taken at `fps`.

Angles are Pillow's: degrees clockwise from three o'clock. The sweep is
signed (negative runs counterclockwise), so a point along the scale is
always start + sweep * fraction.
"""

from __future__ import annotations

import math
import time
from functools import lru_cache
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .. import gradient, sources
from ..schema import (
    Auto, Schema, boolean, choice, color, colors, font, integer, number, source,
    string, text_format, zones,
)
from ..textfx import READING_SHAPES, gradient_options
from .base import PluginError, Widget, WidgetSpec, register

if TYPE_CHECKING:
    from ..theme import Theme

STYLES = ("arc", "dial", "meter")
# Supersampling for what is drawn smooth: the face once, the needle per frame.
SS = 4
# How often a gliding needle is redrawn while it moves.
ANIM_FPS = 30.0


def _range(values) -> tuple[float, float]:
    """The source's own usual range: 0..1 for fractions, degrees for a
    temperature."""
    return getattr(values.get("source"), "range", (0.0, 1.0))


def _style(values) -> str:
    style = values.get("style")
    return style if style in STYLES else "arc"


def _per(arc, dial, meter, describe: str) -> Auto:
    """A default that depends on the style."""
    table = {"arc": arc, "dial": dial, "meter": meter}

    def compute(values, spec):
        value = table[_style(values)]
        return value(values, spec) if callable(value) else value

    return Auto(compute, describe)


def _short(spec) -> int:
    return min(spec.rect.width, spec.rect.height)


def _is_arc(values) -> bool:
    return _style(values) == "arc"


def _has_needle(values) -> bool:
    return not _is_arc(values) or bool(values.get("needle"))


def _has_ticks(values) -> bool:
    ticks = values.get("ticks")
    return isinstance(ticks, int) and ticks > 0


def _shows_text(values) -> bool:
    return bool(values.get("label") or values.get("format"))


@register("gauge")
class GaugeWidget(Widget):
    """A scalar as an arc, a needle dial, or an analog meter."""

    default_fps = 1.0
    summary = "A reading as a swept arc, a needle dial, or an analog meter"
    schema = Schema(
        source(accepts=(sources.SCALAR,), required=True),
        number("min", Auto(lambda v, spec: _range(v)[0], "the source's usual low"),
               group="Data", doc="Value drawn as empty"),
        number("max", Auto(lambda v, spec: _range(v)[1], "the source's usual high"),
               group="Data", doc="Value drawn as full"),
        text_format("format", None, group="Data",
                    doc="Value text, filled from the source's fields"),
        choice("style", STYLES, "arc", group="Gauge",
               doc="arc: a filling arc; dial: a round face with ticks and a "
                   "needle; meter: a cream meter face with a needle pivoting below"),
        number("sweep", _per(270.0, 270.0, 90.0, "270; 90 for meter"), 30.0, 360.0,
               group="Gauge", doc="Degrees the scale spans"),
        choice("start", ("bottom", "top"), "bottom", group="Gauge", beside="sweep",
               when=lambda v: _style(v) != "meter",
               doc="Where the scale's ends are: bottom, with the gap at the "
                   "bottom like a car's dial, or top. A full circle starts there"),
        choice("direction", ("clockwise", "counterclockwise"), "clockwise",
               group="Gauge", beside="sweep",
               doc="Which way the scale runs from its start"),
        zones(group="Gauge",
              doc="Ranges of the reading drawn in a colour, as [from, to, "
                  "colour] in the same units as min and max: a red line"),
        number("glide", 0.0, 0.0, 5.0, group="Gauge",
               doc="Seconds the needle (or arc) takes to swing to a new "
                   "reading; 0 jumps. Redraws while it moves"),
        # -- arc --
        colors("fill", ("#22dd66", "#ffd022", "#ff3b30"), group="Arc", when=_is_arc,
               doc="The filled part of the arc: one colour, or several for a gradient"),
        *gradient_options("fill", group="Arc", subject="the gauge", shapes=READING_SHAPES,
                          default_shape="level", when=_is_arc),
        color("track", "#0a3c46", group="Arc", when=_is_arc,
              doc="The unfilled part of the arc"),
        integer("thickness", 12, 1, 128, group="Arc", when=_is_arc),
        choice("caps", ("flat", "round"), "flat", group="Arc", when=_is_arc,
               doc="The arc's two ends: cut square, or rounded"),
        choice("tip", ("flat", "round"), "flat", group="Arc", when=_is_arc,
               doc="The fill's moving end, at the value: cut square, or rounded"),
        boolean("needle", False, group="Arc", when=_is_arc,
                doc="A needle pointing at the value, coloured like the arc there"),
        # -- the scale --
        color("face", _per(None, "#12161e", "#ece0be", "dark for dial; cream for meter"),
              group="Scale", when=lambda v: not _is_arc(v)),
        color("rim", _per(None, "#3c4452", "#5a4628", "a shade of the face"),
              group="Scale", when=lambda v: not _is_arc(v)),
        integer("ticks", _per(0, 5, 5, "0 on an arc; 5 on dial and meter"), 0, 50,
                group="Scale", doc="Major divisions of the scale; 0 for none"),
        integer("minor_ticks", 4, 0, 20, group="Scale", when=_has_ticks,
                doc="Small marks between two major ones"),
        boolean("numbers", _per(False, True, True, "off on an arc; on otherwise"),
                group="Scale", when=_has_ticks,
                doc="A number at each major mark; a reading in fractions (0..1) is numbered as a percentage"),
        color("scale_color", _per("#5a6478", "#e6e8ec", "#2a221c", "suits the face"),
              group="Scale", when=lambda v: _has_ticks(v) or not _is_arc(v)),
        integer("number_size",
                _per(lambda v, s: max(8, round(_short(s) * 0.06)),
                     lambda v, s: max(8, round(_short(s) * 0.042)),
                     lambda v, s: max(8, round(s.rect.height * 0.067)),
                     "scaled to the gauge"), 4, 256,
                group="Scale", when=lambda v: _has_ticks(v) and bool(v.get("numbers"))),
        # -- the needle --
        colors("needle_color", _per(lambda v, s: _hexes(v.get("fill")), ("#ff483c",),
                                    ("#1a1714",),
                                    "the arc's fill; red on a dial; black on meter"),
               group="Needle", when=_has_needle,
               doc="One colour, or several: the needle takes the colour for "
                   "the value it points at"),
        number("needle_length", _per(0.92, 0.88, 1.12, "suits the style"), 0.1, 1.5,
               group="Needle", when=_has_needle,
               doc="As a fraction of the scale's radius"),
        integer("needle_width",
                _per(lambda v, s: max(2, round(_short(s) * 0.022)),
                     lambda v, s: max(3, round(_short(s) * 0.03)),
                     lambda v, s: max(2, round(s.rect.height * 0.018)),
                     "scaled to the gauge"), 1, 64,
                group="Needle", when=_has_needle, doc="Width at the pivot, in pixels"),
        integer("hub",
                _per(lambda v, s: max(3, round(_short(s) * 0.035)),
                     lambda v, s: max(4, round(_short(s) * 0.07)),
                     0, "scaled to the gauge; none on meter"), 0, 256,
                group="Needle", when=_has_needle,
                doc="Radius of the cap over the pivot; 0 for none"),
        color("hub_color", _per(None, "#282c34", "#1a1714", "the needle's colour on an arc"),
              group="Needle", when=lambda v: _has_needle(v) and bool(v.get("hub"))),
        boolean("shadow", _per(False, True, True, "on for dial and meter"),
                group="Needle", when=_has_needle, doc="A soft shadow under the needle"),
        # -- text --
        string("label", None, group="Text", doc="Caption, e.g. CPU"),
        color("label_color", _per("#c0c4d0", "#8c94a0", "#2a221c", "suits the face"),
              group="Text", when=_shows_text),
        color("color", _per("#ffffff", "#ffffff", "#2a221c", "suits the face"),
              group="Text", when=_shows_text, doc="Colour of the value text"),
        font(group="Text", when=_shows_text),
        integer("label_size",
                _per(20, lambda v, s: max(8, round(_short(s) * 0.036)),
                     lambda v, s: max(8, round(s.rect.height * 0.11)), "suits the style"),
                1, 512, group="Text", when=lambda v: bool(v.get("label"))),
        integer("size",
                _per(14, lambda v, s: max(8, round(_short(s) * 0.08)),
                     lambda v, s: max(8, round(s.rect.height * 0.09)), "suits the style"),
                1, 512, group="Text", when=lambda v: bool(v.get("format")),
                doc="Size of the value text"),
    )

    __slots__ = ("_centre", "_colours", "_ring", "_round_tip", "_draw", "_font", "_format",
                 "_glide", "_hub", "_hub_fill", "_label", "_label_fill", "_label_font",
                 "_label_xy", "_last", "_last_time", "_lut", "_needle", "_needle_lut",
                 "_next_read", "_radius", "_range", "_read_period", "_shadow", "_shown",
                 "_source", "_start", "_style", "_sweep", "_target", "_text_fill", "_text_now",
                 "_thickness", "_track", "_value_xy", "animating")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        o = self.schema.read(options, spec)
        self._source = o.source
        self._style = o.style
        low, high = o.min, o.max
        if high <= low:
            raise options.fail("max", f"greater than min ({low})", high)
        self._range = (low, high - low)
        # Angles as Pillow has them: degrees clockwise from three o'clock.
        # The sweep is signed -- negative runs counterclockwise -- so a
        # point along the scale is always start + sweep * fraction.
        self._sweep = o.sweep if o.direction == "clockwise" else -o.sweep
        # Centred opposite its ends: on the top (270) when they are at
        # the bottom. A meter's pivot is below its scale, so it stays so.
        centre = 90.0 if o.start == "top" and o.style != "meter" else 270.0
        self._start = centre - self._sweep / 2
        self._lut = gradient.lut(o.fill)
        self._track = o.track
        self._thickness = o.thickness
        self._label = o.label
        self._format = o.format
        self._label_fill = o.label_color
        self._text_fill = o.color
        self._label_font = theme.font(o.font, o.label_size)
        self._font = theme.font(o.font, o.size)
        options.reject_unknown()

        width, height = spec.rect.size
        self._needle = None
        if self._style != "arc" or o.needle:
            stops = o.needle_color or ((255, 72, 60),)
            self._needle_lut = gradient.lut(tuple(stops))
            self._needle = (o.needle_length, o.needle_width)
        self._hub = o.hub if self._needle else 0
        self._hub_fill = o.hub_color
        self._shadow = bool(o.shadow) and self._needle is not None

        if self._style == "arc":
            pad = self._thickness // 2 + 1
            self._ring = _ring((width, height), (width / 2 - pad, height / 2 - pad),
                               self._thickness, self._start, self._sweep, o.caps == "round")
            # The fill's colours, made once: round the arc, or across the
            # box. By level there are none; it takes one colour as it moves.
            shape = o.fill_shape if len(o.fill) > 1 else "level"
            self._colours = (
                self._ring.coloured(self._lut) if shape == "scale"
                else gradient.paint(tuple(o.fill), (width, height), o.fill_angle, shape)
                if shape in gradient.SHAPES else None)
            self._round_tip = o.tip == "round"
            self._centre = (width / 2 - 0.5, height / 2 - 0.5)
            # The inner edge of the arc's stroke: ticks and needle sit inside it.
            self._radius = min(width, height) / 2 - pad - self._thickness
            # The two lines sit either side of the middle, so the pair
            # reads as centred even though neither line is.
            self._label_xy = (width // 2, height // 2 - 2)
            self._value_xy = (width // 2, height // 2 + 2)
            if o.needle:
                # The middle is the needle's: the text goes below it.
                below = round(self._radius * 0.42)
                self._label_xy = (width // 2, height // 2 + below - 2)
                self._value_xy = (width // 2, height // 2 + below + 2)
        elif self._style == "dial":
            self._radius = min(width, height) / 2 - 1
            cx, cy = self._centre = (width / 2, height / 2)
            # In the gap between the scale's ends, where there are no numbers.
            if o.start == "top":
                self._label_xy = (cx, cy - self._radius * 0.62)
                self._value_xy = (cx, cy - self._radius * 0.40)
            else:
                self._label_xy = (cx, cy + self._radius * 0.40)
                self._value_xy = (cx, cy + self._radius * 0.62)
        else:  # meter
            half = math.radians(min(abs(self._sweep), 170.0) / 2)
            top = height * 0.24
            radius = (width * 0.40) / max(math.sin(half), 0.2)
            radius = min(radius, max(height * 2.5, 1.0))
            self._radius = radius
            self._centre = (width / 2, top + radius)
            self._label_xy = (width / 2, height * 0.70)
            self._value_xy = (width / 2, height * 0.88)

        self.backdrop = self._face(o, theme)
        self.surface.paste(self.backdrop, (0, 0))
        self._draw = ImageDraw.Draw(self.surface)
        self._last: tuple | None = None

        # Reading and gliding.
        self._glide = o.glide
        self._read_period = self.period
        self._next_read = 0.0
        self._last_time: float | None = None
        self._target = self._shown = 0.0
        self._text_now: str | None = None
        self.animating = False
        if self._glide > 0:
            # Visited often enough to swing smoothly; read at `fps`.
            self.period = min(self.period, 1.0 / ANIM_FPS)
            self.animating = True
        # Format against a real sample now, so one that does not fit its
        # source fails at load rather than on the first frame.
        self._text(self._source.read(time.monotonic())[1])

    # -- the face, drawn once --

    def _face(self, o, theme: Theme) -> Image.Image:
        """The backdrop with everything that does not move drawn on it."""
        face = self.backdrop.copy()
        width, height = face.size
        start, sweep = self._start, self._sweep
        if self._style == "arc":
            ring = self._ring
            face.paste(self._track, (0, 0), ring.mask)
            low, size = self._range
            for zone_low, zone_high, colour in o.zones:
                within = ring.between((zone_low - low) / size, (zone_high - low) / size)
                face.paste(_mix(self._track, colour, 0.6), (0, 0), within)
        layer = Image.new("RGBA", (width * SS, height * SS), (0, 0, 0, 0))
        big = ImageDraw.Draw(layer)
        cx, cy = (c * SS for c in self._centre)
        r = self._radius * SS
        drew = False
        if self._style == "dial":
            rim = max(2.0, r * 0.017)
            big.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*o.face, 255),
                        outline=(*o.rim, 255), width=round(rim))
            band = r * 0.045
            for low, high, colour in o.zones:
                rb = r * 0.90
                a0, a1 = self._angle(low), self._angle(high)
                if a1 != a0:
                    big.arc((cx - rb, cy - rb, cx + rb, cy + rb), *_span(a0, a1),
                            fill=(*colour, 255), width=round(band))
            outer, major, minor, numbers_at = r * 0.93, r * 0.11, r * 0.055, r * 0.70
            drew = True
        elif self._style == "meter":
            corner = min(width, height) * 0.08 * SS
            rim = max(2.0, min(width, height) * 0.016 * SS)
            big.rounded_rectangle((rim / 2, rim / 2, width * SS - rim / 2, height * SS - rim / 2),
                                  corner, fill=(*o.face, 255), outline=(*o.rim, 255),
                                  width=round(rim))
            line = max(SS, round(r * 0.006))
            big.arc((cx - r, cy - r, cx + r, cy + r), *_span(start, start + sweep),
                    fill=(*o.scale_color, 255), width=line)
            for low, high, colour in o.zones:
                a0, a1 = self._angle(low), self._angle(high)
                band = max(2 * SS, round(r * 0.03))
                rb = r + band
                if a1 != a0:
                    big.arc((cx - rb, cy - rb, cx + rb, cy + rb), *_span(a0, a1),
                            fill=(*colour, 255), width=band)
            outer, major, minor, numbers_at = -r, r * 0.07, r * 0.035, None
            drew = True
        else:
            inner = r - 3 * SS
            outer, major, minor, numbers_at = inner, r * 0.09, r * 0.045, inner - r * 0.22
        if o.ticks:
            drew = True
            number_font = theme.font(o.font, o.number_size * SS) if o.numbers else None
            colour = (*o.scale_color, 255)
            count = o.ticks * (o.minor_ticks + 1)
            for index in range(count + 1):
                fraction = index / count
                angle = self._start + self._sweep * fraction
                is_major = index % (o.minor_ticks + 1) == 0
                length = major if is_major else minor
                if outer >= 0:
                    a, b = _point(cx, cy, outer, angle), _point(cx, cy, outer - length, angle)
                else:   # meter: ticks stand outward from the scale line
                    a, b = _point(cx, cy, r, angle), _point(cx, cy, r + length, angle)
                thick = max(SS, round(r * (0.014 if is_major else 0.007)))
                big.line((a, b), fill=colour if is_major else (*o.scale_color, 170),
                         width=thick)
                if is_major and number_font is not None:
                    value = self._range[0] + self._range[1] * fraction
                    at = numbers_at if numbers_at is not None else \
                        r + major + o.number_size * SS * 0.9
                    x, y = _point(cx, cy, at, angle)
                    big.text((x, y), _number(value, getattr(self._source, "range", None) == (0.0, 1.0)), font=number_font,
                             fill=self._zone_colour(o.zones, value, colour), anchor="mm")
        if self._style != "arc" and self._label:
            drew = True
            label_font = theme.font(o.font, o.label_size * SS)
            lx, ly = self._label_xy
            big.text((lx * SS, ly * SS), self._label, font=label_font,
                     fill=(*self._label_fill, 255), anchor="mm")
        if drew:
            # Premultiplied while scaling, so edges do not darken.
            small = layer.convert("RGBa").resize((width, height),
                                                 Image.Resampling.LANCZOS).convert("RGBA")
            face.paste(small.convert("RGB"), (0, 0), small.getchannel("A"))
        return face

    def _zone_colour(self, zones, value, default):
        for low, high, colour in zones:
            if low <= value <= high:
                return (*colour, 255)
        return default

    def _angle(self, value: float) -> float:
        low, size = self._range
        fraction = min(1.0, max(0.0, (value - low) / size))
        return self._start + self._sweep * fraction

    # -- per frame --

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
        if now >= self._next_read:
            value, fields = self._source.read(now)
            self._next_read = now + self._read_period if self._glide > 0 else 0.0
            low, size = self._range
            self._target = min(1.0, max(0.0, ((value or 0.0) - low) / size))
            self._text_now = self._text(fields)
            if self._last_time is None:
                self._shown = self._target      # no swing in from zero at load
        if self._glide > 0 and self._last_time is not None:
            step = 1.0 - math.exp(-(now - self._last_time) / (self._glide / 4))
            self._shown += (self._target - self._shown) * step
            if abs(self._target - self._shown) < 0.0005:
                self._shown = self._target
        else:
            self._shown = self._target
        self._last_time = now
        fraction = self._shown
        if self._needle is None:
            # Whole degrees: the arc cannot show finer than that, so a value
            # wobbling below one degree redraws nothing.
            position: float = round(self._sweep * fraction)
        else:
            position = round(self._sweep * fraction * 4) / 4
        state = (position, self._text_now)
        if state == self._last:
            return False
        self._last = state

        self.clear()
        if self._style == "arc":
            self._arc(fraction)
        if self._needle is not None:
            self._draw_needle(fraction)
        if self._style == "arc" and self._label:
            self._draw.text(self._label_xy, self._label, font=self._label_font,
                            fill=self._label_fill, anchor="md")
        text = self._text_now
        if text:
            anchor = "ma" if self._style == "arc" else "mm"
            self._draw.text(self._value_xy, text, font=self._font, fill=self._text_fill,
                            anchor=anchor)
        return True

    def _arc(self, fraction: float) -> None:
        """The fill: the ring drawn once at load, shown up to `fraction`."""
        if fraction <= 0:
            return
        ring = self._ring
        shown = ring.upto(fraction)
        # The colour at the value: the whole fill's when it is one colour,
        # and the tip's when the ramp runs round the arc.
        colour = gradient.colour_at(self._lut, fraction)
        self.surface.paste(colour if self._colours is None else self._colours, (0, 0), shown)
        if self._round_tip and fraction < 1:
            # Trimmed to the arc: by flat ends, a tip near either one is
            # cut square with them rather than poking out past them.
            _fill_circle(self.surface, ring.point(fraction), ring.thickness / 2, colour,
                         within=ring.mask)

    def _draw_needle(self, fraction: float) -> None:
        length, width = self._needle
        cx, cy = self._centre
        r = self._radius
        angle = self._start + self._sweep * fraction
        tail = r * 0.14 if self._style == "dial" else 0.0
        half = width / 2
        tip_half = max(0.5, half * 0.25)
        tip = _point(cx, cy, r * length, angle)
        points = [
            _point(*tip, tip_half, angle - 90), _point(*tip, tip_half, angle + 90),
            _point(cx, cy, half, angle + 90),
        ]
        if tail:
            points.append(_point(cx, cy, -tail, angle))
        points.append(_point(cx, cy, half, angle - 90))
        colour = gradient.colour_at(self._needle_lut, fraction)
        if self._shadow:
            offset = max(1.5, r * 0.02)
            shadow = [(x + offset, y + offset * 1.2) for x, y in points]
            _fill_polygon(self.surface, shadow, (0, 0, 0), alpha=0.5,
                          blur=max(1.0, r * 0.025))
        _fill_polygon(self.surface, points, colour)
        if self._hub:
            hub = self._hub_fill or colour
            _fill_circle(self.surface, (cx, cy), self._hub, hub)
            if self._hub_fill is not None and self._hub_fill != colour:
                _fill_circle(self.surface, (cx, cy), max(1.5, self._hub * 0.38), colour)


class _Ring:
    """An arc's stroke, antialiased, and how far round it each pixel is.

    Built once per geometry (and cached): `mask` is the stroke itself,
    drawn at SS times and scaled down; `position` holds, for each pixel
    of it, how far along the sweep it lies, in 1/SCALE steps, in a 32-bit
    image. Showing the arc up to a value is then one linear map of that
    image -- clamped to 0..255, with a pixel-wide soft edge at the cut --
    times the mask: a couple of C loops, not a redraw.

    A round cap's pixels count as the start (0) or end (SCALE), so the
    caps come and go with the ends they belong to.
    """

    SCALE = 1 << 16

    def __init__(self, size, axes, thickness, start, sweep, round_caps) -> None:
        width, height = size
        cx, cy = width / 2, height / 2
        a, b = axes                                # outer semi-axes
        self.thickness = thickness
        self.round_caps = round_caps
        self._start, self._sweep = start, sweep
        self._centre = (cx, cy)
        self._mid = (a - thickness / 2, b - thickness / 2)
        big = Image.new("L", (width * SS, height * SS), 0)
        draw = ImageDraw.Draw(big)
        draw.arc((round((cx - a) * SS), round((cy - b) * SS),
                  round((cx + a) * SS) - 1, round((cy + b) * SS) - 1),
                 *_span(start, start + sweep), fill=255, width=thickness * SS)
        if round_caps:
            r = thickness / 2 * SS
            for end in (0.0, 1.0):
                x, y = (v * SS for v in self.point(end))
                draw.ellipse((x - r, y - r, x + r, y + r), fill=255)
        self.mask = big.reduce(SS)
        coverage = self.mask.tobytes()
        mid_a, mid_b = max(self._mid[0], 0.5), max(self._mid[1], 0.5)
        # A signed sweep: how far along is measured the way it runs.
        turn, span = (1.0, sweep) if sweep > 0 else (-1.0, -sweep)
        gap = 360.0 / span
        positions = []
        for y in range(height):
            dy = (y + 0.5 - cy) / mid_b
            row = y * width
            for x in range(width):
                if not coverage[row + x]:
                    positions.append(0)
                    continue
                angle = math.degrees(math.atan2(dy, (x + 0.5 - cx) / mid_a))
                along = (((angle - start) * turn) % 360.0) / span
                if along > 1.0:   # in a cap, past an end: which one is nearer
                    along = 1.0 if along - 1.0 < gap - along else 0.0
                positions.append(int(along * self.SCALE))
        self.position = Image.new("I", size)
        self.position.putdata(positions)
        # A soft edge about a pixel wide, measured along the middle of
        # the stroke.
        length = math.radians(span) * (mid_a + mid_b) / 2
        self._edge = max(1.0, self.SCALE / max(length, 1.0))

    def point(self, fraction: float) -> tuple[float, float]:
        """The middle of the stroke `fraction` of the way along."""
        angle = math.radians(self._start + self._sweep * fraction)
        (cx, cy), (a, b) = self._centre, self._mid
        return cx + a * math.cos(angle), cy + b * math.sin(angle)

    def _cut(self, at: float, keep_below: bool) -> Image.Image:
        edge = self._edge
        gain = 255.0 / edge
        if keep_below:     # 255 before `at`, 0 after, a ramp between
            return self.position.point(lambda v: v * -gain + (at + edge / 2) * gain) \
                .convert("L")
        return self.position.point(lambda v: v * gain - (at - edge / 2) * gain).convert("L")

    def upto(self, fraction: float) -> Image.Image:
        """The stroke from the start to `fraction` of the way along."""
        at = 2.0 * self.SCALE if fraction >= 1.0 else fraction * self.SCALE
        return ImageChops.multiply(self._cut(at, True), self.mask)

    def between(self, low: float, high: float) -> Image.Image:
        """The stroke from `low` to `high` of the way along (fractions)."""
        lo = -self.SCALE if low <= 0 else low * self.SCALE
        hi = 2.0 * self.SCALE if high >= 1 else high * self.SCALE
        if hi <= lo:
            return Image.new("L", self.mask.size, 0)
        span = ImageChops.multiply(self._cut(hi, True), self._cut(lo, False))
        return ImageChops.multiply(span, self.mask)

    def coloured(self, tables) -> Image.Image:
        """The whole stroke coloured by `tables`, each pixel by how far
        along it is: the ramp running round the arc."""
        along = self.position.point(lambda v: v * (255.0 / self.SCALE)).convert("L")
        return Image.merge("RGB", [along.point(table) for table in tables])


@lru_cache(maxsize=16)
def _ring(size, axes, thickness, start, sweep, round_caps) -> _Ring:
    """Cached: an editor rebuilds a widget on every change, and the
    position map is the one costly part. Callers must not draw on it."""
    return _Ring(size, axes, thickness, start, sweep, round_caps)


def _span(a: float, b: float) -> tuple[float, float]:
    """Two angles in the order Pillow's arc takes them: it always draws
    clockwise from the first, so a counterclockwise scale's arc is the
    same stroke drawn from its other end."""
    return (a, b) if b >= a else (b, a)


def _point(cx: float, cy: float, r: float, degrees: float) -> tuple[float, float]:
    a = math.radians(degrees)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _hexes(stops) -> tuple[str, ...] | None:
    """Colours as "#rrggbb", whether read (RGB tuples) or as written."""
    if isinstance(stops, str):
        return (stops,)
    if not stops:
        return None
    return tuple(s if isinstance(s, str) else "#%02x%02x%02x" % tuple(s[:3])
                 for s in stops)


def _mix(a, b, t: float):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _number(value: float, fractional: bool) -> str:
    """A scale number. A reading in fractions (cpu, memory, volume: the
    source's own range is 0..1) is numbered as a percentage -- whatever
    min and max the gauge sets, so a volume to 1.5 reads 0..150."""
    if fractional:
        value *= 100
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}"


def _fill_polygon(surface: Image.Image, points, colour, alpha: float = 1.0,
                  blur: float = 0.0) -> None:
    """A smooth-edged polygon, supersampled over its own bounding box only."""
    pad = 2 + math.ceil(blur * 2)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    left = max(0, math.floor(min(xs)) - pad)
    top = max(0, math.floor(min(ys)) - pad)
    right = min(surface.width, math.ceil(max(xs)) + pad)
    bottom = min(surface.height, math.ceil(max(ys)) + pad)
    if right <= left or bottom <= top:
        return
    mask = Image.new("L", ((right - left) * SS, (bottom - top) * SS), 0)
    ImageDraw.Draw(mask).polygon([((x - left) * SS, (y - top) * SS) for x, y in points],
                                 fill=255)
    mask = mask.reduce(SS)
    if blur:
        mask = mask.filter(ImageFilter.GaussianBlur(blur))
    if alpha < 1.0:
        mask = mask.point(lambda v: round(v * alpha))
    surface.paste(colour, (left, top, right, bottom), mask)


def _fill_circle(surface: Image.Image, centre, radius: float, colour,
                 within: Image.Image | None = None) -> None:
    cx, cy = centre
    left = max(0, math.floor(cx - radius) - 1)
    top = max(0, math.floor(cy - radius) - 1)
    right = min(surface.width, math.ceil(cx + radius) + 2)
    bottom = min(surface.height, math.ceil(cy + radius) + 2)
    if right <= left or bottom <= top:
        return
    mask = Image.new("L", ((right - left) * SS, (bottom - top) * SS), 0)
    x, y = (cx - left) * SS, (cy - top) * SS
    ImageDraw.Draw(mask).ellipse((x - radius * SS, y - radius * SS,
                                  x + radius * SS, y + radius * SS), fill=255)
    mask = mask.reduce(SS)
    if within is not None:
        mask = ImageChops.multiply(mask, within.crop((left, top, right, bottom)))
    surface.paste(colour, (left, top, right, bottom), mask)
