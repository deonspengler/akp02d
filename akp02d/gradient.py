"""Multi-stop colour ramps, shared by themes and the spectrum plugin.

One implementation so a theme's gradient and a widget's interpolate
identically. Both build a greyscale position map and push it through
three 256-entry lookups, which are C loops in Pillow -- the cost is paid
once at load and never per frame.
"""

from __future__ import annotations

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
