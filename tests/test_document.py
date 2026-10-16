"""ThemeDocument edits the text in place and leaves the rest alone."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from akp02d.editor.document import DocumentError, Ref, ThemeDocument, toml_value

TEXT = """\
# My theme -- a comment at the top
[theme]
orientation = "portrait"   # tall
wallpaper = "panel.jpg"

[[widget.label]]
x = 8
y = 0
width = 170
height = 50
format = "hello"

[[widget.graph]]
x = 72
y = 400
width = 318
height = 160
source = "network"
max = 12500000      # 100 Mbit
line_color = [
    "#11e3f5",   # up
    "#66f0ff",
]
cava = { general = { autosens = 1 } }

# The clock
[[widget.label]]
x = 80
y = 90
width = 302
height = 46
"quoted key" = 1
"""


@pytest.fixture
def doc() -> ThemeDocument:
    return ThemeDocument(TEXT)


def lines_changed(before: str, after: str) -> list[str]:
    import difflib
    return [line for line in difflib.ndiff(before.splitlines(), after.splitlines())
            if line[:1] in "+-"]


def test_order(doc: ThemeDocument) -> None:
    assert [r.default_id for r in doc.file_order()] == ["label#1", "graph#1", "label#2"]
    assert [r.default_id for r in doc.refs()] == ["label#1", "label#2", "graph#1"]


def test_set_existing_keeps_comment(doc: ThemeDocument) -> None:
    doc.set(Ref("graph", 1), "max", 25000000)
    assert lines_changed(TEXT, doc.text) == [
        "- max = 12500000      # 100 Mbit", "+ max = 25000000      # 100 Mbit"]


def test_set_new_key_goes_after_last(doc: ThemeDocument) -> None:
    doc.set(Ref("label", 2), "size", 44)
    lines = doc.text.splitlines()
    assert lines[lines.index('"quoted key" = 1') + 1] == "size = 44"
    assert doc.entry(Ref("label", 2))["size"] == 44


def test_multiline_value_replaced_whole(doc: ThemeDocument) -> None:
    doc.set(Ref("graph", 1), "line_color", ["#000000"])
    assert 'line_color = ["#000000"]' in doc.text
    assert "# up" not in doc.text
    assert doc.entry(Ref("graph", 1))["cava"] == {"general": {"autosens": 1}}


def test_remove_key(doc: ThemeDocument) -> None:
    doc.set(Ref("graph", 1), "line_color", None)
    assert "line_color" not in doc.entry(Ref("graph", 1))
    assert '"#66f0ff"' not in doc.text
    doc.set(Ref("label", 2), "quoted key", None)
    assert "quoted key" not in doc.text


def test_theme_table(doc: ThemeDocument) -> None:
    doc.set_theme("orientation", "landscape")
    assert 'orientation = "landscape"   # tall' in doc.text
    doc.set_theme("font", "x.ttf")
    assert doc.theme["font"] == "x.ttf"
    bare = ThemeDocument('[[widget.label]]\nx = 0\ny = 0\nwidth = 1\nheight = 1\n')
    bare.set_theme("orientation", "portrait")
    assert bare.text.startswith('[theme]\norientation = "portrait"\n\n[[widget.label]]')


def test_add_and_remove_widget(doc: ThemeDocument) -> None:
    ref = doc.add_widget("gauge", {"x": 1, "y": 2, "width": 3, "height": 4, "source": "cpu"})
    assert ref == Ref("gauge", 1)
    assert doc.text.endswith('[[widget.gauge]]\nx = 1\ny = 2\nwidth = 3\nheight = 4\n'
                             'source = "cpu"\n')
    doc.remove_widget(ref)
    assert doc.text == TEXT
    doc.remove_widget(Ref("graph", 1))
    data = tomllib.loads(doc.text)
    assert "graph" not in data["widget"]
    # The comment above the next block stays with it.
    assert "# The clock\n[[widget.label]]" in doc.text


def test_remove_widget_with_comment_above(doc: ThemeDocument) -> None:
    doc.remove_widget(Ref("label", 2))
    assert "# The clock" not in doc.text
    assert not doc.text.endswith("\n\n")


def test_duplicate(doc: ThemeDocument) -> None:
    new = doc.duplicate_widget(Ref("graph", 1), {"y": 600})
    entry = doc.entry(new)
    assert new == Ref("graph", 2)
    assert entry["y"] == 600 and entry["max"] == 12500000
    assert entry["cava"] == {"general": {"autosens": 1}}


def test_untouched_is_byte_identical(doc: ThemeDocument) -> None:
    doc.set(Ref("label", 1), "x", 8)   # same value
    assert doc.text == TEXT


def test_invalid_refused(doc: ThemeDocument) -> None:
    with pytest.raises(DocumentError):
        doc.set(Ref("label", 9), "x", 1)
    with pytest.raises(DocumentError):
        ThemeDocument("this is = = not toml")


def test_toml_value() -> None:
    assert toml_value(0.1 + 0.2) == "0.3"
    assert toml_value(1e-7) == "1e-07"
    assert toml_value(2.0) == "2.0"
    assert toml_value('a "b"\n') == '"a \\"b\\"\\n"'
    assert toml_value({"a b": [1, True]}) == '{ "a b" = [1, true] }'
    for value in (0.1 + 0.2, 1e-7, 'q"\\', {"x": {"y": 1}}, [1.5, "a"]):
        assert tomllib.loads(f"v = {toml_value(value)}")["v"] == pytest.approx(value) \
            if isinstance(value, float) else tomllib.loads(f"v = {toml_value(value)}")["v"] == value


def test_save_roundtrip(tmp_path: Path, doc: ThemeDocument) -> None:
    target = doc.save(tmp_path / "t")
    assert target == tmp_path / "t" / "theme.toml"
    assert ThemeDocument.open(tmp_path / "t").text == TEXT


def test_default_theme_editable() -> None:
    import akp02d

    path = Path(akp02d.__file__).parent / "themes" / "default"
    doc = ThemeDocument.open(path)
    original = doc.text
    for ref in doc.refs():
        entry = doc.entry(ref)
        doc.update(ref, {"x": entry["x"], "y": entry["y"]})
    assert doc.text == original
