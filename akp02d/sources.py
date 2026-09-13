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
from typing import Any

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

    def __init__(self, options: Table, widget: str) -> None:
        pass

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

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        return None, {"value": datetime.now()}


class CpuSource(Source):
    """Busy fraction since this source's previous read.

    A delta, not a reading, so it keeps its own previous snapshot. The
    first read happens at construction, so the first frame already has a
    baseline to measure against rather than reporting zero.
    """

    kind = SCALAR

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

    def __init__(self, options: Table, widget: str) -> None:
        self._path = options.string("path", "/")
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

    def __init__(self, options: Table, widget: str) -> None:
        self._name = options.string("interface", None)
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


_REGISTRY: dict[str, Callable[..., Source]] = {
    "time": TimeSource,
    "cpu": CpuSource,
    "memory": MemorySource,
    "disk": DiskSource,
    "network": NetworkSource,
}


def create(name: str, options: Table, widget: str) -> Source:
    """Instantiate a source by name."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise SourceError(
            f"widget {widget!r}: unknown source {name!r}; available: "
            f"{', '.join(sorted(_REGISTRY))}"
        )
    try:
        return factory(options, widget)
    except OSError as exc:
        raise SourceError(f"widget {widget!r}: source {name!r} is unavailable: {exc}") from exc


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
