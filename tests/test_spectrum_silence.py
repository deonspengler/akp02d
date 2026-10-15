"""A silent spectrum sweeps after a while; sound brings the bars back."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from akp02 import Orientation
from akp02d.layout import Rect
from akp02d.plugins import lookup
from akp02d.plugins import spectrum as spectrum_module
from akp02d.plugins.base import WidgetSpec, set_preview
from akp02d.plugins.cava import MAX
from akp02d.table import Table
from akp02d.theme import Theme

BG = (0, 0, 0)


class Feed:
    """Stands in for cava: hands over whatever frame it is given."""

    def __init__(self, bars: int) -> None:
        self.frame: tuple[int, ...] | None = (0,) * bars

    def read(self):
        return self.frame

    def close(self) -> None:
        pass


def spectrum(options: dict, bars: int = 16):
    theme = Theme(name="t", path=Path("."), orientation=Orientation.PORTRAIT,
                  background=Image.new("RGB", (462, 1920), BG), font_default=None,
                  widgets=())
    spec = WidgetSpec("spectrum#1", "spectrum", Rect(0, 0, 160, 80), None,
                      Table({"bars": bars, "channels": "mono", **options}))
    set_preview(True)
    try:
        widget = lookup("spectrum")(spec, theme)
    finally:
        set_preview(False)
    widget._stream = feed = Feed(bars)
    return widget, feed


def lit(widget) -> int:
    return sum(1 for x in range(160) for y in range(80) if widget.surface.getpixel((x, y)) != BG)


def test_silence_then_sweep_then_sound() -> None:
    widget, feed = spectrum({})
    widget.render(0.0)
    assert lit(widget) == 0
    widget.render(4.9)
    assert lit(widget) == 0                          # not yet: a pause between tracks
    widget.render(5.6)
    first = widget.surface.copy()
    assert lit(widget) > 0                           # sweeping, fading in
    widget.render(6.6)
    assert widget.surface.tobytes() != first.tobytes()   # and moving
    feed.frame = tuple([MAX // 2] * 16)              # sound: the bars, at once
    widget.render(6.7)
    widget._last = None
    feed.frame = (0,) * 16
    widget.render(6.8)
    assert lit(widget) == 0                          # silent again: the clock restarts
    widget.render(11.9)
    assert lit(widget) > 0


def test_silence_counts_when_cava_sends_nothing() -> None:
    widget, feed = spectrum({})
    feed.frame = None
    assert widget.render(0.0) is False
    assert widget.render(6.0) is True and lit(widget) > 0


def test_when_silent_none_stays_flat() -> None:
    widget, _ = spectrum({"when_silent": "none"})
    for now in (0.0, 6.0, 9.0):
        widget.render(now)
    assert lit(widget) == 0


def test_sweep_moves_across_and_loops() -> None:
    bars = 32
    period = spectrum_module.SWEEP_PERIOD_SEC
    peak = lambda now: max(range(bars), key=lambda i: spectrum_module._sweep(bars, now, 5.0)[i])  # noqa: E731
    positions = [peak(t * period / 4) for t in range(4)]
    assert positions == sorted(positions)            # left to right
    assert peak(0.0) == peak(period)                 # and round again
    faint = max(spectrum_module._sweep(bars, 1.0, 0.1))
    full = max(spectrum_module._sweep(bars, 1.0, 2.0))
    assert faint < full                              # fades in
