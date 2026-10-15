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


def peak(sweep, now: float) -> int:
    sweep.reset()                                    # no tail from an earlier call
    frame = sweep.frame(now, 5.0)
    return max(range(len(frame)), key=frame.__getitem__)


def test_sweep_goes_there_and_back() -> None:
    period = spectrum_module.SWEEP_PERIOD_SEC
    sweep = spectrum_module.Sweep(32, stereo=False)
    out = [peak(sweep, period * (0.1 + 0.2 * t)) for t in range(5)]
    back = [peak(sweep, period * (1.1 + 0.2 * t)) for t in range(5)]
    assert out == sorted(out) and out[0] < out[-1]          # left to right
    assert back == sorted(back, reverse=True)               # then right to left
    assert peak(sweep, period * 0.3) == peak(sweep, period * 2.3)   # and again
    # It turns at the end rather than jumping: no step bigger than the
    # pass would make anyway.
    path = [peak(sweep, period * (0.9 + i / 30)) for i in range(7)]
    assert max(abs(b - a) for a, b in zip(path, path[1:])) <= 2
    sweep.reset()
    assert max(sweep.frame(0.0, 5.0)) == MAX                # a full-height peak


def test_sweep_trails_and_fades_in() -> None:
    sweep = spectrum_module.Sweep(32, stereo=False)
    t = 0.0
    while t < 1.0:
        frame = sweep.frame(t, 5.0)
        t += 1 / 30
    frame = sweep.frame(t, 5.0)
    head = max(range(32), key=frame.__getitem__)
    behind = [frame[i] for i in range(max(0, head - 8), max(0, head - 3))]
    assert behind and all(0 < level < MAX for level in behind)   # a fading tail
    fresh = spectrum_module.Sweep(32, stereo=False)
    assert max(fresh.frame(1.0, 0.1)) < max(fresh.frame(1.0 + 1 / 30, 2.0))


def test_stereo_sweep_runs_out_from_the_middle() -> None:
    sweep = spectrum_module.Sweep(32, stereo=True)
    period = spectrum_module.SWEEP_PERIOD_SEC
    frame = sweep.frame(period * 0.1, 5.0)
    left = max(range(16), key=frame.__getitem__)
    right = max(range(16, 32), key=frame.__getitem__)
    assert abs(left - (31 - right)) <= 1             # mirrored
    sweep.reset()
    later = sweep.frame(period * 0.5, 5.0)
    assert max(range(16), key=later.__getitem__) < left    # moving outward
    sweep.reset()
    edge = sweep.frame(period * 0.95, 5.0)
    sweep.reset()
    home = sweep.frame(period * 1.9, 5.0)
    assert max(range(16), key=home.__getitem__) > max(range(16), key=edge.__getitem__)  # back in


# -- channels --


def cava_text(monkeypatch, options: dict) -> str:
    """The cava config a spectrum would start cava with."""
    from akp02d.plugins import cava

    seen: list[str] = []
    real = cava.config
    monkeypatch.setattr(cava, "config", lambda *a: seen.append(real(*a)) or seen[-1])
    theme = Theme(name="t", path=Path("."), orientation=Orientation.PORTRAIT,
                  background=Image.new("RGB", (462, 1920), BG), font_default=None,
                  widgets=())
    spec = WidgetSpec("spectrum#1", "spectrum", Rect(0, 0, 160, 80), None,
                      Table({"bars": 16, **options}))
    set_preview(True)
    try:
        lookup("spectrum")(spec, theme)
    finally:
        set_preview(False)
    return seen[-1]


def output(text: str) -> dict[str, str]:
    section = text.split("[output]")[1].split("[")[0]
    return dict(line.split(" = ", 1) for line in section.strip().splitlines())


def test_the_four_channel_choices(monkeypatch) -> None:
    assert output(cava_text(monkeypatch, {}))["channels"] == "stereo"
    for choice, mono in (("both", "average"), ("left", "left"), ("right", "right")):
        out = output(cava_text(monkeypatch, {"channels": choice}))
        assert out["channels"] == "mono" and out["mono_option"] == mono


def test_an_older_mono_theme_still_loads(monkeypatch) -> None:
    out = output(cava_text(monkeypatch, {"channels": "mono"}))
    assert out["mono_option"] == "average"
    out = output(cava_text(monkeypatch, {"channels": "mono", "mono_option": "right"}))
    assert out["mono_option"] == "right"
    schema = lookup("spectrum").schema
    shown = schema.effective({"channels": "mono", "mono_option": "left"})
    assert shown.channels == "left"                  # what the editor shows
    assert "mono" not in schema["channels"].choices  # and never offers


def test_an_unknown_channel_is_refused(monkeypatch) -> None:
    import pytest
    from akp02d.table import SettingsError

    with pytest.raises(SettingsError, match="stereo, both, left, right"):
        cava_text(monkeypatch, {"channels": "middle"})
