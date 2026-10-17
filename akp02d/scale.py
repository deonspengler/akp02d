"""The range a reading is drawn against: linear, or log for rates.

Shared by every widget that turns a reading into a position (bar,
gauge, graph), so `scale`, `min` and `max` mean the same everywhere and
default the same way. A source says what suits it: `range` is its usual
low and high, `scale` how it is best drawn, and `floor` what counts as
nothing on a log scale. Network rates are the reason for the last two:
browsing runs at a few KB/s and a download at a hundred MB/s, so on a
linear scale to a gigabit the everyday rates never leave the bottom.
"""

from __future__ import annotations

import math
from typing import Any

from .schema import Auto, Option, choice, number

SCALES = ("linear", "log")


def _source(values) -> Any:
    return values.get("source")


def _usual(values) -> tuple[float, float]:
    return getattr(_source(values), "range", (0.0, 1.0))


def _floor(values) -> float:
    """A log scale's default floor: the source's, or five decades down."""
    floor = getattr(_source(values), "floor", None)
    return floor if floor else max(_usual(values)[1], 1e-9) / 1e5


def options(group: str = "Data", when=None) -> tuple[Option, ...]:
    """`scale`, `min` and `max`, in that order (min's default depends on
    the scale)."""
    extra = {} if when is None else {"when": when}
    return (
        choice("scale", SCALES,
               Auto(lambda v, spec: getattr(_source(v), "scale", "linear"),
                    "log for a rate (network), else linear"),
               group=group, **extra,
               doc="log spreads readings that span orders of magnitude, so "
                   "small ones still show"),
        number("min", Auto(lambda v, spec: _floor(v) if v.get("scale") == "log"
                           else _usual(v)[0],
                           "the source's usual low; on a log scale, 1 KB/s for a rate"),
               group=group, **extra,
               doc="Bottom of the range; on a log scale, what counts as nothing"),
        number("max", Auto(lambda v, spec: _usual(v)[1],
                           "the source's usual high; a gigabit/s for a rate"),
               group=group, **extra, doc="Top of the range"),
    )


class Scale:
    """Where a reading sits between min and max, as a fraction 0..1."""

    __slots__ = ("_span", "high", "log", "low")

    def __init__(self, low: float, high: float, log: bool) -> None:
        self.low, self.high, self.log = low, high, log
        self._span = math.log(high / low) if log else high - low

    @classmethod
    def read(cls, o, options) -> Scale:
        """From read values, failing on a range that cannot be drawn."""
        low, high = o.min, o.max
        if high <= low:
            raise options.fail("max", f"greater than min ({low})", high)
        log = o.scale == "log"
        # For a log scale `min` is the floor -- what counts as nothing --
        # rather than the bottom of a range, so it cannot be zero.
        if log and low <= 0:
            raise options.fail("min", "greater than zero on a log scale", low)
        return cls(low, high, log)

    def fraction(self, value: float | None) -> float:
        if value is None:
            return 0.0
        if self.log:
            if value <= self.low:
                return 0.0
            return min(1.0, math.log(value / self.low) / self._span)
        return min(1.0, max(0.0, (value - self.low) / self._span))

    def marks(self, majors: int, minors: int, rate: bool = False
              ) -> list[tuple[float, bool, float]]:
        """(fraction, is_major, value) for a scale's tick marks.

        Linear: `majors` even divisions. Log: a major mark at each power
        of ten that falls in range -- 1K, 10K, 100K, 1M for a rate, which
        counts in 1024s -- so the numbers are round, whatever `majors`
        says. Either way `minors` marks evenly between two majors.
        """
        if self.log:
            steps = (1, 10, 100) if rate else (1,)
            base = 1024 if rate else 10
            values = sorted(m * base**j for j in range(-6, 7) for m in steps)
            at = [v for v in values if self.low * (1 - 1e-9) <= v <= self.high * (1 + 1e-9)]
        if not self.log or len(at) < 2:
            at = [self.value_at(i / majors) for i in range(majors + 1)]
        out: list[tuple[float, bool, float]] = []
        for index, value in enumerate(at):
            fraction = self.fraction(value)
            out.append((fraction, True, value))
            if index + 1 < len(at):
                step = (self.fraction(at[index + 1]) - fraction) / (minors + 1)
                out += [(fraction + step * k, False, self.value_at(fraction + step * k))
                        for k in range(1, minors + 1)]
        return out

    def value_at(self, fraction: float) -> float:
        """The reading at `fraction` along the scale: for tick numbers."""
        if self.log:
            return self.low * math.exp(self._span * fraction)
        return self.low + self._span * fraction


def compact(value: float) -> str:
    """A rate in bytes/s as a scale number: 1K, 10K, 1.2M, 125M."""
    for unit, size in (("G", 2**30), ("M", 2**20), ("K", 2**10)):
        if value >= size * 0.95:
            scaled = value / size
            return f"{scaled:.0f}{unit}" if scaled >= 9.5 else f"{scaled:.1f}".rstrip("0").rstrip(".") + unit
    return f"{value:.0f}"
