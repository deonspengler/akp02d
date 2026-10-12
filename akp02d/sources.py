"""Sources: where a widget's number or text comes from.

A source is owned by the widget that names it and read on that widget's
own schedule, so `fps` keeps meaning what it says -- how often this
widget updates. Two widgets on the same source read it twice, which
costs about 18us for cpu and 35us for memory: not worth infrastructure
to avoid, and it keeps a widget's rate a property of the widget.

That reasoning is specific to cheap local reads. A source that costs an
API call or holds a connection will need sharing, and can grow it then.

Each read produces a `value` in 0..1 for shapes to draw, and `fields`
for text to format. Sources that are not numbers (time) leave value at
None and put the useful thing in fields.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import Schema, string
from .table import SettingsError, Table

# What a source produces, and therefore which shapes can draw it.
SCALAR, TEXT, MOMENT, PAIR = "scalar", "text", "moment", "pair"


class SourceError(SettingsError):
    """A source is unknown, misconfigured, or unavailable."""


class Source:
    """Base class. Subclasses implement read().

    Constructed with the widget's own options table, so a source that
    needs configuring can read its own keys from it -- they sit beside
    the widget's, and reject_unknown still catches a typo in either.
    """

    kind = SCALAR
    # Options of the source's own, read from the widget's table. Declared
    # so an editor can show them beside the widget's.
    schema = Schema()
    # The names a format string can use, for editors and error messages.
    fields: tuple[str, ...] = ()
    # One line on what it reads, for editors.
    summary = ""
    # The range a widget draws `value` against unless the theme sets its
    # own min and max. Fractions by default; a temperature is degrees.
    range: tuple[float, float] = (0.0, 1.0)

    def __init__(self, options: Table, widget: str) -> None:
        self.schema.read(options)

    def read(self, now: float) -> tuple[float | None, dict[str, Any]]:
        """Return (value in 0..1 or None, fields for formatting).

        A pair source leaves value None and puts `up` and `down` in
        fields: the two are read together, from one sample, because
        reading them separately would straddle a delta boundary and let
        them disagree about the same instant.
        """
        raise NotImplementedError


class TimeSource(Source):
    """The current local time. Formatted, not drawn."""

    kind = MOMENT
    fields = ("value",)
    summary = "The local time; format it with strftime codes, e.g. {value:%H:%M}"

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        return None, {"value": datetime.now()}


class CpuSource(Source):
    """Busy fraction since this source's previous read.

    A delta, not a reading, so it keeps its own previous snapshot. The
    first read happens at construction, so the first frame already has a
    baseline to measure against rather than reporting zero.
    """

    kind = SCALAR
    fields = ("value", "percent")
    summary = "Busy fraction of all CPUs since the last read"

    def __init__(self, options: Table, widget: str) -> None:
        self._previous = _cpu_times()

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        idle, total = _cpu_times()
        idle_delta = idle - self._previous[0]
        total_delta = total - self._previous[1]
        self._previous = (idle, total)
        busy = 1.0 - idle_delta / total_delta if total_delta > 0 else 0.0
        busy = min(1.0, max(0.0, busy))
        return busy, {"value": busy, "percent": busy * 100}


class MemorySource(Source):
    """Used fraction of physical memory.

    Used is total minus MemAvailable, which is the kernel's own estimate
    of what a new process could claim -- closer to what a person means
    by "used" than total minus free, which counts cache as used.
    """

    kind = SCALAR
    fields = ("value", "percent", "used", "total", "available", "used_gb",
              "total_gb")
    summary = "Used fraction of physical memory"

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        info = _meminfo()
        total = info["MemTotal"]
        available = info.get("MemAvailable", info["MemFree"])
        used = total - available
        fraction = used / total if total else 0.0
        return fraction, {
            "value": fraction,
            "percent": fraction * 100,
            "used": used,
            "total": total,
            "available": available,
            "used_gb": used / 2**30,
            "total_gb": total / 2**30,
        }


class DiskSource(Source):
    """Used fraction of a filesystem.

    Reads `path`, which is any location on the filesystem of interest --
    a mount point, or anything under it.
    """

    kind = SCALAR
    schema = Schema(
        string("path", "/", group="Data",
               doc="Any path on the filesystem to measure: a mount point or "
                   "anything under it"),
    )
    fields = ("value", "percent", "used", "free", "total", "used_gb",
              "free_gb", "total_gb")
    summary = "Used fraction of a filesystem"

    def __init__(self, options: Table, widget: str) -> None:
        self._path = self.schema.read(options).path
        try:
            os.statvfs(self._path)
        except OSError as exc:
            raise SourceError(
                f"widget {widget!r}: cannot read {self._path!r}: {exc}"
            ) from exc

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        info = os.statvfs(self._path)
        total = info.f_blocks * info.f_frsize
        free = info.f_bavail * info.f_frsize
        used = total - free
        fraction = used / total if total else 0.0
        return fraction, {
            "value": fraction,
            "percent": fraction * 100,
            "used": used,
            "free": free,
            "total": total,
            "used_gb": used / 2**30,
            "free_gb": free / 2**30,
            "total_gb": total / 2**30,
        }


class NetworkSource(Source):
    """Bytes per second in each direction since this source's last read.

    Reads `interface`; the default sums every interface except loopback,
    which is what "how busy is my network" usually means. Rates are
    unbounded, so a graph showing this wants a log scale.
    """

    kind = PAIR
    schema = Schema(
        string("interface", None, group="Data",
               doc="One interface, e.g. eno1; leave empty to sum every "
                   "interface except loopback"),
    )
    fields = ("up", "down", "up_kb", "down_kb", "up_mb", "down_mb")
    summary = "Bytes per second sent (up) and received (down)"

    def __init__(self, options: Table, widget: str) -> None:
        self._name = self.schema.read(options).interface
        self._previous = _net_bytes(self._name)
        self._when: float | None = None
        if self._name is not None and self._previous == (0, 0):
            known = ", ".join(sorted(_net_interfaces())) or "none"
            raise SourceError(
                f"widget {widget!r}: no interface {self._name!r}; found: {known}"
            )

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        received, sent = _net_bytes(self._name)
        elapsed = 0.0 if self._when is None else now - self._when
        self._when = now
        if elapsed <= 0:
            down = up = 0.0
        else:
            down = max(0.0, (received - self._previous[0]) / elapsed)
            up = max(0.0, (sent - self._previous[1]) / elapsed)
        self._previous = (received, sent)
        return None, {
            "up": up,
            "down": down,
            "up_kb": up / 1024,
            "down_kb": down / 1024,
            "up_mb": up / 2**20,
            "down_mb": down / 2**20,
        }


class TemperatureSource(Source):
    """A temperature sensor, in degrees Celsius.

    Read from what the kernel already exposes: hwmon chips first (CPU
    packages, GPUs, NVMe drives), then thermal zones, so no extra
    program is needed. With no `sensor` named it picks the CPU -- the
    first of the usual CPU drivers present -- which is what "the
    temperature" usually means.

    `value` is in degrees, not 0..1: what counts as hot is a display
    decision, so a gauge or graph sets its own min and max (20 and 100,
    say) the way a graph does for network rates.
    """

    kind = SCALAR
    schema = Schema(
        string("sensor", None, group="Data",
               doc="Sensor chip, e.g. k10temp, coretemp, amdgpu, nvme; "
                   "empty picks the CPU"),
        string("sensor_label", None, group="Data",
               doc="One reading of that chip, e.g. Tctl or 'Package id 0'; "
                   "empty takes its main one"),
    )
    fields = ("value", "celsius", "fahrenheit", "sensor", "sensor_label")
    range = (20.0, 100.0)
    summary = "A temperature sensor in degrees C (CPU unless told otherwise)"

    def __init__(self, options: Table, widget: str) -> None:
        o = self.schema.read(options)
        found = _temperature_file(o.sensor, o.sensor_label)
        self.unavailable: str | None = None
        if found is None:
            if o.sensor is None and o.sensor_label is None:
                wanted = "no CPU temperature sensor"
            else:
                wanted = "no temperature sensor " + " ".join(
                    part for part in (o.sensor, repr(o.sensor_label) if o.sensor_label else None)
                    if part)
            known = ", ".join(
                f"{chip}/{label}" for chip, label, _ in _temperature_sensors()
            ) or "none"
            message = f"widget {widget!r}: {wanted}; found: {known}"
            if wrap is None:
                raise SourceError(message)
            # A tool standing in for readings (an editor on a machine
            # without this sensor) can still draw it; it says why.
            self.unavailable = message
            self._file, self._chip, self._label = None, o.sensor or "", o.sensor_label or ""
            return
        self._chip, self._label, self._file = found
        self.read(0.0)  # fail now, not on the first frame, if unreadable

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        if self._file is None:
            celsius = 0.0
        else:
            with open(self._file, "rb") as handle:
                celsius = int(handle.read().strip()) / 1000
        return celsius, temperature_fields(celsius, self._chip, self._label)


def temperature_fields(celsius: float, chip: str, label: str) -> dict[str, Any]:
    return {
        "value": celsius,
        "celsius": celsius,
        "fahrenheit": celsius * 9 / 5 + 32,
        "sensor": chip,
        "sensor_label": label,
    }


# Where the kernel publishes sensors; module-level so tests can point
# them at a fake tree.
HWMON = Path("/sys/class/hwmon")
THERMAL = Path("/sys/class/thermal")

# The usual CPU drivers, in the order to prefer them, and the reading of
# each that means "the package" rather than one core.
_CPU_CHIPS = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "x86_pkg_temp",
              "acpitz")
_MAIN_LABELS = ("Tctl", "Tdie", "Package id 0", "edge", "Composite")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _temperature_sensors() -> list[tuple[str, str, Path]]:
    """(chip, label, file) for every temperature reading present."""
    out: list[tuple[str, str, Path]] = []
    try:
        chips = sorted(HWMON.glob("hwmon*"), key=lambda p: (len(p.name), p.name))
    except OSError:
        chips = []
    for chip in chips:
        name = _read_text(chip / "name") or chip.name
        for reading in sorted(chip.glob("temp*_input"),
                              key=lambda p: (len(p.name), p.name)):
            label = _read_text(reading.with_name(reading.name.replace("_input", "_label")))
            out.append((name, label or reading.name.removesuffix("_input"), reading))
    try:
        zones = sorted(THERMAL.glob("thermal_zone*"), key=lambda p: (len(p.name), p.name))
    except OSError:
        zones = []
    for zone in zones:
        if (zone / "temp").exists():
            out.append((_read_text(zone / "type") or zone.name, zone.name, zone / "temp"))
    return out


def _temperature_file(sensor: str | None, label: str | None) -> tuple[str, str, Path] | None:
    """The reading a theme asked for, or the CPU's if it named none."""
    found = _temperature_sensors()
    if sensor is not None:
        found = [f for f in found if f[0].lower() == sensor.lower()]
    else:
        ranked = sorted(
            (f for f in found if f[0] in _CPU_CHIPS),
            key=lambda f: _CPU_CHIPS.index(f[0]),
        )
        found = ranked or found
    if label is not None:
        found = [f for f in found if f[1].lower() == label.lower()]
    if not found:
        return None
    main = [f for f in found if f[1] in _MAIN_LABELS]
    return (main or found)[0]


def _net_interfaces() -> set[str]:
    with open("/proc/net/dev", "rb") as handle:
        lines = handle.read().decode().splitlines()[2:]
    return {line.split(":", 1)[0].strip() for line in lines if ":" in line}


def _net_bytes(name: str | None) -> tuple[int, int]:
    """(received, sent) totals, for one interface or all but loopback."""
    received = sent = 0
    with open("/proc/net/dev", "rb") as handle:
        for line in handle.read().decode().splitlines()[2:]:
            face, _, rest = line.partition(":")
            face = face.strip()
            if name is None:
                if face == "lo":
                    continue
            elif face != name:
                continue
            values = rest.split()
            received += int(values[0])
            sent += int(values[8])
    return received, sent


def _cpu_times() -> tuple[int, int]:
    with open("/proc/stat", "rb") as handle:
        parts = handle.readline().split()[1:]
    values = [int(part) for part in parts]
    # user nice system idle iowait irq softirq steal ...
    return values[3] + values[4], sum(values)


def _meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    with open("/proc/meminfo", "rb") as handle:
        for line in handle:
            name, _, rest = line.partition(b":")
            info[name.decode()] = int(rest.split()[0]) * 1024
    return info


_REGISTRY: dict[str, type[Source]] = {
    "time": TimeSource,
    "cpu": CpuSource,
    "memory": MemorySource,
    "disk": DiskSource,
    "network": NetworkSource,
    "temperature": TemperatureSource,
}


# A seam for tools, unset in the daemon. When set, every source create()
# makes is passed through it, so an editor can stand a simulated or
# frozen reading in front of the real one without the widget knowing.
wrap: Callable[[Source, str], Source] | None = None


def create(name: str, options: Table, widget: str) -> Source:
    """Instantiate a source by name."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise SourceError(
            f"widget {widget!r}: unknown source {name!r}; available: "
            f"{', '.join(sorted(_REGISTRY))}"
        )
    try:
        made = factory(options, widget)
    except OSError as exc:
        raise SourceError(f"widget {widget!r}: source {name!r} is unavailable: {exc}") from exc
    return wrap(made, name) if wrap is not None else made


def resolve(options: Table, widget: str, accepts: tuple[str, ...]) -> Source | None:
    """Read the `source` option and check the shape can draw it.

    A shape that plots numbers cannot show a clock, and saying so at
    load is better than formatting None into the picture.
    """
    name = options.string("source", None)
    if name is None:
        return None
    source = create(name, options, widget)
    if source.kind not in accepts:
        raise SourceError(
            f"widget {widget!r}: source {name!r} produces a {source.kind}, "
            f"but this widget needs {' or a '.join(accepts)}"
        )
    return source


def available() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def lookup(name: str) -> type[Source] | None:
    """The class registered as `name`, without constructing it.

    For tools that need to know what a source is -- its kind, fields and
    options -- without reading /proc or checking a path exists.
    """
    return _REGISTRY.get(name)
