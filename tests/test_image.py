"""The image widget: fitting, transparency, corners, animation, reload."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from akp02 import Orientation
from akp02d import app as app_module
from akp02d import theme as theme_module
from akp02d.layout import Rect
from akp02d.plugins import PluginError, lookup
from akp02d.plugins import image as image_module
from akp02d.plugins.base import WidgetSpec
from akp02d.plugins.image import placement
from akp02d.table import Table

BG = (10, 20, 30)
RED, GREEN, BLUE = (255, 0, 0), (0, 255, 0), (0, 0, 255)


def theme_at(path: Path) -> theme_module.Theme:
    return theme_module.Theme(name="t", path=path, orientation=Orientation.PORTRAIT,
                              background=Image.new("RGB", (462, 1920), BG),
                              font_default=None, widgets=())


def widget(path: Path, options: dict, size=(200, 200), fps=None):
    spec = WidgetSpec("image#1", "image", Rect(0, 0, *size), fps, Table(options))
    return lookup("image")(spec, theme_at(path))


@pytest.fixture
def pictures(tmp_path: Path) -> Path:
    Image.new("RGB", (100, 50), RED).save(tmp_path / "wide.png")
    # Left half opaque green, right half fully transparent.
    half = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    half.paste((*GREEN, 255), (0, 0, 20, 40))
    half.save(tmp_path / "half.png")
    frames = [Image.new("RGB", (20, 20), c) for c in (RED, GREEN, BLUE)]
    frames[0].save(tmp_path / "anim.gif", save_all=True, append_images=frames[1:],
                   duration=100, loop=0)
    return tmp_path


def test_placement() -> None:
    box = (200, 100)
    assert placement((100, 100), box, "contain", (0.5, 0.5)) == (50, 0, 100, 100)
    assert placement((100, 100), box, "contain", (0.0, 0.5)) == (0, 0, 100, 100)
    assert placement((100, 100), box, "cover", (0.5, 0.5)) == (0, -50, 200, 200)
    assert placement((100, 100), box, "cover", (0.5, 0.0)) == (0, 0, 200, 200)
    assert placement((100, 100), box, "stretch", (0.5, 0.5)) == (0, 0, 200, 100)
    assert placement((40, 40), box, "none", (1.0, 1.0)) == (160, 60, 40, 40)


def test_contain_draws_once_over_the_background(pictures: Path) -> None:
    w = widget(pictures, {"path": "wide.png"})
    assert w.render(0.0) is True
    assert w.render(5.0) is False                 # a still picture costs nothing after
    assert w.surface.getpixel((100, 100)) == RED
    assert w.surface.getpixel((100, 10)) == BG     # spare room above and below
    top = widget(pictures, {"path": "wide.png", "align": "top"})
    top.render(0.0)
    assert top.surface.getpixel((100, 10)) == RED


def test_transparency_opacity_and_corners(pictures: Path) -> None:
    w = widget(pictures, {"path": "half.png", "scaling": "pixel"})
    w.render(0.0)
    assert w.surface.getpixel((50, 100)) == GREEN
    assert w.surface.getpixel((150, 100)) == BG    # transparent half shows through
    faded = widget(pictures, {"path": "wide.png", "opacity": 0.5})
    faded.render(0.0)
    r, g, b = faded.surface.getpixel((100, 100))
    assert abs(r - (255 + BG[0]) / 2) <= 1 and abs(b - BG[2] / 2) <= 1
    round_ = widget(pictures, {"path": "wide.png", "fit": "stretch", "radius": 40})
    round_.render(0.0)
    assert round_.surface.getpixel((1, 1)) == BG and round_.surface.getpixel((100, 100)) == RED


def test_animation_plays_at_its_own_timing(pictures: Path) -> None:
    w = widget(pictures, {"path": "anim.gif"}, size=(40, 40))
    assert w.fps == pytest.approx(10.0) and w.fps_auto
    colours = []
    for at in (0.0, 0.05, 0.15, 0.25, 0.35):
        changed = w.render(100.0 + at)
        colours.append((changed, w.surface.getpixel((20, 20))[:3]))
    assert [c for _, c in colours] == [RED, RED, GREEN, BLUE, RED]   # loops after 0.3 s
    assert [changed for changed, _ in colours] == [True, False, True, True, True]
    still = widget(pictures, {"path": "anim.gif", "animate": False}, size=(40, 40))
    assert len(still._frames) == 1 and not still.fps_auto
    pinned = widget(pictures, {"path": "anim.gif"}, size=(40, 40), fps=4.0)
    assert pinned.fps == 4.0 and not pinned.fps_auto


def test_long_animation_is_thinned_evenly(pictures: Path, monkeypatch) -> None:
    monkeypatch.setattr(image_module, "MAX_BYTES", 40 * 40 * 3 * 2)
    w = widget(pictures, {"path": "anim.gif"}, size=(40, 40))
    assert len(w._frames) == 2
    assert w._ends[-1] == pytest.approx(0.3)     # same length, fewer frames


def test_missing_and_bad_files(pictures: Path) -> None:
    with pytest.raises(PluginError, match="no file 'nope.png'"):
        widget(pictures, {"path": "nope.png"})
    (pictures / "junk.png").write_text("not a picture")
    with pytest.raises(PluginError, match="cannot read 'junk.png'"):
        widget(pictures, {"path": "junk.png"})
    with pytest.raises(Exception, match="path"):
        widget(pictures, {})


def test_editing_the_picture_reloads(pictures: Path) -> None:
    (pictures / "theme.toml").write_text(
        '[theme]\norientation = "portrait"\n\n[[widget.image]]\n'
        'x = 0\ny = 0\nwidth = 40\nheight = 40\npath = "half.png"\n')
    theme = theme_module.load(pictures)
    before = app_module._stamp(theme)
    Image.new("RGB", (40, 40), BLUE).save(pictures / "half.png")
    import os
    os.utime(pictures / "half.png", ns=(1, 1))
    assert app_module._stamp(theme) != before
