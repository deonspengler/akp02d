"""Network as an ordinary source, the graph's styles, and log scales on
bars and gauges."""

from __future__ import annotations

import pytest

from akp02d import sources
from akp02d.layout import Rect
from akp02d.plugins import lookup
from akp02d.plugins.base import PluginError
from akp02d.scale import Scale, compact
from akp02d.table import SettingsError

from test_widgets import build

UP, DOWN = "#ff0000", "#0000ff"


class Rates:
    """Network-like readings, fixed: up and down in bytes per second."""

    kind = sources.SCALAR
    range = sources.NetworkSource.range
    scale = sources.NetworkSource.scale
    floor = sources.NetworkSource.floor
    pair = sources.NetworkSource.pair

    def __init__(self, up: float, down: float, traffic: str = "total") -> None:
        self.up, self.down, self.traffic = up, down, traffic

    def read(self, now):
        return sources.network_fields(self.up, self.down, self.traffic)


# -- the source --


def test_network_fields_follow_traffic() -> None:
    value, fields = sources.network_fields(100.0, 300.0, "total")
    assert value == fields["total"] == 400.0
    assert sources.network_fields(100.0, 300.0, "down")[0] == 300.0
    assert sources.network_fields(100.0, 300.0, "up")[0] == 100.0
    assert fields["down_kb"] == 300.0 / 1024 and fields["total_mb"] == 400.0 / 2**20


def test_network_is_a_number_for_bars_and_gauges() -> None:
    assert sources.NetworkSource.kind == sources.SCALAR
    for plugin in ("bar", "gauge"):
        accepts = lookup(plugin).schema["source"].accepts
        assert sources.NetworkSource.kind in accepts
    bar = build("bar", {"source": "network", "traffic": "down"})
    assert bar._source.traffic == "down"
    assert build("gauge", {"source": "network"}, Rect(0, 0, 160, 160))._source.traffic == "total"


def test_traffic_is_checked() -> None:
    with pytest.raises(SettingsError, match="traffic"):
        build("bar", {"source": "network", "traffic": "sideways"})


# -- scales --


def test_rates_default_to_log_from_a_kilobyte_to_a_gigabit() -> None:
    for plugin, rect in (("bar", Rect(0, 0, 300, 80)), ("gauge", Rect(0, 0, 160, 160)),
                         ("graph", Rect(0, 0, 120, 100))):
        scale = build(plugin, {"source": "network"}, rect)._scale
        assert (scale.low, scale.high, scale.log) == (1024.0, 125_000_000.0, True)
    cpu = build("bar", {"source": "cpu"})._scale
    assert (cpu.low, cpu.high, cpu.log) == (0.0, 1.0, False)


def test_a_rate_can_still_be_linear() -> None:
    scale = build("bar", {"source": "network", "scale": "linear"})._scale
    assert (scale.low, scale.log) == (0.0, False)


def test_log_scale_spreads_everyday_rates() -> None:
    scale = Scale(1024.0, 125_000_000.0, True)
    assert scale.fraction(500.0) == 0.0                     # below the floor: idle
    assert scale.fraction(10 * 1024) == pytest.approx(0.20, abs=0.01)
    assert scale.fraction(2**20) == pytest.approx(0.59, abs=0.01)
    assert scale.fraction(1e12) == 1.0
    for fraction in (0.0, 0.3, 1.0):
        assert scale.fraction(scale.value_at(fraction)) == pytest.approx(fraction)


def test_log_needs_a_floor_above_zero() -> None:
    with pytest.raises(SettingsError, match="greater than zero"):
        build("bar", {"source": "network", "min": 0})


def test_a_log_bar_fills_by_magnitude() -> None:
    bar = build("bar", {"source": "network", "fill": "#ff0000", "track": "#000000"})
    bar._source = Rates(0.0, 2**20)                         # 1 MB/s down
    bar.render(0.0)
    lit = sum(1 for x in range(300) if bar.surface.getpixel((x, 40))[0] > 128)
    assert 0.5 * 300 < lit < 0.7 * 300


def test_gauge_numbers_rates_compactly() -> None:
    assert compact(1024) == "1K" and compact(10_700) == "10K" and compact(1_150_000) == "1.1M"
    gauge = build("gauge", {"source": "network", "style": "dial"}, Rect(0, 0, 160, 160))
    assert gauge._scale.log
    # A zone is placed by the scale: 1 MB/s sits well past halfway round.
    angle = gauge._angle(2**20)
    assert (angle - gauge._start) / gauge._sweep == pytest.approx(0.59, abs=0.01)


# -- the graph's styles --


def graph(options: dict, source):
    widget = build("graph", {"line_width": 3, "line_color": UP,
                             "line_color_down": DOWN, **options}, Rect(0, 0, 120, 100))
    widget._source = source
    for step in range(130):
        widget.render(float(step))
    return widget


def rows_of(surface, colour) -> list[int]:
    want = (255, 0, 0) if colour == UP else (0, 0, 255)
    return [y for y in range(100) for x in (60,) if surface.getpixel((x, y)) == want]


def test_style_defaults_to_mirrored_for_network_and_line_otherwise() -> None:
    schema = lookup("graph").schema
    assert schema.effective({"source": sources.NetworkSource}).style == "mirrored"
    assert schema.effective({"source": sources.CpuSource}).style == "line"


def test_two_traces_need_two_directions() -> None:
    for style in ("mirrored", "overlaid"):
        with pytest.raises(SettingsError, match="two directions"):
            build("graph", {"source": "cpu", "style": style})


def test_mirrored_puts_up_above_and_down_below() -> None:
    surface = graph({"source": "network", "style": "mirrored"}, Rates(2**20, 2**20)).surface
    assert max(rows_of(surface, UP)) < 50 < min(rows_of(surface, DOWN))


def test_overlaid_draws_both_on_one_axis() -> None:
    # Down busier than up: both rise from the bottom, down the higher.
    surface = graph({"source": "network", "style": "overlaid"},
                    Rates(4 * 1024, 8 * 2**20)).surface
    up, down = rows_of(surface, UP), rows_of(surface, DOWN)
    assert up and down
    assert min(up) > 50 and max(down) < min(up)


def test_line_on_network_follows_traffic() -> None:
    quiet_up = graph({"source": "network", "style": "line"}, Rates(0.0, 8 * 2**20, "up"))
    busy_down = graph({"source": "network", "style": "line"}, Rates(0.0, 8 * 2**20, "down"))
    assert quiet_up._history[-1] == 0.0 and busy_down._history[-1] == 8 * 2**20
    assert quiet_up._second is None


def test_line_color_down_shows_only_for_two_traces() -> None:
    schema = lookup("graph").schema
    option = schema["line_color_down"]
    assert option.applies({"style": "overlaid"}) and option.applies({"style": "mirrored"})
    assert not option.applies({"style": "line"})


def test_unknown_style_is_refused() -> None:
    with pytest.raises((SettingsError, PluginError)):
        build("graph", {"source": "network", "style": "stacked"})


def test_log_ticks_fall_on_round_numbers() -> None:
    scale = Scale(1024.0, 125_000_000.0, True)
    majors = [value for _, major, value in scale.marks(5, 4, rate=True) if major]
    assert [compact(v) for v in majors] == ["1K", "10K", "100K", "1M", "10M", "100M"]
    minors = [f for f, major, _ in scale.marks(5, 4, rate=True) if not major]
    assert len(minors) == 5 * 4 and all(0 < f < 1 for f in minors)
    linear = Scale(0.0, 1.0, False).marks(5, 1)
    assert [round(f, 2) for f, major, _ in linear if major] == [0, 0.2, 0.4, 0.6, 0.8, 1.0]
