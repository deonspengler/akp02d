"""Gauge styles: arc, dial, vu; needles, zones, glide."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from PIL import Image

from akp02 import Orientation
from akp02d.layout import Rect
from akp02d.plugins import lookup
from akp02d.plugins.base import WidgetSpec
from akp02d.plugins.gauge import ANIM_FPS, _number
from akp02d.table import SettingsError, Table
from akp02d.theme import Theme

BG = (10, 10, 10)
RED = (255, 0, 0)


class Steady:
    """A source that reads whatever it is told."""

    kind = "scalar"
    range = (0.0, 1.0)

    def __init__(self, value: float = 0.5) -> None:
        self.value = value
        self.reads = 0

    def read(self, now):
        self.reads += 1
        return self.value, {"value": self.value, "percent": self.value * 100}

    def close(self) -> None:
        pass


def gauge(options: dict, size=(200, 200), value: float = 0.5, fps=None):
    theme = Theme(name="t", path=Path("."), orientation=Orientation.PORTRAIT,
                  background=Image.new("RGB", (462, 1920), BG), font_default=None,
                  widgets=())
    spec = WidgetSpec("gauge#1", "gauge", Rect(0, 0, *size), fps,
                      Table({"source": "cpu", **options}))
    widget = lookup("gauge")(spec, theme)
    widget._source = Steady(value)
    return widget


def along(widget, fraction: float, at: float) -> tuple[int, int, int]:
    """The pixel `at` of the radius out along the needle for `fraction`."""
    cx, cy = widget._centre
    angle = math.radians(widget._start + widget._sweep * fraction)
    r = widget._radius * at
    return widget.surface.getpixel((round(cx + r * math.cos(angle)),
                                    round(cy + r * math.sin(angle))))


@pytest.mark.parametrize("value", [0.0, 0.25, 0.5, 0.9, 1.0])
def test_dial_needle_points_at_the_value(value) -> None:
    w = gauge({"style": "dial", "needle_color": "#ff0000", "shadow": False}, value=value)
    assert w.render(0.0)
    assert along(w, value, 0.6) == RED
    other = (value + 0.5) % 1.0
    assert along(w, other, 0.6) != RED


def test_needle_colour_follows_the_value() -> None:
    w = gauge({"style": "dial", "needle_color": ["#0000ff", "#ff0000"], "shadow": False},
              value=1.0)
    w.render(0.0)
    assert along(w, 1.0, 0.6) == RED


def test_arc_needle_takes_the_arc_colour() -> None:
    w = gauge({"needle": True, "gradient": ["#00ff00", "#ff0000"], "color_by": "level"},
              value=1.0)
    w.render(0.0)
    assert along(w, 1.0, 0.5) == RED


def test_vu_has_a_face_and_its_needle_crosses_it() -> None:
    w = gauge({"style": "vu", "needle_color": "#ff0000", "shadow": False,
               "face": "#ece0be"}, size=(380, 180), value=0.5)
    w.render(0.0)
    assert w.surface.getpixel((40, 40)) == (236, 224, 190)       # face
    column = [w.surface.getpixel((190, y)) for y in range(60, 170)]
    assert RED in column                                       # upright at half


def test_zones_colour_the_scale() -> None:
    w = gauge({"style": "dial", "zones": [[0.8, 1.0, "#ff0000"]]}, value=0.1)
    w.render(0.0)
    reds = sum(1 for x in range(200) for y in range(200)
               if w.surface.getpixel((x, y)) == RED)
    assert reds > 50
    plain = gauge({"style": "dial"}, value=0.1)
    plain.render(0.0)
    assert sum(1 for x in range(200) for y in range(200)
               if plain.surface.getpixel((x, y)) == RED) == 0


@pytest.mark.parametrize("zones,message", [
    ([[90, 80, "red"]], "must be greater than from"),
    ([[80, "red"]], "must be \\[from, to, colour\\]"),
    ([[80, 90, "octarine"]], "cannot read"),
    ("red", "a list of"),
])
def test_bad_zones(zones, message) -> None:
    with pytest.raises(SettingsError, match=message):
        gauge({"zones": zones})


def test_glide_swings_then_rests() -> None:
    w = gauge({"style": "dial", "glide": 0.5}, value=0.2)
    assert w.animating and w.period == pytest.approx(1 / ANIM_FPS)
    w.render(0.0)
    assert w._shown == pytest.approx(0.2)                 # no swing in at load
    w._source.value = 0.8                                 # read at 1 s, the next due time
    shown, now = [], 0.0
    while now < 3.0:
        now += 1 / ANIM_FPS
        w.render(now)
        if now >= 1.0:
            shown.append(w._shown)
    assert 0.2 < shown[0] < 0.5 and shown == sorted(shown)   # moves, one way
    assert shown[-1] == pytest.approx(0.8)
    assert w.render(now) is False                         # at rest: nothing to draw
    # The reading is still taken at fps, not at the animation's rate.
    assert w._source.reads <= 5


def test_no_glide_jumps() -> None:
    w = gauge({"style": "dial"}, value=0.2)
    assert not w.animating
    w.render(0.0)
    w._source.value = 0.8
    w.render(1.0)
    assert w._shown == 0.8


def test_scale_numbers() -> None:
    assert _number(0.8, (0.0, 1.0)) == "80"           # a fraction numbered as percent
    assert _number(60.0, (20.0, 100.0)) == "60"
    assert _number(2.5, (0.0, 5.0)) == "2.5"


def test_style_hides_what_it_does_not_use() -> None:
    schema = lookup("gauge").schema
    shown = lambda style, **v: {o.key for o in schema  # noqa: E731
                                if o.applies({"style": style, **v})}
    assert "thickness" in shown("arc") and "face" not in shown("arc")
    assert "needle_color" not in shown("arc") and "needle_color" in shown("arc", needle=True)
    assert "face" in shown("vu") and "gradient" not in shown("vu")
    assert "minor_ticks" not in shown("arc", ticks=0)
    assert "minor_ticks" in shown("dial", ticks=5)
