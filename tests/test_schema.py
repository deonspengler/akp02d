"""Plugin schemas: complete, and read exactly as the plugins used to."""

from __future__ import annotations

import pytest

from akp02d import sources
from akp02d.layout import Rect
from akp02d.plugins import BASE, available, lookup
from akp02d.plugins.base import WidgetSpec, set_preview
from akp02d.schema import SOURCE, Auto, Option, Schema, choice, integer, string
from akp02d.table import REQUIRED, SettingsError, Table
from akp02d.theme import Theme

from PIL import Image

RESERVED = {"id", "x", "y", "width", "height", "fps"}


class Recording(Table):
    """A Table that remembers every key anyone asked it for."""

    __slots__ = ("asked",)

    def __init__(self, data, where=""):
        super().__init__(data, where)
        self.asked: set[str] = set()

    def raw(self, key, default=None):
        self.asked.add(key)
        return super().raw(key, default)


def make_theme() -> Theme:
    from akp02 import Orientation

    return Theme(name="t", path=__import__("pathlib").Path("."),
                 orientation=Orientation.PORTRAIT,
                 background=Image.new("RGB", (462, 1920)), font_default=None,
                 widgets=())


SAMPLES = {
    "label": [{"source": "cpu", "format": "{percent:.0f}"}, {"format": "x",
               "color": ["#000000", "#ffffff"], "outline": "#000000",
               "shadow": "#000000"},
              {"source": "time", "format": "{value:%H}"}],
    "gauge": [{"source": "memory"}, {"source": "disk"}],
    "graph": [{"source": "cpu", "fill": "#123456"}, {"source": "network"}],
    "bar": [{"source": "cpu", "label": "CPU", "format": "{percent:.0f}%"},
            {"source": "memory", "segments": 10, "text": "inside", "format": "x",
             "direction": "vertical", "radius": 4}],
    "spectrum": [{"type": t, "peaks": True, "origin": "#ff0000", "channels": "mono",
                  "smoothing": 10} for t in ("bars", "mirrored", "segmented", "radial")],
}


@pytest.mark.parametrize("plugin", sorted(SAMPLES))
def test_schema_covers_every_key_read(plugin: str) -> None:
    """A plugin that reads a key its schema does not declare would be
    invisible to the editor; this catches it."""
    cls = lookup(plugin)
    assert cls is not None and len(cls.schema) > 0
    set_preview(True)
    try:
        for sample in SAMPLES[plugin]:
            table = Recording(dict(sample), f"widget.{plugin}[0]")
            spec = WidgetSpec("w", plugin, Rect(0, 0, 200, 120), None, table)
            cls(spec, make_theme()).close()
            declared = set(cls.schema.keys()) | set(BASE.keys())
            source_cls = sources.lookup(sample.get("source", "")) if plugin != "spectrum" else None
            if source_cls is not None:
                declared |= set(source_cls.schema.keys())
            assert table.asked - RESERVED <= declared, table.asked - declared
    finally:
        set_preview(False)


def test_every_registered_plugin_has_a_schema() -> None:
    for name in available():
        cls = lookup(name)
        assert cls is not None
        assert isinstance(cls.schema, Schema)
        assert cls.summary, f"{name} has no summary for the editor"
        for option in cls.schema:
            assert option.key not in RESERVED


def test_auto_defaults() -> None:
    graph = lookup("graph").schema
    spec = WidgetSpec("g", "graph", Rect(0, 0, 300, 100), None, Table({}))
    pair = graph.effective({"source": "network"}, spec, resolve=sources.lookup)
    assert pair.scale == "log" and pair.min == 1024.0 and pair.max == 125_000_000.0
    assert pair.points == 300
    scalar = graph.effective({"source": "cpu"}, spec, resolve=sources.lookup)
    assert scalar.scale == "linear" and scalar.min == 0.0 and scalar.max == 1.0
    assert not graph["line_color_down"].applies(scalar)
    assert graph["line_color_down"].applies(pair)


def test_when_hides_irrelevant_options() -> None:
    spectrum = lookup("spectrum").schema
    bars = spectrum.effective({"type": "bars"})
    radial = spectrum.effective({"type": "radial", "peaks": True})
    assert not spectrum["inner"].applies(bars)
    assert spectrum["inner"].applies(radial)
    assert not spectrum["peak_hold"].applies(bars)
    assert spectrum["peak_hold"].applies(radial)
    assert radial.color_shape == "level" and bars.color_shape == "scale"


def test_reading() -> None:
    schema = Schema(
        string("name", "x"),
        integer("count", 3, 1, 10),
        choice("mode", ("a", "b"), Auto(lambda v, s: "b" if v.count > 5 else "a", "")),
        integer("optional", None, 0, 5),
    )
    values = schema.read(Table({"count": 7}))
    assert (values.name, values.count, values.mode, values.optional) == ("x", 7, "b", None)
    with pytest.raises(SettingsError, match="between 1 and 10"):
        schema.read(Table({"count": 11}, "w"))
    with pytest.raises(SettingsError, match="w.mode must be a or b, got 'c'"):
        schema.read(Table({"mode": "c"}, "w"))
    with pytest.raises(SettingsError, match="between 0 and 5"):
        schema.read(Table({"optional": 9}))


def test_required_source() -> None:
    gauge = lookup("gauge")
    spec = WidgetSpec("gauge#1", "gauge", Rect(0, 0, 10, 10), None, Table({}))
    with pytest.raises(sources.SourceError, match="gauge needs a source"):
        gauge(spec, make_theme())


def test_duplicate_keys_refused() -> None:
    with pytest.raises(ValueError):
        Schema(string("a"), string("a"))


def test_source_option_kind() -> None:
    option = lookup("graph").schema["source"]
    assert option.kind == SOURCE and option.required
    assert isinstance(option, Option) and option.default is REQUIRED


# -- the temperature source --


def _fake_sysfs(root, monkeypatch) -> None:
    """Two hwmon chips and a thermal zone, as the kernel lays them out."""
    hwmon = root / "hwmon"
    for index, (name, readings) in enumerate((
        ("nvme", [("Composite", 38500)]),
        ("k10temp", [("Tctl", 61250), ("Tccd1", 55000)]),
    )):
        chip = hwmon / f"hwmon{index}"
        chip.mkdir(parents=True)
        (chip / "name").write_text(name + "\n")
        for number, (label, milli) in enumerate(readings, start=1):
            (chip / f"temp{number}_input").write_text(f"{milli}\n")
            (chip / f"temp{number}_label").write_text(label + "\n")
    zone = root / "thermal" / "thermal_zone0"
    zone.mkdir(parents=True)
    (zone / "type").write_text("acpitz\n")
    (zone / "temp").write_text("27800\n")
    monkeypatch.setattr(sources, "HWMON", hwmon)
    monkeypatch.setattr(sources, "THERMAL", root / "thermal")


def test_temperature_picks_the_cpu(tmp_path, monkeypatch) -> None:
    _fake_sysfs(tmp_path, monkeypatch)
    source = sources.create("temperature", Table({}), "w")
    value, fields = source.read(0.0)
    assert value == 61.25 and fields["celsius"] == 61.25
    assert fields["sensor"] == "k10temp" and fields["sensor_label"] == "Tctl"
    assert round(fields["fahrenheit"], 2) == 142.25
    assert "{celsius:.0f}°C".format(**fields) == "61°C"


def test_temperature_named_sensor_and_label(tmp_path, monkeypatch) -> None:
    _fake_sysfs(tmp_path, monkeypatch)
    nvme = sources.create("temperature", Table({"sensor": "nvme"}), "w")
    assert nvme.read(0.0)[0] == 38.5
    ccd = sources.create("temperature", Table({"sensor": "k10temp",
                                               "sensor_label": "tccd1"}), "w")
    assert ccd.read(0.0)[0] == 55.0
    zone = sources.create("temperature", Table({"sensor": "acpitz"}), "w")
    assert zone.read(0.0)[0] == 27.8


def test_temperature_missing_sensor(tmp_path, monkeypatch) -> None:
    _fake_sysfs(tmp_path, monkeypatch)
    with pytest.raises(sources.SourceError,
                       match="no temperature sensor amdgpu; found: nvme/Composite, "
                             "k10temp/Tctl, k10temp/Tccd1, acpitz/thermal_zone0"):
        sources.create("temperature", Table({"sensor": "amdgpu"}), "gauge#1")


def test_temperature_on_a_gauge(tmp_path, monkeypatch) -> None:
    _fake_sysfs(tmp_path, monkeypatch)
    gauge = lookup("gauge")
    table = Table({"source": "temperature", "min": 20, "max": 100,
                   "format": "{celsius:.0f}°C"}, "widget.gauge[0]")
    spec = WidgetSpec("gauge#1", "gauge", Rect(0, 0, 160, 160), None, table)
    widget = gauge(spec, make_theme())
    assert widget.render(0.0)
    widget.close()
