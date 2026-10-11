"""The daemon reloads a theme whose files change, and keeps a bad one out."""

from __future__ import annotations

import io
import os
import threading
import time
from pathlib import Path

from PIL import Image

from akp02 import Orientation
from akp02d import config, theme as theme_module
from akp02d.app import App

THEME = """\
[theme]
orientation = "portrait"
background = "#000000"

[[widget.label]]
x = 0
y = 0
width = 200
height = 40
format = "{text}"
"""


class FakePanel:
    """Just enough of AKP02 for App: records what it was asked to show."""

    def __init__(self) -> None:
        self.full: list[Image.Image] = []
        self.regions = 0
        self.orientations: list[Orientation] = []
        self.inverted = False

    def orientation(self, value: Orientation) -> None:
        self.orientations.append(value)

    def set_brightness(self, value: int) -> None:
        pass

    def screen_on(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def encode_region(self, image: Image.Image) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        return buffer.getvalue()

    def show(self, image, at=None, size=None) -> None:
        if at is None:
            self.full.append(image.copy())
        else:
            self.regions += 1


def write(path: Path, text: str) -> None:
    file = path / "theme.toml"
    file.write_text(text)
    # Make sure the stamp moves even on a coarse-mtime filesystem.
    stamp = time.time_ns() + 10_000_000
    os.utime(file, ns=(stamp, stamp))


def wait_for(predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_reload(tmp_path: Path, caplog) -> None:
    write(tmp_path, THEME.format(text="one"))
    panel = FakePanel()
    app = App(config.Config(), theme_module.load(tmp_path), panel)  # type: ignore[arg-type]
    app.prepare()
    assert len(panel.full) == 1
    runner = threading.Thread(target=app.run, daemon=True)
    runner.start()
    try:
        # A good change: the theme is swapped and the panel repainted.
        write(tmp_path, THEME.format(text="two").replace("#000000", "#102030"))
        assert wait_for(lambda: len(panel.full) == 2)
        assert panel.full[-1].getpixel((300, 300)) == (16, 32, 48)

        # A broken save: logged, and the running theme stays.
        widgets = app.widgets
        write(tmp_path, THEME.format(text="three").replace("width = 200", "width = 0"))
        assert wait_for(lambda: "did not load" in caplog.text)
        assert app.widgets is widgets
        assert len(panel.full) == 2

        # An overlap is a layout error, refused the same way.
        caplog.clear()
        overlap = THEME.format(text="a") + THEME.split("\n\n", 1)[1].format(text="b")
        write(tmp_path, overlap)
        assert wait_for(lambda: "did not load" in caplog.text)
        assert len(panel.full) == 2

        # Fixed again: it comes back, orientation change included.
        # (Landscape binds y: y + height must be 6 mod 8.)
        write(tmp_path, THEME.format(text="four").replace("portrait", "landscape")
              .replace("y = 0", "y = 6"))
        assert wait_for(lambda: len(panel.full) == 3)
        assert panel.orientations[-1] is Orientation.LANDSCAPE
        assert panel.full[-1].size == (1920, 462)
    finally:
        app.stop()
        runner.join(timeout=5)
        app.close()


def test_no_watch(tmp_path: Path) -> None:
    write(tmp_path, THEME.format(text="one"))
    panel = FakePanel()
    app = App(config.Config(), theme_module.load(tmp_path), panel, watch=False)  # type: ignore[arg-type]
    app.prepare()
    runner = threading.Thread(target=app.run, daemon=True)
    runner.start()
    try:
        write(tmp_path, THEME.format(text="two"))
        time.sleep(1.6)
        assert len(panel.full) == 1
    finally:
        app.stop()
        runner.join(timeout=5)
        app.close()
