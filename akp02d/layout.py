"""Geometry, and the checks a theme must pass to be drawn.

Pure and hardware-free: reads the panel's constants off the AKP02 class
but never opens a device, so a theme can be validated with nothing
plugged in.

The rule worth knowing: a region renders with the wrong colour unless
the offset reaching the CRTDRA header's x field is congruent to
SHORT_AXIS_ALIGN_RESIDUE modulo SHORT_AXIS_ALIGN_MODULUS. show()
handles a violation by nudging the region and warning, every frame;
akp02d would rather reject the theme at load so the widget lands where
its author put it. Both constants are read from the library, so a
firmware finding that moves them moves this too.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from akp02 import AKP02, Orientation

_SHORT = AKP02.PANEL_SHORT_SIDE
_MODULUS = AKP02.SHORT_AXIS_ALIGN_MODULUS
_RESIDUE = AKP02.SHORT_AXIS_ALIGN_RESIDUE


class LayoutError(Exception):
    """A theme's geometry cannot be drawn as written."""


class Rect(NamedTuple):
    """A widget's box in caller space for the active orientation."""

    x: int
    y: int
    width: int
    height: int

    @property
    def at(self) -> tuple[int, int]:
        return self.x, self.y

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def overlaps(self, other: Rect) -> bool:
        return (
            self.x < other.x + other.width
            and other.x < self.x + self.width
            and self.y < other.y + other.height
            and other.y < self.y + self.height
        )


def screen_size(orientation: Orientation) -> tuple[int, int]:
    """Caller-space size. AKP02.size needs an open panel; a theme is
    validated before one exists."""
    if orientation is Orientation.PORTRAIT:
        return _SHORT, AKP02.PANEL_LONG_SIDE
    return AKP02.PANEL_LONG_SIDE, _SHORT


def bound_axis(rect: Rect, orientation: Orientation, inverted: bool):
    """(name, value, extent, reflected) for the coordinate the rule binds.

    Mirrors AKP02._region_rect: landscape binds y, portrait binds x, and
    reflection flips with `inverted`.
    """
    if orientation is Orientation.LANDSCAPE:
        return "y", rect.y, rect.height, not inverted
    return "x", rect.x, rect.width, inverted


def header_offset(rect: Rect, orientation: Orientation, inverted: bool) -> int:
    """What the header's x field will carry for `rect`."""
    _, value, extent, reflected = bound_axis(rect, orientation, inverted)
    return (_SHORT - value - extent) if reflected else value


def is_aligned(rect: Rect, orientation: Orientation, inverted: bool) -> bool:
    """Whether `rect` renders with correct colour, unnudged."""
    return header_offset(rect, orientation, inverted) % _MODULUS == _RESIDUE


def nearest_aligned(
    rect: Rect, orientation: Orientation, inverted: bool
) -> tuple[int, ...]:
    """Valid coordinates on the bound axis, nearest first, for the error."""
    _, value, extent, reflected = bound_axis(rect, orientation, inverted)
    if extent > _SHORT:
        return ()
    # header is (S - v - e) or v, so v itself is fixed to one residue class.
    target = (_SHORT - extent - _RESIDUE if reflected else _RESIDUE) % _MODULUS
    base = value - (value - target) % _MODULUS
    return tuple(
        sorted(
            (c for c in (base, base + _MODULUS) if 0 <= c <= _SHORT - extent),
            key=lambda c: (abs(c - value), c),
        )
    )


def validate(
    rects: Iterable[tuple[str, Rect]],
    screen: tuple[int, int],
    orientation: Orientation,
    inverted: bool,
) -> None:
    """Reject a layout the panel cannot draw as written.

    Per widget: positive size, on screen, colour-safe. Then no overlaps,
    since akp02d pushes each widget as its own region with no
    compositing step, and overlapping boxes would fight over the same
    pixels in whatever order the loop visited them.
    """
    items = list(rects)
    screen_w, screen_h = screen
    for name, rect in items:
        where = f"widget {name!r}:"
        if rect.width <= 0 or rect.height <= 0:
            raise LayoutError(f"{where} size must be positive, got {rect.width}x{rect.height}")
        if (rect.x < 0 or rect.y < 0
                or rect.x + rect.width > screen_w
                or rect.y + rect.height > screen_h):
            raise LayoutError(
                f"{where} ({rect.x},{rect.y}) {rect.width}x{rect.height} does not "
                f"fit the {screen_w}x{screen_h} screen"
            )
        if not is_aligned(rect, orientation, inverted):
            axis, value, extent, _ = bound_axis(rect, orientation, inverted)
            extent_name = "height" if axis == "y" else "width"
            options = nearest_aligned(rect, orientation, inverted)
            fix = (
                f"nearest colour-safe {axis} for this size: "
                + " or ".join(map(str, options))
                if options
                else f"no colour-safe {axis} exists; change the {extent_name}"
            )
            raise LayoutError(
                f"{where} {axis}={value} with {extent_name}={extent} is not "
                f"colour-safe; {fix}"
            )
    for index, (name, rect) in enumerate(items):
        for other_name, other in items[index + 1 :]:
            if rect.overlaps(other):
                raise LayoutError(
                    f"widgets {name!r} and {other_name!r} overlap; akp02d draws "
                    f"each as its own region and does not composite"
                )
