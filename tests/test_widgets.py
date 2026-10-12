"""Label effects, the bar widget, and the text sources."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from akp02 import Orientation
from akp02d import sources
from akp02d.layout import Rect
from akp02d.plugins import lookup
from akp02d.plugins.base import PluginError, WidgetSpec
from akp02d.table import Table
from akp02d.theme import Theme

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def theme() -> Theme:
    return Theme(name="t", path=Path("."), orientation=Orientation.PORTRAIT,
                 background=Image.new("RGB", (462, 1920), (10, 10, 10)),
                 font_default=FONT if Path(FONT).exists() else None, widgets=())


def build(plugin: str, options: dict, rect=Rect(0, 0, 300, 80)):
    table = Table(dict(options), f"widget.{plugin}[0]")
    return lookup(plugin)(WidgetSpec(f"{plugin}#1", plugin, rect, None, table), theme())


def pixels(image: Image.Image) -> list:
    width, height = image.size
    return [image.getpixel((x, y)) for y in range(height) for x in range(width)]


class Fixed:
    """A scalar source that reads whatever it is told."""

    kind = sources.SCALAR
    range = (0.0, 1.0)

    def __init__(self, value: float) -> None:
        self.value = value

    def read(self, now):
        return self.value, {"value": self.value, "percent": self.value * 100}


# -- label effects --


def label_pixels(**options) -> Image.Image:
    widget = build("label", {"format": "Hi", "size": 48, **options})
    widget.render(0.0)
    return widget.surface


def test_label_gradient_runs_top_to_bottom() -> None:
    image = label_pixels(gradient=["#ff0000", "#0000ff"], color="#00ff00")
    reds = [image.getpixel((x, y)) for y in range(80) for x in range(300)
            if image.getpixel((x, y)) != (10, 10, 10)]
    assert reds, "nothing drawn"
    top = min(y for y in range(80) for x in range(300) if image.getpixel((x, y)) != (10, 10, 10))
    bottom = max(y for y in range(80) for x in range(300) if image.getpixel((x, y)) != (10, 10, 10))
    row = lambda y: [image.getpixel((x, y)) for x in range(300) if image.getpixel((x, y)) != (10, 10, 10)]
    r_top, _, b_top = row(top + 2)[0]
    r_bot, _, b_bot = row(bottom - 2)[0]
    assert r_top > b_top and b_bot > r_bot
    assert all(g < 40 for _, g, _ in reds)   # `color` is not used with a gradient


def test_label_outline_and_shadow_add_pixels() -> None:
    plain = label_pixels()
    outlined = label_pixels(outline="#ff0000", outline_width=3)
    shadowed = label_pixels(shadow="#00ff00", shadow_x=4, shadow_y=4, shadow_blur=0,
                            shadow_alpha=1.0)
    count = lambda img, colour: sum(1 for p in pixels(img) if p == colour)
    assert count(plain, (255, 0, 0)) == 0 and count(outlined, (255, 0, 0)) > 50
    assert count(shadowed, (0, 255, 0)) > 50
    # The letters themselves are still white on top.
    assert count(outlined, (255, 255, 255)) > 50


def test_glow_is_a_centred_soft_shadow() -> None:
    image = label_pixels(shadow="#00ffff", shadow_x=0, shadow_y=0, shadow_blur=6)
    cyanish = [p for p in pixels(image) if p != (10, 10, 10) and p[1] > p[0] + 20]
    assert len(cyanish) > 100


# -- bar --


def test_bar_redraws_only_when_the_fill_moves() -> None:
    bar = build("bar", {"source": "cpu"})
    bar._source = Fixed(0.5)
    assert bar.render(0.0)
    assert not bar.render(1.0)
    bar._source.value = 0.5001        # same pixel
    assert not bar.render(2.0)
    bar._source.value = 0.9
    assert bar.render(3.0)


def test_bar_segments_move_in_whole_cells() -> None:
    bar = build("bar", {"source": "cpu", "segments": 10})
    bar._source = Fixed(0.52)
    bar.render(0.0)
    bar._source.value = 0.54          # still 5 of 10 cells
    assert not bar.render(1.0)
    bar._source.value = 0.56          # 6 cells
    assert bar.render(2.0)


def test_bar_fills_from_the_start() -> None:
    bar = build("bar", {"source": "cpu", "gradient": ["#ff0000"], "track": "#000080"})
    bar._source = Fixed(0.25)
    bar.render(0.0)
    assert bar.surface.getpixel((10, 40)) == (255, 0, 0)
    assert bar.surface.getpixel((290, 40)) == (0, 0, 128)
    tall = build("bar", {"source": "cpu", "gradient": ["#ff0000"], "track": "#000080"},
                 Rect(0, 0, 40, 200))
    tall._source = Fixed(0.25)
    tall.render(0.0)
    assert tall.surface.getpixel((20, 195)) == (255, 0, 0)    # bottom up
    assert tall.surface.getpixel((20, 5)) == (0, 0, 128)


def test_bar_text_above_takes_room() -> None:
    bar = build("bar", {"source": "cpu", "label": "CPU", "format": "{percent:.0f}%"})
    assert bar._bar[1] > 0
    with pytest.raises(PluginError, match="no room for the bar"):
        build("bar", {"source": "cpu", "label": "CPU", "size": 60}, Rect(0, 0, 300, 40))
    with pytest.raises(PluginError, match="do not fit"):
        build("bar", {"source": "cpu", "segments": 200, "segment_gap": 4})


def test_bar_range_follows_the_source() -> None:
    bar = build("bar", {"source": "cpu", "min": 0.2, "max": 0.6})
    assert bar._range == (0.2, pytest.approx(0.4))


# -- text sources --


def test_uptime_fields() -> None:
    assert sources.uptime_fields(3 * 86400 + 4 * 3600 + 12 * 60 + 5)["value"] == "3d 4h 12m"
    assert sources.uptime_fields(4 * 3600 + 60)["value"] == "4h 1m"
    assert sources.uptime_fields(300)["value"] == "5m"
    assert sources.uptime_fields(90061)["total_hours"] == 25


@pytest.mark.parametrize("name", ["hostname", "uptime", "kernel", "ip"])
def test_text_sources_read(name: str) -> None:
    source = sources.create(name, Table({}), "w")
    assert source.kind == sources.TEXT
    value, fields = source.read(0.0)
    assert value is None and isinstance(fields["value"], str) and fields["value"]
    assert set(source.fields) <= set(fields)


def test_text_sources_on_a_label_not_a_gauge() -> None:
    label = build("label", {"source": "hostname", "format": "{short}"})
    assert label.render(0.0)
    with pytest.raises(sources.SourceError, match="produces a text"):
        build("gauge", {"source": "kernel"}, Rect(0, 0, 160, 160))


def test_ip_unknown_interface() -> None:
    with pytest.raises(sources.SourceError, match="no interface 'zz9'"):
        sources.create("ip", Table({"interface": "zz9"}), "w")


# -- what the machine offers, for the editor's dropdowns --


def test_mount_choices_include_root() -> None:
    choices = dict(sources.mount_choices())
    assert "/" in choices


def test_sensor_choices(tmp_path, monkeypatch) -> None:
    hwmon = tmp_path / "hwmon"
    for index, (name, readings) in enumerate((
        ("nvme", [("Composite", 38500)]),
        ("k10temp", [("Tctl", 61250), ("Tccd1", 55000)]),
    )):
        chip = hwmon / f"hwmon{index}"
        chip.mkdir(parents=True)
        (chip / "name").write_text(name)
        for number, (label, milli) in enumerate(readings, start=1):
            (chip / f"temp{number}_input").write_text(str(milli))
            (chip / f"temp{number}_label").write_text(label)
    monkeypatch.setattr(sources, "HWMON", hwmon)
    monkeypatch.setattr(sources, "THERMAL", tmp_path / "none")
    assert sources.sensor_choices() == [("nvme", "nvme  (38 °C)"),
                                        ("k10temp", "k10temp  (61 °C)")]
    # No sensor named: the CPU's readings.
    assert [v for v, _ in sources.sensor_label_choices(None)] == ["Tctl", "Tccd1"]
    assert [v for v, _ in sources.sensor_label_choices("nvme")] == ["Composite"]
    option = sources.TemperatureSource.schema["sensor_label"]
    from akp02d.schema import Values
    assert [v for v, _ in option.suggestions(Values(sensor="nvme"))] == ["Composite"]
