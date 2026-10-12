"""Text effects: gradient fill, outline, and drop shadow or glow.

Shared so every widget that draws text can offer the same options with
the same names. A widget declares `options()` in its schema, builds a
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

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import gradient
from .schema import Option, choice, color, colors, integer, number

RGB = tuple[int, int, int]


def options(group: str = "Text", effects: str = "Effects") -> list[Option]:
    """The options a text-drawing widget adds to its schema."""
    has_gradient = lambda v: bool(v.get("gradient"))  # noqa: E731
    has_outline = lambda v: v.get("outline") is not None  # noqa: E731
    has_shadow = lambda v: v.get("shadow") is not None  # noqa: E731
    return [
        colors("gradient", None, group=group,
               doc="Fill the text with a gradient instead of one colour; "
                   "empty uses `color`"),
        choice("gradient_direction", ("vertical", "horizontal"), "vertical",
               group=group, when=has_gradient,
               doc="vertical runs top to bottom over the letters' height; "
                   "horizontal runs left to right across the text"),
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


@dataclass(slots=True)
class TextStyle:
    fill: RGB
    stops: tuple[RGB, ...] | None = None
    vertical: bool = True
    outline: RGB | None = None
    outline_width: int = 2
    shadow: RGB | None = None
    shadow_offset: tuple[int, int] = (2, 2)
    shadow_blur: int = 3
    shadow_alpha: float = 0.6
    _ramps: dict = field(default_factory=dict)

    @classmethod
    def read(cls, values, fill: RGB) -> TextStyle:
        """From schema values read through `options()`."""
        return cls(
            fill=fill,
            stops=tuple(values.gradient) if values.gradient else None,
            vertical=values.gradient_direction == "vertical",
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
        if self.vertical:
            start, end = band
        else:
            box = letters.getbbox() or (0, 0, size[0], size[1])
            start, end = box[0], box[2]
        surface.paste(self._ramp(size, start, end), (0, 0), letters)

    def _ramp(self, size: tuple[int, int], start: int, end: int) -> Image.Image:
        """The gradient over [start, end] on the axis, flat beyond it."""
        key = (size, self.vertical, start, end)
        ramp = self._ramps.get(key)
        if ramp is None:
            if len(self._ramps) > 16:  # a horizontal ramp follows the text
                self._ramps.clear()
            width, height = size
            length = height if self.vertical else width
            span = max(end - start, 1)
            positions = [min(255, max(0, (i - start) * 255 // span)) for i in range(length)]
            if self.vertical:
                strip = Image.new("L", (1, length))
            else:
                strip = Image.new("L", (length, 1))
            strip.putdata(positions)
            stretched = strip.resize(size, Image.Resampling.NEAREST)
            assert self.stops is not None
            tables = gradient.lut(self.stops)
            ramp = Image.merge("RGB", tuple(stretched.point(t) for t in tables))
            self._ramps[key] = ramp
        return ramp


def _mask(size, xy, text, font, anchor, stroke: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255, anchor=anchor,
                              stroke_width=stroke, stroke_fill=255)
    return mask
