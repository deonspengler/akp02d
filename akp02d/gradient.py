"""Multi-stop colour ramps, shared by themes and the spectrum plugin.

One implementation so a theme's gradient and a widget's interpolate
identically. Both build a greyscale position map and push it through
three 256-entry lookups, which are C loops in Pillow -- the cost is paid
once at load and never per frame.
"""

from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image

RGB = tuple[int, int, int]


def lut(stops: tuple[RGB, ...]) -> tuple[list[int], ...]:
    """256-entry lookup tables, one per channel, across the stops."""
    if len(stops) == 1:
        return tuple([stops[0][channel]] * 256 for channel in range(3))
    segments = len(stops) - 1
    tables: tuple[list[int], ...] = ([], [], [])
    for index in range(256):
        position = index / 255 * segments
        first = min(int(position), segments - 1)
        blend = position - first
        low, high = stops[first], stops[first + 1]
        for channel in range(3):
            tables[channel].append(
                round(low[channel] + (high[channel] - low[channel]) * blend)
            )
    return tables


def colour_at(tables: tuple[list[int], ...], value: float) -> RGB:
    """The ramp colour for a 0..1 position."""
    index = 0 if value <= 0 else (255 if value >= 1 else int(value * 255))
    return tables[0][index], tables[1][index], tables[2][index]


def position_map(kind: str, width: int, height: int) -> Image.Image:
    """A greyscale map of where each pixel sits along the ramp.

    "up" runs 0 at the bottom, so a tall bar reaches the last stop;
    "centre" runs 0 at the middle row, for a mirrored shape measuring
    from its own axis; "radial" runs 0 at the centre.
    """
    if kind == "radial":
        return Image.radial_gradient("L").resize((width, height))
    strip = Image.new("L", (1, height))
    last = max(height - 1, 1)
    if kind == "centre":
        middle = max((height - 1) / 2, 1)
        strip.putdata(
            [min(255, int(abs(y - middle) / middle * 255)) for y in range(height)]
        )
    else:  # up
        strip.putdata([(last - y) * 255 // last for y in range(height)])
    return strip.resize((width, height))


def image(
    stops: tuple[RGB, ...], size: tuple[int, int], kind: str = "up"
) -> Image.Image:
    """A gradient filling `size`, interpolated across `stops`."""
    width, height = size
    position = position_map(kind, width, height)
    return Image.merge("RGB", tuple(position.point(t) for t in lut(stops)))


# -- gradients painted across an area: a panel, a widget, a line of text --

SHAPES = ("linear", "radial")
# Angles as CSS writes them: 0 runs bottom to top, then clockwise -- 90
# left to right, 180 top to bottom, 270 right to left.
DOWN = 180.0


@lru_cache(maxsize=64)
def ramp(size: tuple[int, int], angle: float = DOWN, shape: str = "linear") -> Image.Image:
    """Positions 0..255 across an area: where each pixel falls between
    the first stop and the last.

    Linear runs at `angle`, over the length CSS uses -- the corners the
    line starts and ends at get exactly the first and last stop,
    whatever the box's shape. Radial runs from the centre to the edges.
    Cached: callers must not draw on it.
    """
    width, height = max(1, size[0]), max(1, size[1])
    if shape == "radial":
        return Image.radial_gradient("L").resize((width, height), Image.Resampling.BILINEAR)
    theta = math.radians(angle)
    length = max(1, round(abs(width * math.sin(theta)) + abs(height * math.cos(theta))))
    side = math.ceil(math.hypot(width, height)) + 4
    # A top-to-bottom ramp `length` long in the middle of a square, flat
    # beyond it, turned to the angle; the box is cut from the middle.
    square = Image.new("L", (side, side), 0)
    top = (side - length) // 2
    square.paste(255, (0, top + length, side, side))
    square.paste(Image.linear_gradient("L").resize((side, length), Image.Resampling.BILINEAR),
                 (0, top))
    turned = square.rotate(DOWN - angle, resample=Image.Resampling.BILINEAR,
                           center=(side / 2, top + length / 2))
    left = round(side / 2 - width / 2)
    upper = round(top + length / 2 - height / 2)
    return turned.crop((left, upper, left + width, upper + height))


@lru_cache(maxsize=16)
def paint(stops: tuple[RGB, ...], size: tuple[int, int], angle: float = DOWN,
          shape: str = "linear") -> Image.Image:
    """`stops` painted across an area of `size`: a flat colour for one
    stop, else the gradient. Cached: callers must not draw on it."""
    if len(stops) == 1:
        return Image.new("RGB", size, stops[0])
    positions = ramp(size, angle, shape)
    return Image.merge("RGB", [positions.point(table) for table in lut(stops)])
