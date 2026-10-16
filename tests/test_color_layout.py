"""Every gradient that shows a reading is laid out the same four ways:
level, scale, linear (at an angle) and radial."""

from __future__ import annotations

import pytest

from akp02d.layout import Rect
from akp02d.plugins.cava import MAX

from test_spectrum_silence import spectrum
from test_widgets import Fixed, build

RED, BLUE = "#ff0000", "#0000ff"


def reddish(pixel) -> bool:
    return pixel[0] > pixel[2] + 60


def bluish(pixel) -> bool:
    return pixel[2] > pixel[0] + 60


def bar(shape: str, value: float = 1.0, **extra):
    widget = build("bar", {"source": "cpu", "fill": [RED, BLUE], "fill_shape": shape,
                           "track": "#000000", **extra})
    widget._source = Fixed(value)
    widget.render(0.0)
    return widget.surface


def test_bar_four_ways() -> None:
    scale = bar("scale")
    assert reddish(scale.getpixel((5, 40))) and bluish(scale.getpixel((294, 40)))
    level = bar("level", 0.5)
    assert level.getpixel((5, 40)) == level.getpixel((140, 40))       # one colour
    across = bar("linear", fill_angle=0.0)                               # bottom to top
    assert reddish(across.getpixel((150, 78))) and bluish(across.getpixel((150, 1)))
    radial = bar("radial")
    assert reddish(radial.getpixel((150, 40))) and bluish(radial.getpixel((2, 2)))


def gauge(shape: str, value: float = 1.0, **extra):
    widget = build("gauge", {"source": "cpu", "fill": [RED, BLUE], "fill_shape": shape,
                             "sweep": 360.0, "track": "#000000", "thickness": 14, **extra},
                   Rect(0, 0, 160, 160))
    widget._source = Fixed(value)
    widget.render(0.0)
    return widget.surface


def test_gauge_four_ways() -> None:
    left, right = (9, 80), (150, 80)                     # on the ring, either side
    level = gauge("level", 0.999)
    assert level.getpixel(left) == level.getpixel(right) and bluish(level.getpixel(left))
    scale = gauge("scale")
    assert scale.getpixel(left) != scale.getpixel(right)
    across = gauge("linear", fill_angle=90.0)            # left to right
    assert reddish(across.getpixel(left)) and bluish(across.getpixel(right))


def graph(shape: str, **extra):
    widget = build("graph", {"source": "cpu", "line_color": [RED, BLUE],
                             "line_color_shape": shape, "line_width": 4, **extra},
                   Rect(0, 0, 120, 100))
    widget._source = Fixed(0.5)
    for step in range(130):
        widget.render(float(step))
    return widget.surface


def line_pixel(surface, x: int):
    return max((surface.getpixel((x, y)) for y in range(100)), key=sum)


def test_graph_line_four_ways() -> None:
    across = graph("linear", line_color_angle=90.0)
    assert reddish(line_pixel(across, 3)) and bluish(line_pixel(across, 116))
    level = graph("level")
    assert line_pixel(level, 3) == line_pixel(level, 116)
    scale = graph("scale")                                # by height: a flat line is one colour
    assert line_pixel(scale, 3) == line_pixel(scale, 116)


@pytest.mark.parametrize("shape", ["linear", "level", "scale"])
def test_spectrum_four_ways(shape: str) -> None:
    options = {"color": [RED, BLUE], "color_shape": shape, "gap": 1}
    if shape == "linear":
        options["color_angle"] = 90.0
    widget, feed = spectrum(options)
    feed.frame = (MAX,) * 16
    widget.render(0.0)
    first, last = widget.surface.getpixel((3, 60)), widget.surface.getpixel((155, 60))
    if shape == "linear":
        assert reddish(first) and bluish(last)
    else:
        assert first == last                              # every bar full: same colour


def test_one_colour_has_no_layout_to_choose() -> None:
    from akp02d.plugins import lookup

    schema = lookup("bar").schema
    assert not schema["fill_shape"].applies(schema.effective({"fill": "#ff0000"}))
    assert schema["fill_shape"].applies(schema.effective({"fill": [RED, BLUE]}))
    assert not schema["fill_angle"].applies(schema.effective({"fill": [RED, BLUE]}))
    assert schema["fill_angle"].applies(
        schema.effective({"fill": [RED, BLUE], "fill_shape": "linear"}))
