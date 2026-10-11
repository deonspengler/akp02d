"""The editor's preview engine: the daemon's pixels, without the panel."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from akp02 import Orientation
from akp02d import config, layout, theme as theme_module
from akp02d.app import App
from akp02d.editor import simulate
from akp02d.editor.document import Ref, ThemeDocument
from akp02d.editor.engine import ERROR, WARNING, Encoding, Engine, as_sent_image, snap
from akp02d.layout import Rect

STATIC = """\
[theme]
orientation = "portrait"
background = "#203040"

[[widget.label]]
x = 8
y = 10
width = 200
height = 40
format = "fixed text"
background = "#ff0000"
alpha = 0.5

[[widget.label]]
x = 8
y = 60
width = 200
height = 40
format = "second"
size = 30
color = "#00ff00"
"""


def engine_for(text: str, path: Path, **kw) -> tuple[Engine, ThemeDocument]:
    (path / "theme.toml").write_text(text)
    doc = ThemeDocument.open(path)
    engine = Engine(**kw)
    engine.load(doc)
    return engine, doc


def test_matches_daemon_frame(tmp_path: Path) -> None:
    """For a theme with no moving data, the preview and the daemon's
    composite are the same pixels: both are the same widget code."""
    engine, _ = engine_for(STATIC, tmp_path)
    engine.tick(0.0)

    class Panel:
        inverted = False
        def orientation(self, _): pass
        def set_brightness(self, _): pass
        def screen_on(self): pass
        def show(self, image, at=None, size=None): self.shown = image

    app = App(config.Config(), theme_module.load(tmp_path), Panel())  # type: ignore[arg-type]
    app.prepare()
    for widget in app.widgets:
        widget.render(0.0)
        app.screen.paste(widget.surface, widget.rect.at)
    assert engine.frame().tobytes() == app.screen.tobytes()


def test_bad_widget_does_not_stop_others(tmp_path: Path) -> None:
    text = STATIC.replace('size = 30', 'size = "big"')
    engine, _ = engine_for(text, tmp_path)
    errors = [p for p in engine.problems() if p.level == ERROR]
    assert [p.widget for p in errors] == ["label#2"]
    assert "size must be an integer" in errors[0].message
    assert engine.slots[0].widget is not None and engine.slots[1].widget is None


def test_machine_specific_settings_are_warnings(tmp_path: Path) -> None:
    text = STATIC + '\n[[widget.graph]]\nx = 8\ny = 120\nwidth = 200\nheight = 60\n' \
        'source = "network"\ninterface = "no-such-if0"\n'
    engine, _ = engine_for(text, tmp_path)
    problems = engine.problems()
    assert not [p for p in problems if p.level == ERROR]
    warning = next(p for p in problems if p.widget == "graph#1")
    assert warning.level == WARNING and "no-such-if0" in warning.message
    assert not warning.message.startswith("widget ")


def test_layout_problems(tmp_path: Path) -> None:
    text = STATIC.replace("x = 8\ny = 60", "x = 9\ny = 30")
    engine, _ = engine_for(text, tmp_path)
    messages = [(p.widget, p.message) for p in engine.problems()]
    assert ("label#2", "x=9 is not colour-safe; nearest: 8 or 16") in messages
    assert ("label#1", "overlaps 'label#2'") in messages


def test_unchanged_widgets_are_reused(tmp_path: Path) -> None:
    engine, doc = engine_for(STATIC, tmp_path)
    first, second = (slot.widget for slot in engine.slots)
    doc.set(Ref("label", 2), "format", "changed")
    engine.load(doc)
    assert engine.slots[0].widget is first
    assert engine.slots[1].widget is not second


def test_theme_error_keeps_last_background(tmp_path: Path) -> None:
    engine, doc = engine_for(STATIC, tmp_path)
    doc.set_theme("background", "octarine")
    engine.load(doc)
    assert engine.theme_error and "octarine" in engine.theme_error
    assert engine.frame().getpixel((400, 1000)) == (32, 48, 64)


@pytest.mark.parametrize("orientation,inverted", [
    (Orientation.PORTRAIT, False), (Orientation.PORTRAIT, True),
    (Orientation.LANDSCAPE, False), (Orientation.LANDSCAPE, True)])
def test_snap_always_lands_colour_safe(orientation, inverted) -> None:
    screen = layout.screen_size(orientation)
    for x in range(0, 40, 3):
        for y in range(0, 40, 5):
            for width in (50, 77):
                for mode in ("move", "r", "l", "t", "b", "rb"):
                    start = Rect(x, y, width, 60)
                    rect = snap(start, orientation, inverted, screen, mode)
                    axis, _, _, reflected = layout.bound_axis(start, orientation, inverted)
                    # The rule constrains one edge: the near one, or the
                    # far one on a reflected axis.
                    edge = {("x", False): "l", ("x", True): "r",
                            ("y", False): "t", ("y", True): "b"}[(axis, reflected)]
                    reaches = mode == "move" or edge in mode
                    if reaches:
                        assert layout.is_aligned(rect, orientation, inverted), (rect, mode)
                    else:
                        # Dragging an edge on the free axis cannot fix the
                        # bound one, and does not move the box to try.
                        assert (rect.x, rect.y) == (start.x, start.y)


def test_snap_moves_only_what_is_dragged() -> None:
    screen = layout.screen_size(Orientation.PORTRAIT)
    assert snap(Rect(59, 190, 160, 160), Orientation.PORTRAIT, False, screen) == \
        Rect(56, 190, 160, 160)
    # Inverted portrait binds the right edge: resizing it snaps the width.
    assert snap(Rect(56, 190, 163, 160), Orientation.PORTRAIT, True, screen, "r") == \
        Rect(56, 190, 166, 160)


def test_as_sent_roundtrip() -> None:
    image = Image.new("RGB", (462, 1920), (200, 30, 90))
    for orientation in (Orientation.PORTRAIT, Orientation.LANDSCAPE):
        size = layout.screen_size(orientation)
        source = image.resize(size)
        result, nbytes = as_sent_image(source, orientation, False, Encoding(80, 0))
        assert result.size == size and nbytes > 0
        r, g, b = result.getpixel((10, 10))
        assert abs(r - 200) < 6 and abs(g - 30) < 6 and abs(b - 90) < 6


def test_frozen_and_simulated_data(tmp_path: Path) -> None:
    text = STATIC + '\n[[widget.gauge]]\nx = 8\ny = 120\nwidth = 120\nheight = 120\n' \
        'source = "memory"\nformat = "{percent:.0f}%"\n' \
        '\n[[widget.graph]]\nx = 8\ny = 260\nwidth = 200\nheight = 60\nsource = "cpu"\n'
    settings = simulate.Settings(data=simulate.FROZEN)
    settings.frozen.memory = 0.25
    engine, _ = engine_for(text, tmp_path, settings=settings)
    gauge = next(s for s in engine.slots if s.plugin == "gauge").widget
    value, fields = gauge._source.read(0.0)
    assert value == 0.25 and fields["percent"] == 25
    graph = next(s for s in engine.slots if s.plugin == "graph").widget
    # Seeded: full of history before the first tick.
    assert len(graph._history) == graph._history.maxlen - 1
    assert set(graph._history) == {settings.frozen.cpu}
    settings.data = simulate.SIMULATED
    engine.reseed()
    assert len(set(graph._history)) > 10


def test_simulated_audio_shape() -> None:
    settings = simulate.Settings()
    audio = simulate.Audio(32, True, settings)
    frame = audio.read()
    assert frame is not None and len(frame) == 32
    assert all(0 <= v <= 65535 for v in frame)
    settings.audio = simulate.SILENCE
    for _ in range(60):
        frame = audio.read()
    assert max(frame) < 2000
