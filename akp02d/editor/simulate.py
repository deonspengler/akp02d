"""Stand-in data, so a theme can be designed on any machine.

The editor's preview runs the real widgets, which read real sources.
That is the point -- what you see is what the daemon draws -- but a
desk machine idling at 2% CPU makes every graph a flat line, and a
spectrum needs cava and music. So in the editor every known source is
answered with plausible moving data, and every spectrum with made-up
music: graphs scroll, gauges move, bars dance, on any machine.

The clock stays real. Sources the editor does not know -- a third-party
plugin's -- are read live; standing in for them would mean guessing at
their fields.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

from .. import sources
from ..plugins import cava

KNOWN = ("time", "cpu", "memory", "disk", "network", "temperature")


def _wave(now: float, *periods: float) -> float:
    """A smooth 0..1 wander built from a few incommensurate sines."""
    total = sum(math.sin(now * 2 * math.pi / p + i * 1.7) for i, p in enumerate(periods))
    return 0.5 + 0.5 * total / len(periods)


def _memory_total() -> int:
    try:
        with open("/proc/meminfo", "rb") as handle:
            for line in handle:
                if line.startswith(b"MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 16 * 2**30


def _disk_total(real: Any) -> int:
    path = getattr(real, "_path", "/")
    try:
        info = os.statvfs(path)
        return info.f_blocks * info.f_frsize
    except OSError:
        return 512 * 2**30


def scalar_fields(name: str, fraction: float, real: Any) -> dict[str, Any]:
    """The fields a real `name` source would offer at `fraction`."""
    fields: dict[str, Any] = {"value": fraction, "percent": fraction * 100}
    if name == "memory":
        total = _memory_total()
        used = int(total * fraction)
        fields.update(used=used, total=total, available=total - used,
                      used_gb=used / 2**30, total_gb=total / 2**30)
    elif name == "disk":
        total = _disk_total(real)
        used = int(total * fraction)
        fields.update(used=used, free=total - used, total=total,
                      used_gb=used / 2**30, free_gb=(total - used) / 2**30,
                      total_gb=total / 2**30)
    return fields


def pair_fields(up: float, down: float) -> dict[str, Any]:
    return {"up": up, "down": down, "up_kb": up / 1024, "down_kb": down / 1024,
            "up_mb": up / 2**20, "down_mb": down / 2**20}


class StandIn:
    """Wraps a real source and answers with simulated readings.

    Readings depend only on the time asked about, so any moment -- past
    ones included -- can be read, which is what lets a graph be filled
    with history the moment it is built.
    """

    __slots__ = ("formats", "kind", "name", "range", "real")

    def __init__(self, real: sources.Source, name: str) -> None:
        self.real = real
        self.name = name
        self.kind = real.kind
        self.range = getattr(real, "range", (0.0, 1.0))
        self.formats = getattr(real, "formats", ())

    def read(self, now: float) -> tuple[Any, dict[str, Any]]:
        name = self.name
        if name not in KNOWN or name == "time":
            return self.real.read(now)
        if name == "network":
            # Mostly idle with bursts, spanning the orders of magnitude a
            # log-scale graph is for.
            down = 10 ** (3.2 + 3.6 * _wave(now, 23, 9.1, 3.3) ** 3)
            up = 10 ** (2.8 + 2.6 * _wave(now + 40, 17, 6.7, 2.9) ** 3)
            return None, pair_fields(up, down)
        if name == "temperature":
            # Idles warm and drifts: a heatsink smooths out what the CPU
            # does second to second, so only slow waves here.
            celsius = 42.0 + 28.0 * _wave(now, 53, 23, 11) ** 2 + 3.0 * _wave(now, 7.1)
            real = self.real
            return celsius, sources.temperature_fields(
                celsius, getattr(real, "_chip", ""), getattr(real, "_label", ""))
        if name == "cpu":
            fraction = 0.08 + 0.8 * _wave(now, 11, 4.3, 1.7) ** 2
        elif name == "memory":
            fraction = 0.45 + 0.25 * _wave(now, 60, 23)
        else:  # disk barely moves
            fraction = 0.70 + 0.02 * _wave(now, 120)
        fraction = min(1.0, max(0.0, fraction))
        return fraction, scalar_fields(name, fraction, self.real)


class Audio:
    """A stand-in for cava's stream: the same frames, made up.

    `read()` has the same contract as cava.Stream.read: the newest frame
    as 16-bit levels, one per bar. Stereo is laid out as cava lays it
    out -- the two channels mirrored with the lows in the middle -- so a
    radial spectrum gets the same symmetric shape it will have on the
    panel.
    """

    __slots__ = ("_bars", "_level", "_stereo", "_start")

    def __init__(self, bars: int, stereo: bool) -> None:
        self._bars = bars
        self._stereo = stereo and bars % 2 == 0
        self._level = [0.0] * bars
        self._start = time.monotonic()

    def read(self) -> tuple[int, ...] | None:
        now = time.monotonic() - self._start
        half = self._bars // 2 if self._stereo else self._bars
        target = self._music(now, half)
        if self._stereo:
            # Right channel slightly different, as real stereo is.
            other = [v * (0.85 + 0.15 * math.sin(now * 3.1 + i)) for i, v in enumerate(target)]
            frame = target[::-1] + other
        else:
            frame = target
        # Fast attack, slower release: what cava's smoothing looks like.
        level = self._level
        for i, value in enumerate(frame):
            level[i] = value if value > level[i] else level[i] * 0.82 + value * 0.18
        return tuple(int(min(1.0, max(0.0, v)) * cava.MAX) for v in level)

    @staticmethod
    def _music(now: float, count: int) -> list[float]:
        beat = max(0.0, 1.0 - (now * 2.1 % 1.0) * 3.0)   # kick, ~126 bpm
        out = []
        for i in range(count):
            f = i / max(count - 1, 1)                     # 0 low .. 1 high
            body = 0.75 * (1.0 - f) ** 1.4 + 0.12
            kick = beat * 0.5 * max(0.0, 1.0 - f * 4)
            jitter = 0.5 + 0.5 * math.sin(now * (7.3 + i * 1.91) + i * 2.3)
            sparkle = 0.25 * f * max(0.0, math.sin(now * 5.0 + i * 0.7)) ** 6
            out.append(min(1.0, body * (0.55 + 0.45 * jitter) + kick + sparkle))
        return out

    def close(self) -> None:
        pass
