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


# -- pictures from a source: cover art --

import base64  # noqa: E402
import http.server  # noqa: E402
import io  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from akp02d import artwork, media  # noqa: E402
from akp02d.plugins.base import set_dry_run  # noqa: E402


def png_bytes(colour, size=(30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


def data_url(colour, padded=True) -> str:
    encoded = base64.b64encode(png_bytes(colour)).decode()
    return "data:image/png;base64," + (encoded if padded else encoded.rstrip("="))


@pytest.fixture(autouse=True)
def fresh_artwork():
    artwork.clear()
    yield
    artwork.clear()


def test_artwork_from_data_file_and_path(tmp_path: Path) -> None:
    assert artwork.picture(data_url(RED)).getpixel((5, 5))[:3] == RED
    assert artwork.picture(data_url(GREEN, padded=False)).getpixel((5, 5))[:3] == GREEN
    cover = tmp_path / "my cover.png"
    cover.write_bytes(png_bytes(BLUE))
    url = "file://" + str(cover).replace(" ", "%20")
    assert artwork.picture(url).getpixel((5, 5))[:3] == BLUE
    assert artwork.picture(str(cover)).getpixel((5, 5))[:3] == BLUE
    # A player that rewrites one file per track: the new picture is read.
    cover.write_bytes(png_bytes(RED, (31, 31)))
    assert artwork.picture(url).getpixel((5, 5))[:3] == RED
    assert artwork.picture("") is None
    assert artwork.picture("data:image/png;base64,bm90IGEgcGljdHVyZQ==") is None
    assert artwork.picture("file:///no/such/cover.jpg") is None


def test_artwork_download_never_blocks(tmp_path: Path) -> None:
    body = png_bytes(GREEN)
    release = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            release.wait(5)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/cover.png"
        started = time.monotonic()
        assert artwork.picture(url) is artwork.PENDING
        assert artwork.picture(url) is artwork.PENDING     # one download, not two
        assert time.monotonic() - started < 0.5
        release.set()
        end = time.monotonic() + 5
        while (got := artwork.picture(url)) is artwork.PENDING and time.monotonic() < end:
            time.sleep(0.02)
        assert got.getpixel((5, 5))[:3] == GREEN
    finally:
        server.shutdown()


class FakeSource:
    def __init__(self) -> None:
        self.art = ""

    def read(self, now):
        return 0.0, {"art": self.art}

    def close(self) -> None:
        pass


def art_widget(path: Path, options: dict, monkeypatch):
    monkeypatch.setattr(media, "installed", lambda: True)
    set_dry_run(True)     # no playerctl: the source is swapped below
    try:
        w = widget(path, {"source": "media", **options}, size=(60, 60))
    finally:
        set_dry_run(False)
    w._source.close()
    w._source = fake = FakeSource()
    return w, fake


def test_cover_art_with_fallback(pictures: Path, monkeypatch) -> None:
    Image.new("RGB", (20, 20), BLUE).save(pictures / "no-cover.png")
    w, source = art_widget(pictures, {"path": "no-cover.png", "fit": "stretch"},
                           monkeypatch)
    centre = (30, 30)
    assert w.render(0.0) and w.surface.getpixel(centre) == BLUE   # nothing playing
    source.art = data_url(RED)
    assert w.render(1.0) and w.surface.getpixel(centre) == RED
    assert w.render(2.0) is False                                  # same cover: no redraw
    # Next track's cover still downloading: the old one stays up.
    source.art = "https://example.invalid/next.jpg"
    monkeypatch.setattr(artwork, "picture",
                        lambda url, real=artwork.picture: artwork.PENDING
                        if url.startswith("https") else real(url))
    assert w.render(3.0) is False and w.surface.getpixel(centre) == RED
    source.art = data_url(GREEN)
    assert w.render(4.0) and w.surface.getpixel(centre) == GREEN
    source.art = ""                                                # stopped
    assert w.render(5.0) and w.surface.getpixel(centre) == BLUE


def test_cover_art_without_fallback(pictures: Path, monkeypatch) -> None:
    w, source = art_widget(pictures, {}, monkeypatch)
    w.render(0.0)
    assert w.surface.getpixel((30, 30)) == BG
    source.art = data_url(RED)
    assert w.render(1.0) and w.surface.getpixel((30, 30)) == RED


def test_needs_a_path_or_a_source(pictures: Path) -> None:
    with pytest.raises(PluginError, match="needs a picture"):
        widget(pictures, {})


def test_playerctl_line_carries_art() -> None:
    line = media.SEP.join(("Playing", "spotify", "1000000", "2000000", "A", "T", "Al",
                           "https://i.scdn.co/image/abc"))
    assert media.parse(line, 0.0).art == "https://i.scdn.co/image/abc"
    assert media.parse(media.SEP.join(("Playing", "x", "0", "0", "A", "T", "Al")), 0.0).art == ""
