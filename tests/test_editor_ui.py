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
QtGui = pytest.importorskip("PySide6.QtGui")
QTest = pytest.importorskip("PySide6.QtTest").QTest

import akp02d  # noqa: E402
from akp02d import config  # noqa: E402
from akp02d.editor import controls, style  # noqa: E402
from akp02d.editor.document import Ref, ThemeDocument  # noqa: E402
from akp02d.editor.window import EditorWindow  # noqa: E402
from test_control import daemon, home  # noqa: E402,F401  (fixtures)


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
        window._widget_edited(Ref("label", 4), "source", "hostname")
        pump(app, 0.2)
        assert "format" not in window.document.entry(Ref("label", 4))
        slot = next(s for s in window.engine.slots if s.ref == Ref("label", 4))
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


def test_color_is_one_colour_or_a_gradient(app, tmp_path: Path) -> None:
    """A label's `color` is one option: a colour field until a second
    stop is added, then a gradient with its direction. A colour-safe box
    carries no note, an unsafe one does."""
    (tmp_path / "theme.toml").write_text(
        '[theme]\norientation = "portrait"\n\n[[widget.label]]\n'
        'x = 8\ny = 0\nwidth = 170\nheight = 50\nformat = "Hi"\n'
        'color = "#ff8800"\n')
    window = EditorWindow(ThemeDocument.open(tmp_path), config.Config())
    try:
        window.show()
        ref = Ref("label", 1)
        window._set_selection(ref)
        pump(app, 0.2)

        def field():
            return window.inspector.findChild(controls.GradientField, "opt.color")

        def direction():
            return window.inspector.findChild(controls.AngleField, "opt.color_angle")

        def labels():
            return [w.text() for w in window.inspector.findChildren(QtWidgets.QLabel)]

        assert not field().is_gradient and field()._stops == ["#ff8800"]
        # Just the swatch: no hex text beside it.
        assert not field().findChildren(QtWidgets.QLineEdit)
        assert direction() is None                 # nothing to direct yet
        assert not any("olour-safe" in t for t in labels())
        assert "gradient" not in labels()

        field()._add()
        pump(app, 0.3)
        assert window.document.entry(ref)["color"] == ["#ff8800", "#ff8800"]
        assert field().is_gradient
        # Shape and angle: linear, top to bottom, its arrow pressed.
        shape = window.inspector.findChild(QtWidgets.QComboBox, "opt.color_shape")
        assert [shape.itemText(i) for i in range(shape.count())] == ["linear", "radial"]
        assert direction().edit.placeholderText() == "180"
        assert [b.isChecked() for b in direction().buttons] == [True, False, False, False]
        direction().buttons[3].click()                        # ↙
        pump(app, 0.3)
        assert window.document.entry(ref)["color_angle"] == 225
        window._widget_edited(ref, "color_shape", "radial")
        pump(app, 0.3)
        assert direction() is None                            # radial has no angle
        window._widget_edited(ref, "color_shape", None)
        window._widget_edited(ref, "color_angle", None)
        pump(app, 0.3)
        fit = window.inspector.findChild(QtWidgets.QComboBox, "opt.fit")
        assert fit.currentText() == "clip" and fit.count() == 3

        # Down to one stop: written as a plain colour again.
        f = field()
        del f._stops[1]
        f._emit()
        pump(app, 0.3)
        assert window.document.entry(ref)["color"] == "#ff8800"
        assert direction() is None

        window._widget_edited(ref, "x", 9)
        pump(app, 0.2)
        assert any("is not colour-safe" in t for t in labels())
    finally:
        window.undo.setClean()
        window.close()


def test_add_image_then_pick_where_pictures_come_from(app, tmp_path: Path, monkeypatch) -> None:
    """Adding an image asks nothing: the widget says it needs a picture,
    and either a file or a source gives it one. A file from elsewhere
    is copied into the theme's folder."""
    from PIL import Image

    def never(*a, **k):
        raise AssertionError("no dialog when adding")

    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", staticmethod(never))
    theme = tmp_path / "theme"
    shutil.copytree(Path(akp02d.__file__).parent / "themes" / "default", theme)
    window = EditorWindow(ThemeDocument.open(theme), config.Config())
    try:
        window.show()
        pump(app, 0.2)
        count = len(window.engine.slots)
        window.add_widget("image")
        pump(app, 0.3)
        assert len(window.engine.slots) == count + 1
        slot = window.engine.slots[-1]
        ref = Ref("image", 1)
        assert slot.plugin == "image" and slot.widget is None
        assert any("needs a picture" in p.message for p in window.engine.problems())

        # Cover art: the simulated playlist's covers come as data: URLs.
        window._widget_edited(ref, "source", "now_playing")
        pump(app, 0.3)
        widget = window.engine.slots[-1].widget
        assert widget is not None and widget._art is not None

        # Or a file, through Choose…
        elsewhere = tmp_path / "pictures"
        elsewhere.mkdir()
        Image.new("RGB", (40, 20), (200, 40, 40)).save(elsewhere / "logo.png")
        monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                            staticmethod(lambda *a, **k: (str(elsewhere / "logo.png"), "")))
        window._widget_edited(ref, "source", None)
        pump(app, 0.3)
        row = window.inspector.findChild(QtWidgets.QWidget, "opt.path.row")
        row.findChild(QtWidgets.QToolButton, "browse").click()
        pump(app, 0.3)
        assert (theme / "logo.png").is_file()
        assert window.document.entry(ref)["path"] == "logo.png"
        assert window.engine.slots[-1].widget is not None
    finally:
        window.undo.setClean()
        window.close()


def test_gauge_style_and_zones(app, tmp_path: Path) -> None:
    """Switching a gauge's style swaps its options; zones are added and
    removed as rows, proposed from the reading's range."""
    theme = tmp_path / "theme"
    shutil.copytree(Path(akp02d.__file__).parent / "themes" / "default", theme)
    window = EditorWindow(ThemeDocument.open(theme), config.Config())
    try:
        window.show()
        ref = Ref("gauge", 1)
        window._set_selection(ref)
        pump(app, 0.2)
        find = lambda name: window.inspector.findChild(QtWidgets.QWidget, name)  # noqa: E731
        assert find("opt.thickness") is not None and find("opt.face") is None
        window._widget_edited(ref, "style", "dial")
        pump(app, 0.3)
        assert find("opt.thickness") is None and find("opt.face") is not None
        assert window.engine.slots[2].widget is not None or any(
            s.widget is not None for s in window.engine.slots if s.plugin == "gauge")

        zones = window.inspector.findChild(controls.ZonesField, "opt.zones")
        zones._add()
        pump(app, 0.3)
        assert window.document.entry(ref)["zones"] == [[0.8, 1, "#e63232"]]
        zones = window.inspector.findChild(controls.ZonesField, "opt.zones")
        zones._add()
        pump(app, 0.3)
        assert window.document.entry(ref)["zones"][1][:2] == [0.6, 0.8]
        zones = window.inspector.findChild(controls.ZonesField, "opt.zones")
        zones._remove(0)
        zones._remove(0)
        pump(app, 0.3)
        assert "zones" not in window.document.entry(ref)
        assert not [p for p in window.engine.problems() if p.widget == "gauge#1"
                    and p.level == "error"]
    finally:
        window.undo.setClean()
        window.close()


def test_background_gradient(app, tmp_path: Path) -> None:
    """The panel's background is a colour or a gradient: a second stop
    makes it one and brings out its direction."""
    (tmp_path / "theme.toml").write_text(
        '[theme]\norientation = "portrait"\nbackground = "#000000"\n\n'
        '[[widget.label]]\nx = 8\ny = 0\nwidth = 160\nheight = 48\nformat = "Hi"\n')
    window = EditorWindow(ThemeDocument.open(tmp_path), config.Config())
    try:
        window.show()
        window._set_selection("theme")
        pump(app, 0.2)
        find = lambda name: window.inspector.findChild(QtWidgets.QWidget, name)  # noqa: E731
        assert find("theme.background_direction") is None
        field = find("theme.background")
        field._stops = ["#000000", "#0000ff"]
        field._emit()
        pump(app, 0.4)
        assert window.document.theme["background"] == ["#000000", "#0000ff"]
        angle = find("theme.background_angle")
        assert angle is not None and angle.buttons[0].isChecked()   # ↓ by default
        frame = window.engine.frame()
        assert frame.getpixel((300, 5))[2] < 10 and frame.getpixel((300, 1915))[2] > 245
        angle.buttons[1].click()                                  # →
        pump(app, 0.4)
        frame = window.engine.frame()
        assert frame.getpixel((2, 1000))[2] < 10 and frame.getpixel((459, 1000))[2] > 245
        assert window.document.theme["background_angle"] == 90
        window.edit_theme("background_shape", "radial", "x")
        pump(app, 0.4)
        assert find("theme.background_angle") is None
        frame = window.engine.frame()
        assert frame.getpixel((231, 960))[2] < 10 and frame.getpixel((0, 0))[2] > 245
    finally:
        window.undo.setClean()
        window.close()


def test_apply_to_panel(app, monkeypatch, request) -> None:
    """The editor switches the running daemon to the theme it shows:
    saved first, by name from the themes folder; a theme the daemon
    refuses leaves the panel as it was, and says why."""
    from test_control import add_theme
    request.getfixturevalue("home")
    daemon = request.getfixturevalue("daemon")
    from akp02d import paths

    warnings: list[str] = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda _p, _t, text, *a, **k: warnings.append(text)))
    folder = add_theme("mine")
    window = EditorWindow(ThemeDocument.open(folder), daemon.config)
    try:
        window.show()
        window.refresh_panel_status()
        assert window.status_panel.text() == "Panel: start"
        # An unsaved edit is saved, then shown.
        window._widget_edited(Ref("label", 1), "size", 30)
        pump(app, 0.2)
        assert window.apply_to_panel()
        assert "size = 30" in (folder / "theme.toml").read_text()
        assert window.status_panel.text() == "Panel: mine"
        assert daemon.theme.name == "mine"
        # A mistake: refused, the panel keeps "mine", the reason shown.
        window._widget_edited(Ref("label", 1), "size", "huge")
        pump(app, 0.2)
        assert not window.apply_to_panel()
        assert warnings and "size" in warnings[-1] and "still showing 'mine'" in warnings[-1]
        assert daemon.theme.name == "mine"
        # And to start with it.
        window._widget_edited(Ref("label", 1), "size", 24)
        pump(app, 0.2)
        assert window.apply_to_panel(save=True)
        assert 'theme = "mine"' in paths.config_file().read_text()
    finally:
        window.undo.setClean()
        window.close()


def test_apply_without_a_daemon(app, monkeypatch, request) -> None:
    request.getfixturevalue("home")
    from test_control import add_theme
    shown: list[str] = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        staticmethod(lambda _p, title, *a, **k: shown.append(title)))
    window = EditorWindow(ThemeDocument.open(add_theme("solo")), config.Config())
    try:
        window.show()
        window.refresh_panel_status()
        assert window.status_panel.text() == "Panel: not running"
        assert not window.apply_to_panel() and shown == ["Panel not running"]
    finally:
        window.undo.setClean()
        window.close()


# -- the themes list, and naming instead of file dialogs --


class Answers:
    """Stand-ins for the editor's dialogs, answering from a script."""

    def __init__(self, monkeypatch) -> None:
        self.names: list[str] = []
        self.asked: list[str] = []
        self.questions: list[str] = []
        self.reply = QtWidgets.QMessageBox.StandardButton.Yes
        self.notes: list[str] = []
        monkeypatch.setattr(QtWidgets.QInputDialog, "getText", staticmethod(self._text))
        monkeypatch.setattr(QtWidgets.QMessageBox, "question", staticmethod(self._question))
        monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                            staticmethod(lambda _p, title, text, *a, **k: self.notes.append(text)))
        monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                            staticmethod(lambda _p, title, text, *a, **k: self.notes.append(text)))

        def no_file_dialogs(*a, **k):
            raise AssertionError("no file dialog: themes are named, not browsed for")

        monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName", staticmethod(no_file_dialogs))

    def _text(self, _parent, _title, label, *args, **kwargs):
        self.asked.append(label)
        return (self.names.pop(0), True) if self.names else ("", False)

    def _question(self, _parent, _title, text, *args, **kwargs):
        self.questions.append(text)
        return self.reply


@pytest.fixture
def themes(request, tmp_path, monkeypatch):
    """A themes folder with three themes, a daemon showing 'start', and
    a trash of our own."""
    request.getfixturevalue("home")
    daemon = request.getfixturevalue("daemon")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return daemon


def editor(folder, cfg):
    window = EditorWindow(ThemeDocument.open(folder), cfg)
    window.show()
    window.refresh_panel_status()
    return window


def test_list_shows_the_themes_open_and_on_panel(app, themes) -> None:
    from akp02d import paths
    window = editor(paths.themes_dir() / "neon", themes.config)
    try:
        pump(app, 0.2)
        listed = window.theme_list
        assert listed.names() == ["broken", "default", "neon", "start"]
        item = {listed.list.item(i).text(): listed.list.item(i) for i in range(4)}
        assert item["neon"].font().bold() and not item["start"].font().bold()
        assert not item["start"].icon().isNull()                  # the panel's dot
        assert "On the panel now" in item["start"].toolTip()
        assert "On the panel" not in item["neon"].toolTip()
        # Duplicate and delete act on the open theme; not deletable while
        # it is the one on the panel.
        assert listed.buttons["duplicate"].isEnabled() and listed.buttons["delete"].isEnabled()
        window.edit_named("start")
        assert not listed.buttons["delete"].isEnabled()
        # A theme added behind the editor's back appears on its own.
        shutil.copytree(paths.themes_dir() / "neon", paths.themes_dir() / "later")
        pump(app, 0.6)
        assert "later" in listed.names()
    finally:
        window.undo.setClean()
        window.close()


def test_one_click_opens_a_theme(app, themes, monkeypatch) -> None:
    from akp02d import paths
    answers = Answers(monkeypatch)
    window = editor(paths.themes_dir() / "neon", themes.config)
    try:
        listed = window.theme_list

        def click(name):
            row = listed.names().index(name)
            item = listed.list.item(row)
            QTest.mouseClick(listed.list.viewport(), QtCore.Qt.MouseButton.LeftButton,
                             QtCore.Qt.KeyboardModifier.NoModifier,
                             listed.list.visualItemRect(item).center())
            pump(app, 0.1)

        click("start")
        assert window.document.directory.name == "start"
        assert listed.selected() == "start"
        # Unsaved changes, then a click elsewhere, then Cancel: nothing
        # opens, and the open theme stays the one selected.
        window._widget_edited(Ref("label", 1), "size", 31)
        pump(app, 0.2)
        answers.reply = QtWidgets.QMessageBox.StandardButton.Cancel
        monkeypatch.setattr(QtWidgets.QMessageBox, "exec",
                            lambda self: QtWidgets.QMessageBox.StandardButton.Cancel)
        click("neon")
        assert window.document.directory.name == "start" and listed.selected() == "start"
        # The toolbar's Apply puts the open theme on the panel (saved first).
        window.edit_named("neon")       # leave start ... (Cancel: still on start)
        assert window.document.directory.name == "start"
        assert window.apply_to_panel()
        assert themes.theme.name == "start"
        assert "size = 31" in (paths.themes_dir() / "start" / "theme.toml").read_text()
        # The heading's duplicate icon copies the open theme.
        answers.names = ["start-2"]
        listed.buttons["duplicate"].click()
        assert window.document.directory.name == "start-2"
    finally:
        window.undo.setClean()
        window.close()


def test_open_is_gone(app, themes) -> None:
    from akp02d import paths
    window = editor(paths.themes_dir() / "neon", themes.config)
    try:
        names = [a.text() for a in window.toolbar.actions()]
        assert "Open" not in names and "New" in names and "Save" in names
    finally:
        window.close()


def test_remove_a_gradient_stop_from_the_picker(app, monkeypatch) -> None:
    from akp02d.editor.controls import ColourDialog, GradientField
    field = GradientField()
    field.set_value(["#ff0000", "#00ff00", "#0000ff"])
    sent = []
    field.changed.connect(sent.append)
    chips = [w for w in field.findChildren(controls.Swatch) if w.removable]
    assert len(chips) == 3
    monkeypatch.setattr(ColourDialog, "ask", classmethod(lambda cls, *a: cls.REMOVE))
    chips[1].click()
    assert sent[-1] == ["#ff0000", "#0000ff"]
    # The dialog itself: a Remove colour button only for a removable stop,
    # and the picker inside it rather than a window of its own.
    removable = ColourDialog(QtGui.QColor("#ff0000"), True)
    plain = ColourDialog(QtGui.QColor("#ff0000"), False)
    assert removable.findChild(QtWidgets.QPushButton, "remove") is not None
    assert plain.findChild(QtWidgets.QPushButton, "remove") is None
    assert not removable.picker.isWindow()
    removable._remove()
    assert removable.removed and removable.result() == QtWidgets.QDialog.DialogCode.Accepted


def test_new_theme_is_named_not_browsed(app, themes, monkeypatch) -> None:
    from akp02d import paths
    answers = Answers(monkeypatch)
    window = editor(paths.themes_dir() / "neon", themes.config)
    try:
        answers.names = ["neon", "fresh"]          # taken first: asked again
        window.new_theme()
        assert "already a theme called 'neon'" in answers.asked[1]
        assert (paths.themes_dir() / "fresh" / "theme.toml").is_file()
        assert window.document.directory == paths.themes_dir() / "fresh"
        assert "fresh" in window.theme_list.names()
        # Its edits save in place, nothing asked.
        window.add_widget("label")
        pump(app, 0.2)
        asked = len(answers.asked)
        assert window.save() and len(answers.asked) == asked
        assert "[[widget.label]]" in (paths.themes_dir() / "fresh" / "theme.toml").read_text()
    finally:
        window.undo.setClean()
        window.close()


def test_saving_an_untitled_theme_asks_only_its_name(app, themes, monkeypatch) -> None:
    from akp02d import paths
    answers = Answers(monkeypatch)
    window = EditorWindow(ThemeDocument.new(), themes.config)
    try:
        window.show()
        answers.names = ["bad/name", "untitled-one"]
        assert window.save()
        assert "no slashes" in answers.asked[1]
        assert (paths.themes_dir() / "untitled-one" / "theme.toml").is_file()
        assert window.theme_list.list.currentItem().text() == "untitled-one"
    finally:
        window.undo.setClean()
        window.close()


def test_duplicate_copies_the_whole_folder(app, themes, monkeypatch) -> None:
    from akp02d import paths
    from PIL import Image
    answers = Answers(monkeypatch)
    Image.new("RGB", (4, 4)).save(paths.themes_dir() / "neon" / "art.png")
    window = editor(paths.themes_dir() / "start", themes.config)
    try:
        answers.names = ["neon-2"]
        window.duplicate_named("neon")
        assert answers.asked == ["Name the copy of neon:"]
        copy = paths.themes_dir() / "neon-2"
        assert (copy / "theme.toml").is_file() and (copy / "art.png").is_file()
        assert window.document.directory == copy
    finally:
        window.undo.setClean()
        window.close()


def test_rename_and_delete(app, themes, monkeypatch) -> None:
    from akp02d import config as config_module, paths
    answers = Answers(monkeypatch)
    config_module.save_theme("neon")
    cfg = config_module.load()
    window = editor(paths.themes_dir() / "neon", cfg)
    try:
        # Not the one on the panel.
        window.rename_named("start")
        window.delete_named("start")
        assert (paths.themes_dir() / "start").is_dir() and len(answers.notes) == 2
        # The one akp02d starts with, and open: renamed, config and editor follow.
        answers.names = ["glow"]
        window.rename_named("neon")
        assert (paths.themes_dir() / "glow").is_dir() and not (paths.themes_dir() / "neon").exists()
        assert config_module.load().theme == "glow" and window.config.theme == "glow"
        assert window.document.directory == paths.themes_dir() / "glow"
        # Deleting the start theme is refused; another is moved to the trash.
        window.delete_named("glow")
        assert (paths.themes_dir() / "glow").is_dir()
        answers.reply = QtWidgets.QMessageBox.StandardButton.Yes
        window.edit_named("broken")
        window.delete_named("broken")
        assert not (paths.themes_dir() / "broken").exists()
        trash = Path(os.environ["XDG_DATA_HOME"]) / "Trash" / "files"
        assert any(p.name.startswith("broken") for p in trash.iterdir())
        # The open theme went: the editor moved to one that is left.
        assert window.document.directory.name in paths.theme_names()
        assert "broken" not in window.theme_list.names()
    finally:
        window.undo.setClean()
        window.close()


def test_image_source_says_what_it_shows(app, tmp_path: Path) -> None:
    (tmp_path / "theme.toml").write_text(
        '[theme]\norientation = "portrait"\n\n[[widget.image]]\n'
        'x = 8\ny = 0\nwidth = 120\nheight = 120\nsource = "now_playing"\n')
    window = EditorWindow(ThemeDocument.open(tmp_path), config.Config())
    try:
        window.show()
        window._set_selection(Ref("image", 1))
        pump(app, 0.2)
        field = window.inspector.findChild(QtWidgets.QComboBox, "opt.source")
        items = [field.itemText(i) for i in range(field.count())]
        assert items == ["none: the file at path", "now_playing"]
        assert field.currentData() == "now_playing"
        tip = field.itemData(1, QtCore.Qt.ItemDataRole.ToolTipRole)
        assert tip == "the cover of what is playing"
    finally:
        window.undo.setClean()
        window.close()



def test_wallpaper_sets_the_background_colour_aside(app, tmp_path: Path) -> None:
    from PIL import Image
    Image.new("RGB", (462, 1920), (20, 20, 20)).save(tmp_path / "wall.png")
    (tmp_path / "theme.toml").write_text(
        '[theme]\norientation = "portrait"\nbackground = ["#000000", "#0000ff"]\n'
        'wallpaper = "wall.png"\n\n[[widget.label]]\nx = 8\ny = 0\nwidth = 160\n'
        'height = 48\nformat = "Hi"\n')
    window = EditorWindow(ThemeDocument.open(tmp_path), config.Config())
    try:
        window.show()
        window._set_selection("theme")
        pump(app, 0.2)
        find = lambda name: window.inspector.findChild(QtWidgets.QWidget, name)  # noqa: E731
        for name in ("theme.background", "theme.background_shape", "theme.background_angle"):
            assert not find(name).isEnabled(), name
        assert "covers the whole panel" in find("theme.background").toolTip()
        browse = window.inspector.findChildren(QtWidgets.QToolButton, "browse")
        assert browse and browse[0].text() == "" and not browse[0].icon().isNull()
        window.edit_theme("wallpaper", None, "x")
        pump(app, 0.3)
        assert find("theme.background").isEnabled()
    finally:
        window.undo.setClean()
        window.close()


def test_image_is_not_offered_text_options(app, tmp_path: Path) -> None:
    """now_playing's `idle` is the title shown when nothing plays: an
    image shows no text, so it is not offered there -- `player` is."""
    from akp02d.editor.inspector import options_for
    image = [o.key for o in options_for("image", {"source": "now_playing"})]
    label = [o.key for o in options_for("label", {"source": "now_playing"})]
    assert "player" in image and "idle" not in image
    assert "player" in label and "idle" in label


def test_apply_is_one_button_and_sets_the_start_theme(app, themes) -> None:
    from akp02d import config as config_module, paths
    window = editor(paths.themes_dir() / "neon", themes.config)
    try:
        bar = window.toolbar
        assert isinstance(window.apply_button, QtWidgets.QPushButton)
        assert window.apply_button.menu() is None
        texts = [w.text() for w in bar.findChildren(QtWidgets.QPushButton)]
        assert "Export PNG" not in texts
        window.apply_button.click()
        pump(app, 0.2)
        assert themes.theme.name == "neon"
        assert config_module.load().theme == "neon"          # comes back after a restart
    finally:
        window.undo.setClean()
        window.close()


def test_save_writes_a_preview(app, themes, monkeypatch) -> None:
    from PIL import Image
    from akp02d import paths
    answers = Answers(monkeypatch)
    window = editor(paths.themes_dir() / "neon", themes.config)
    try:
        answers.names = ["fresh"]
        window.new_theme()
        preview = paths.themes_dir() / "fresh" / "preview.png"
        assert preview.is_file()
        with Image.open(preview) as image:
            assert image.size == (462, 1920)
        before = preview.read_bytes()
        window.add_widget("label")
        pump(app, 0.3)
        assert window.save()
        assert preview.read_bytes() != before                # the new label is in it
    finally:
        window.undo.setClean()
        window.close()


def test_layer_icons_duplicate_and_delete(app, tmp_path: Path) -> None:
    (tmp_path / "theme.toml").write_text(
        '[theme]\norientation = "portrait"\n\n[[widget.label]]\n'
        'x = 8\ny = 0\nwidth = 160\nheight = 48\nformat = "Hi"\n')
    window = EditorWindow(ThemeDocument.open(tmp_path), config.Config())
    try:
        window.show()
        buttons = window.layer_buttons
        window._set_selection("theme")
        pump(app, 0.1)
        assert not buttons["duplicate"].isEnabled() and not buttons["delete"].isEnabled()
        window._set_selection(Ref("label", 1))
        pump(app, 0.1)
        assert buttons["duplicate"].isEnabled() and buttons["delete"].isEnabled()
        buttons["duplicate"].click()
        pump(app, 0.2)
        assert len(window.document.refs()) == 2
        buttons["delete"].click()
        pump(app, 0.2)
        assert len(window.document.refs()) == 1
        assert not buttons["delete"].isEnabled()            # nothing selected now
        window.undo.undo()
        pump(app, 0.2)
        assert len(window.document.refs()) == 2             # Delete is undoable
    finally:
        window.undo.setClean()
        window.close()


def _drop(window, plugin: str, scene_x: float, scene_y: float):
    """A drop of an Add Widget button at a point on the panel, through
    the canvas's own drop handling."""
    from PySide6.QtCore import QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent
    from akp02d.editor.canvas import MIME

    data = QMimeData()
    data.setData(MIME, plugin.encode())
    at = QPointF(window.canvas.mapFromScene(QPointF(scene_x, scene_y)))
    event = QDropEvent(at, Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    window.canvas.dropEvent(event)
    # Where the canvas saw it, after the trip to screen pixels and back.
    return window.canvas.incoming_at(plugin, window.canvas.mapToScene(at.toPoint()))


def test_dropping_an_add_button_places_the_widget_there(app, tmp_path: Path) -> None:
    window = _window(app, tmp_path)
    try:
        canvas = window.canvas
        width, height = canvas.screen
        free = None
        # Find a spot the default label size fits without overlapping.
        for y in range(40, height - 40, 16):
            for x in range(60, width - 60, 16):
                found = canvas.incoming_at("label", __import__("PySide6.QtCore").QtCore.QPointF(x, y))
                if found is not None and not found.overlaps:
                    free = (x, y, found.rect)
                    break
            if free:
                break
        assert free is not None
        before = len(window.engine.slots)
        seen = _drop(window, "label", free[0], free[1])
        pump(app, 0.3)
        assert len(window.engine.slots) == before + 1
        added = window.selection
        entry = window.document.entry(added)
        assert (entry["x"], entry["y"], entry["width"], entry["height"]) == tuple(seen.rect)
        assert abs(seen.rect.y - free[2].y) <= 2       # where it was dropped
        # One undo takes it away again.
        window.undo.undo()
        pump(app, 0.3)
        assert len(window.engine.slots) == before
        # Onto another widget: nothing is added.
        taken = window.engine.slots[0].rect
        _drop(window, "label", taken.x + taken.width / 2, taken.y + taken.height / 2)
        pump(app, 0.3)
        assert len(window.engine.slots) == before
        # Off the panel: nothing either.
        _drop(window, "label", -20, -20)
        pump(app, 0.3)
        assert len(window.engine.slots) == before
    finally:
        window.undo.setClean()
        window.close()
        pump(app)
