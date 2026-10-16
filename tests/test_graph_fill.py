"""A graph's fill: one colour, as before, or a gradient."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from akp02 import Orientation
from akp02d.layout import Rect
from akp02d.plugins import lookup
from akp02d.plugins.base import WidgetSpec
from akp02d.table import Table
from akp02d.theme import Theme

BG = (0, 0, 0)


class Steady:
    """A scalar source that always reads half."""

    kind = "scalar"
    range = (0.0, 1.0)

    def read(self, now):
        return 0.5, {}


def graph(options: dict):
    theme = Theme(name="t", path=Path("."), orientation=Orientation.PORTRAIT,
                  background=Image.new("RGB", (462, 1920), BG), font_default=None,
                  widgets=())
    spec = WidgetSpec("graph#1", "graph", Rect(0, 0, 120, 100), None,
                      Table({"source": "cpu", "gradient": ["#ffffff"], **options}))
    widget = lookup("graph")(spec, theme)
    widget._source = Steady()
    for step in range(130):
        widget.render(float(step))
    return widget.surface


def test_one_colour_fill_is_unchanged() -> None:
    surface = graph({"fill": "#204060"})
    assert surface.getpixel((60, 90)) == (0x20, 0x40, 0x60)


def test_gradient_fill_runs_top_to_bottom_by_default() -> None:
    surface = graph({"fill": ["#ff0000", "#0000ff"]})
    high, low = surface.getpixel((60, 55)), surface.getpixel((60, 97))
    assert high[0] > high[2] * 0.6 and low[2] > low[0] * 3   # red above, blue below
    assert surface.getpixel((60, 20)) == BG                   # nothing above the line


def test_gradient_fill_angle_and_alpha() -> None:
    surface = graph({"fill": ["#ff0000", "#0000ff"], "fill_angle": 90.0})
    left, right = surface.getpixel((3, 80)), surface.getpixel((116, 80))
    assert left[0] > left[2] and right[2] > right[0]          # left to right
    faint = graph({"fill": ["#ff0000", "#0000ff"], "fill_angle": 90.0, "fill_alpha": 0.5})
    assert faint.getpixel((3, 80))[0] < left[0]
