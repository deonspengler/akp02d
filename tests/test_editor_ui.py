"""The editor window, driven headless: no stray top-level windows.

A widget with no parent that is made visible becomes a window of its
own, even if only until it is put in a layout. A floating window manager
barely shows that; a tiling one (bspwm, i3) maps and tiles every one, so
it reads as windows flashing, and can leave an empty tile behind. This
drives the actions that rebuild the UI and fails on any such window.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtCore = pytest.importorskip("PySide6.QtCore")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

import akp02d  # noqa: E402
from akp02d import config  # noqa: E402
from akp02d.editor import controls, style  # noqa: E402
from akp02d.editor.document import Ref, ThemeDocument  # noqa: E402
from akp02d.editor.window import EditorWindow  # noqa: E402


class _Spy(QtCore.QObject):
    def __init__(self) -> None:
        super().__init__()
        self.windows: list[str] = []
        self.allowed: set[int] = set()

    def eventFilter(self, obj, event):  # noqa: N802
        if (event.type() == QtCore.QEvent.Type.Show
                and isinstance(obj, QtWidgets.QWidget) and obj.isWindow()
                and id(obj) not in self.allowed):
            self.windows.append(f"{type(obj).__name__} {obj.objectName()!r}")
        return False


@pytest.fixture
def app():
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    style.apply(instance)
    return instance


def pump(app, seconds: float = 0.1) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def test_no_stray_windows(app, tmp_path: Path) -> None:
    theme = tmp_path / "theme"
    shutil.copytree(Path(akp02d.__file__).parent / "themes" / "default", theme)
    window = EditorWindow(ThemeDocument.open(theme), config.Config())
    window.resize(1200, 800)
    window.show()
    pump(app, 0.3)
    spy = _Spy()
    spy.allowed.add(id(window))
    app.installEventFilter(spy)
    try:
        layers = window.layers
        for index in range(layers.topLevelItemCount()):
            layers.setCurrentItem(layers.topLevelItem(index))
            pump(app)
        window._set_selection(Ref("spectrum", 1))
        pump(app)
        for kind in ("bars", "segmented", "radial"):
            window._widget_edited(Ref("spectrum", 1), "type", kind)
            pump(app)
        gradient = window.inspector.scroll.widget().findChildren(controls.GradientField)[0]
        gradient._add()
        pump(app)
        for plugin in ("label", "gauge", "graph", "spectrum"):
            window.add_widget(plugin)
            pump(app)
        window.undo.undo()
        window.undo.redo()
        pump(app)
        assert spy.windows == []
    finally:
        app.removeEventFilter(spy)
        window.undo.setClean()
        window.close()
        pump(app)


def _window(app, tmp_path):
    theme = tmp_path / "theme"
    shutil.copytree(Path(akp02d.__file__).parent / "themes" / "default", theme)
    window = EditorWindow(ThemeDocument.open(theme), config.Config())
    window.resize(1200, 800)
    window.show()
    pump(app, 0.3)
    return window


def test_reset_button_removes_the_key(app, tmp_path: Path) -> None:
    window = _window(app, tmp_path)
    try:
        window._set_selection(Ref("label", 1))
        pump(app, 0.2)
        field = window.inspector.scroll.widget().findChild(QtWidgets.QWidget, "opt.size")
        reset = [b for b in field.parentWidget().findChildren(QtWidgets.QToolButton)
                 if b.text() == "×"][0]
        assert window.document.entry(Ref("label", 1))["size"] == 32
        reset.click()
        pump(app, 0.2)
        assert "size" not in window.document.entry(Ref("label", 1))
    finally:
        window.undo.setClean()
        window.close()
        pump(app)


def test_changing_source_drops_a_format_that_no_longer_fits(app, tmp_path: Path) -> None:
    window = _window(app, tmp_path)
    try:
        # Fixed text ignores a source: dropped, so the source's default shows.
        window._widget_edited(Ref("label", 1), "source", "hostname")
        pump(app, 0.2)
        assert "format" not in window.document.entry(Ref("label", 1))
        slot = next(s for s in window.engine.slots if s.id == "label#1")
        assert slot.widget.text(0.0) == __import__("socket").gethostname().split(".")[0]
        # Fields the new source lacks: dropped. One undo restores both keys.
        window._widget_edited(Ref("gauge", 1), "source", "temperature")
        pump(app, 0.2)
        assert "format" not in window.document.entry(Ref("gauge", 1))
        window.undo.undo()
        pump(app, 0.2)
        entry = window.document.entry(Ref("gauge", 1))
        assert entry["source"] == "memory" and entry["format"].startswith("{used_gb")
        # A format the new source can fill stays: disk has used_gb and
        # total_gb too.
        window._widget_edited(Ref("gauge", 1), "source", "disk")
        pump(app, 0.2)
        assert window.document.entry(Ref("gauge", 1))["format"] == "{used_gb:.0f}/{total_gb:.0f}G"
    finally:
        window.undo.setClean()
        window.close()
        pump(app)


def test_gradient_sets_color_aside(app, tmp_path: Path) -> None:
    """`color` sits beside `gradient`, greyed while a gradient is set;
    a colour-safe box carries no note, an unsafe one does."""
    (tmp_path / "theme.toml").write_text(
        '[theme]\norientation = "portrait"\n\n[[widget.label]]\n'
        'x = 8\ny = 0\nwidth = 170\nheight = 50\nformat = "Hi"\n'
        'color = "#ff8800"\ngradient = ["#fd44dd", "#11e3f5"]\n')
    window = EditorWindow(ThemeDocument.open(tmp_path), config.Config())
    try:
        window.show()
        ref = Ref("label", 1)
        window._set_selection(ref)
        pump(app, 0.2)

        def colour_field():
            return window.inspector.findChild(QtWidgets.QWidget, "opt.color")

        def labels():
            return [w.text() for w in window.inspector.findChildren(QtWidgets.QLabel)]

        assert not colour_field().isEnabled()
        names = [t for t in labels() if t in ("font", "size", "color", "gradient")]
        assert names.index("gradient") == names.index("color") + 1
        assert not any("olour-safe" in t for t in labels())

        # Only the choices are listed; the default shows as selected.
        direction = window.inspector.findChild(QtWidgets.QComboBox, "opt.gradient_direction")
        items = [direction.itemText(i) for i in range(direction.count())]
        assert items == ["vertical", "horizontal"]
        assert direction.currentText() == "vertical"
        fit = window.inspector.findChild(QtWidgets.QComboBox, "opt.fit")
        assert fit.currentText() == "clip" and fit.count() == 3

        window._widget_edited(ref, "gradient", None)
        pump(app, 0.2)
        assert colour_field().isEnabled()

        window._widget_edited(ref, "x", 9)
        pump(app, 0.2)
        assert any("is not colour-safe" in t for t in labels())
    finally:
        window.undo.setClean()
        window.close()


def test_add_image_asks_for_the_picture(app, tmp_path: Path, monkeypatch) -> None:
    """Adding an image starts with the file: copied into the theme's
    folder, and the box takes the picture's shape."""
    from PIL import Image

    theme = tmp_path / "theme"
    shutil.copytree(Path(akp02d.__file__).parent / "themes" / "default", theme)
    elsewhere = tmp_path / "pictures"
    elsewhere.mkdir()
    Image.new("RGB", (400, 200), (200, 40, 40)).save(elsewhere / "logo.png")
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        staticmethod(lambda *a, **k: (str(elsewhere / "logo.png"), "")))
    window = EditorWindow(ThemeDocument.open(theme), config.Config())
    try:
        window.show()
        pump(app, 0.2)
        count = len(window.engine.slots)
        window.add_widget("image")
        pump(app, 0.3)
        assert (theme / "logo.png").is_file()
        assert len(window.engine.slots) == count + 1
        slot = window.engine.slots[-1]
        assert slot.plugin == "image" and slot.widget is not None
        assert slot.rect.width == 200 and 96 <= slot.rect.height <= 104
        path_field = window.inspector.findChild(QtWidgets.QLineEdit, "opt.path")
        assert path_field is not None and path_field.text() == "logo.png"
        # Cancelling the dialog adds nothing.
        monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: ("", "")))
        window.add_widget("image")
        pump(app, 0.2)
        assert len(window.engine.slots) == count + 1
    finally:
        window.undo.setClean()
        window.close()
