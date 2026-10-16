"""Text effects: gradient fill, outline, and drop shadow or glow.

Shared so every widget that draws text can offer the same options with
the same names. A widget declares `options()` in its schema (or
`fill_options()` and `effect_options()` apart, to place them), builds a
`TextStyle` from what it read, and draws through `draw()`.

Everything is done with masks: the text is drawn once as a greyscale
stencil and colour is pasted through it -- a gradient image for the
fill, a flat colour for the outline, a blurred and offset copy for the
shadow. That is a handful of C loops in Pillow per draw, and a widget
only draws when its text changes, so a clock pays it once a second.

With no effect set, `draw()` is a plain `ImageDraw.text`, the same
pixels a theme got before effects existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import gradient
from .schema import Option, angle, choice, color, colors, integer, number

RGB = tuple[int, int, int]


def fill_options(fill: str = "#ffffff", group: str = "Text") -> list[Option]:
    """How the letters are filled: `color`, one colour or several.

    A gradient is a colour with more stops, so there is one option, not
    two that could disagree: `color = "#fff"` or `color = ["#f0f", "#0ff"]`.
    """
    return [
        colors("color", (fill,), group=group,
               doc="One colour, or a list of them for a gradient across the text"),
        *gradient_options("color", group=group, subject="the text"),
    ]


# How a gradient that shows a reading is laid out: one colour picked by
# the reading, along the scale, or across the box like any other.
READING_SHAPES = ("level", "scale", *gradient.SHAPES)


def gradient_options(key: str, group: str, subject: str = "it",
                     shapes: tuple[str, ...] = gradient.SHAPES,
                     default_shape: Any = "linear", when=None) -> list[Option]:
    """`<key>_shape` and `<key>_angle` for a colours option, shown once it
    has a second stop. `shapes` is gradient.SHAPES for an area, or
    READING_SHAPES for a gradient that shows a reading."""
    def has_gradient(values) -> bool:
        stops = values.get(key)
        return (isinstance(stops, (list, tuple)) and len(stops) > 1
                and (when is None or when(values)))

    doc = (f"linear runs across {subject} at an angle; radial from the middle out")
    if "level" in shapes:
        doc = ("level: one colour, picked by the reading; scale: the gradient "
               "runs along the scale, so a higher reading reaches further "
               f"into it; {doc}")
    return [
        choice(f"{key}_shape", shapes, default_shape, group=group, when=has_gradient,
               doc=doc),
        angle(f"{key}_angle", 180.0, group=group,
              when=lambda v: has_gradient(v) and v.get(f"{key}_shape") == "linear",
              doc="Which way the gradient runs, in degrees: 0 bottom to top, 90 "
                  "left to right, 180 top to bottom, 270 right to left"),
    ]


def effect_options(effects: str = "Effects") -> list[Option]:
    """Outline and shadow or glow."""
    has_outline = lambda v: v.get("outline") is not None  # noqa: E731
    has_shadow = lambda v: v.get("shadow") is not None  # noqa: E731
    return [
        color("outline", None, group=effects,
              doc="A line around each letter, for reading over a busy "
                  "wallpaper; empty for none"),
        integer("outline_width", 2, 1, 16, group=effects, when=has_outline),
        color("shadow", None, group=effects,
              doc="Drop shadow colour; with no offset and a bright colour "
                  "it is a glow. Empty for none"),
        integer("shadow_x", 2, -64, 64, group=effects, when=has_shadow,
                doc="Horizontal offset; 0 with shadow_y 0 for a glow"),
        integer("shadow_y", 2, -64, 64, group=effects, when=has_shadow),
        integer("shadow_blur", 3, 0, 32, group=effects, when=has_shadow,
                doc="Softness; 0 for a hard-edged shadow. Keep it inside "
                    "the widget's box, which clips it"),
        number("shadow_alpha", 0.6, 0.0, 1.0, group=effects, when=has_shadow),
    ]


def options(fill: str = "#ffffff", group: str = "Text",
            effects: str = "Effects") -> list[Option]:
    """Everything a text-drawing widget adds to its schema."""
    return [*fill_options(fill, group), *effect_options(effects)]


@dataclass(slots=True)
class TextStyle:
    fill: RGB
    stops: tuple[RGB, ...] | None = None
    angle: float = 180.0
    shape: str = "linear"
    outline: RGB | None = None
    outline_width: int = 2
    shadow: RGB | None = None
    shadow_offset: tuple[int, int] = (2, 2)
    shadow_blur: int = 3
    shadow_alpha: float = 0.6
    _ramps: dict = field(default_factory=dict)

    @classmethod
    def read(cls, values) -> TextStyle:
        """From schema values read through `options()`."""
        stops = tuple(values.color)
        return cls(
            fill=stops[0],
            stops=stops if len(stops) > 1 else None,
            angle=values.color_angle,
            shape=values.color_shape,
            outline=values.outline,
            outline_width=values.outline_width,
            shadow=values.shadow,
            shadow_offset=(values.shadow_x, values.shadow_y),
            shadow_blur=values.shadow_blur,
            shadow_alpha=values.shadow_alpha,
        )

    @property
    def plain(self) -> bool:
        return self.stops is None and self.outline is None and self.shadow is None

    def draw(
        self,
        surface: Image.Image,
        xy: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        anchor: str,
        band: tuple[int, int],
    ) -> None:
        """Draw `text` at `xy` onto `surface`.

        `band` is the vertical extent a vertical gradient spans -- the
        widget's cap band, so the ramp sits on the letters and stays put
        as the text changes. A horizontal one spans the text itself.
        """
        if self.plain:
            ImageDraw.Draw(surface).text(xy, text, font=font, fill=self.fill,
                                         anchor=anchor)
            return
        size = surface.size
        stroke = self.outline_width if self.outline is not None else 0
        if self.shadow is not None:
            shape = _mask(size, xy, text, font, anchor, stroke)
            dx, dy = self.shadow_offset
            moved = Image.new("L", size, 0)
            moved.paste(shape, (dx, dy))
            if self.shadow_blur:
                moved = moved.filter(ImageFilter.GaussianBlur(self.shadow_blur))
            if self.shadow_alpha < 1.0:
                alpha = self.shadow_alpha
                moved = moved.point(lambda value: int(value * alpha))
            surface.paste(self.shadow, (0, 0), moved)
        if self.outline is not None:
            surface.paste(self.outline, (0, 0),
                          _mask(size, xy, text, font, anchor, stroke))
        letters = _mask(size, xy, text, font, anchor, 0)
        if self.stops is None:
            surface.paste(self.fill, (0, 0), letters)
            return
        # The gradient spans the text: across the ink, and up the cap band
        # -- which stays put as the text changes, so a clock's colours do
        # not shift with its digits.
        ink = letters.getbbox() or (0, 0, size[0], size[1])
        box = (ink[0], band[0], max(ink[2], ink[0] + 1), max(band[1], band[0] + 1))
        surface.paste(self._painted(size, box), (0, 0), letters)

    def _painted(self, size: tuple[int, int], box: tuple[int, int, int, int]) -> Image.Image:
        """The gradient over `box`, carried flat beyond it to `size`."""
        key = (size, box)
        painted = self._ramps.get(key)
        if painted is None:
            if len(self._ramps) > 16:  # follows the text's width
                self._ramps.clear()
            assert self.stops is not None
            inner = gradient.paint(self.stops, (box[2] - box[0], box[3] - box[1]),
                                   self.angle, self.shape)
            painted = _extend(inner, size, box)
            self._ramps[key] = painted
        return painted


def _extend(inner: Image.Image, size: tuple[int, int], box) -> Image.Image:
    """`inner` placed at `box`, its edges stretched out to fill `size`:
    a descender below the cap band keeps the last colour, not black."""
    width, height = size
    left, top, right, bottom = box
    out = Image.new("RGB", size)
    # Stretch the edge rows and columns outward, corners included.
    xs = ((0, left, 0, 1), (left, right, 0, inner.width), (right, width, inner.width - 1, inner.width))
    ys = ((0, top, 0, 1), (top, bottom, 0, inner.height), (bottom, height, inner.height - 1, inner.height))
    for x0, x1, sx0, sx1 in xs:
        for y0, y1, sy0, sy1 in ys:
            if x1 > x0 and y1 > y0:
                piece = inner.crop((sx0, sy0, sx1, sy1)).resize((x1 - x0, y1 - y0),
                                                                Image.Resampling.NEAREST)
                out.paste(piece, (x0, y0))
    return out


def _mask(size, xy, text, font, anchor, stroke: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255, anchor=anchor,
                              stroke_width=stroke, stroke_fill=255)
    return mask
