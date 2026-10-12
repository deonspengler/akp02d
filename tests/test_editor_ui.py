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
