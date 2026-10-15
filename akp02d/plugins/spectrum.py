"""spectrum: an audio visualiser, currently driven by the cava process.

    [[widget.spectrum]]
    x = 40
    y = 200
    width = 600
    height = 200
    fps = 30
    type = "bars"                                 # bars|mirrored|segmented|radial
    bars = 32
    channels = "stereo"                           # stereo | both | left | right
    smoothing = 30                                # 0 twitchy, 100 sluggish
    input = "pulse"                               # cava input method
    source = "auto"                               # what that method captures
    gradient = ["#00ff00", "#ffff00", "#ff0000"]
    origin = "#003c46"                            # omit for none
    peaks = true

What makes it affordable at 30 FPS: the pipe is non-blocking so no
thread is needed, an empty one costs an EAGAIN and an early return; the
change test compares levels in whole units, so silence and sub-unit
movement are free; and colour is resolved once at load, either as a
gradient image pasted through a mask or as a 256-entry lookup.

The settings above are named by akp02d rather than passed through, so a
cava release that renames a key costs one line here instead of an edit
to every theme -- `smoothing` has already been `integral` and is now
`noise_reduction`. Anything not named is still reachable:

    cava = { general = { autosens = 1 }, eq = { 1 = 2, 2 = 1.5 } }

goes into cava's config verbatim. Setting the same key both ways is an
error rather than a silent winner. The frame's shape -- bars, framerate,
and the raw 16-bit output block -- is not the theme's to change, because
the drain expects a fixed frame size.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from .. import gradient
from ..schema import (
    Auto, Schema, boolean, choice, color, colors, integer, number, sections,
    string,
)
from . import cava
from .base import PluginError, Widget, WidgetSpec, dry_run, preview, register

if TYPE_CHECKING:
    from ..theme import Theme

log = logging.getLogger(__name__)

TYPES = ("bars", "mirrored", "segmented", "radial")
# Which gradient a type reads position from, when color_by = "position".
# Where a pixel sits along the ramp, per type. Bars run bottom-up, so a
# tall bar reaches the last stop; mirrored measures from its centre line.
POSITION_MAP = {
    "bars": "up",
    "segmented": "up",
    "mirrored": "centre",
    "radial": "radial",
}


# Silence: every bar below this (of cava.MAX) counts as nothing heard.
SILENCE = cava.MAX // 200
# How long a silence lasts before the sweep starts, and how the sweep moves.
SILENT_SEC = 5.0
SWEEP_PERIOD_SEC = 1 / 0.35   # one pass across the bars, one way
SWEEP_WIDTH = 2.5             # bars either side of the peak that light
SWEEP_FADE_SEC = 1.0
SWEEP_RELEASE = 0.82          # per 1/30 s: the trail a falling bar leaves


class Sweep:
    """A peak running across the bars and back, as cava levels.

    A full-height peak a few bars wide, with cava's own feel: a bar
    jumps up as the peak arrives and falls back slowly, so the peak
    drags a fading tail behind it. In stereo it runs out from the middle
    to both edges and back in, mirrored, as cava lays the two channels out -- the
    right channel a little different, as real stereo is. It fades in
    over its first second, so a silence does not snap into motion.
    """

    __slots__ = ("_bars", "_stereo", "_level", "_time")

    def __init__(self, bars: int, stereo: bool) -> None:
        self._bars = bars
        self._stereo = stereo and bars % 2 == 0
        self.reset()

    def reset(self) -> None:
        self._level = [0.0] * self._bars
        self._time: float | None = None

    def frame(self, now: float, since: float) -> tuple[int, ...]:
        half = self._bars // 2 if self._stereo else self._bars
        # There and back: a triangle wave, so the peak turns at each end
        # instead of jumping back to the start.
        phase = now / SWEEP_PERIOD_SEC % 2.0
        centre = (phase if phase < 1.0 else 2.0 - phase) * max(half - 1, 0)
        strength = min(1.0, max(0.0, since) / SWEEP_FADE_SEC)
        target = [strength * max(0.0, 1.0 - abs(i - centre) / SWEEP_WIDTH)
                  for i in range(half)]
        if self._stereo:
            other = [v * (0.85 + 0.15 * math.sin(now * 3.1 + i))
                     for i, v in enumerate(target)]
            target = target[::-1] + other
        # Fast attack, slow release -- the same at any frame rate.
        dt = 1 / 30 if self._time is None else max(0.0, now - self._time)
        self._time = now
        keep = SWEEP_RELEASE ** (dt * 30)
        level = self._level
        for i, value in enumerate(target):
            level[i] = value if value > level[i] else level[i] * keep + value * (1 - keep)
        return tuple(int(min(1.0, max(0.0, v)) * cava.MAX) for v in level)


# What the panel shows, and how each is asked of cava: stereo is cava's
# own; the other three are its mono, from both channels mixed or from
# one of them.
CHANNELS = ("stereo", "both", "left", "right")
_MONO = {"both": "average", "left": "left", "right": "right"}


def _from_mono(peek) -> str:
    """An older theme's channels = "mono", with its mono_option."""
    option = peek("mono_option")
    return {"left": "left", "right": "right"}.get(option, "both")


@register("spectrum")
class SpectrumWidget(Widget):
    """An audio visualiser, driven by cava."""

    default_fps = 30.0
    summary = "Audio spectrum from cava: bars, mirrored, segmented or radial"
    schema = Schema(
        choice("type", TYPES, "bars", group="Shape"),
        choice("color_by", ("position", "level"),
               Auto(lambda v, spec: "level" if v.type == "radial" else "position",
                    "level for radial, else position"),
               group="Colour",
               doc="position: the gradient is fixed in place; level: each "
                   "bar takes one colour by how loud it is"),
        integer("bars", 32, 1, 512, group="Shape",
                doc="Number of bars; must be even in stereo"),
        choice("channels", CHANNELS, "stereo", group="Audio",
               legacy={"mono": _from_mono},
               doc="stereo: both channels, mirrored, lows in the middle; "
                   "both: the two mixed into one; left or right: that one alone"),
        # What a "mono" theme said which channel to use, before channels
        # said it itself. Read so such a theme still loads; never shown.
        string("mono_option", None, group="Audio", when=lambda v: False),
        boolean("reverse", False, group="Shape",
                doc="Swap low and high frequencies"),
        integer("smoothing", None, 0, 100, group="Audio",
                doc="cava's noise_reduction: 0 twitchy, 100 sluggish; "
                    "empty keeps cava's default"),
        string("input", None, group="Audio",
               doc="cava input method, e.g. pulse or pipewire; empty lets "
                   "cava choose"),
        string("source", None, group="Audio",
               doc="What that input method captures, e.g. auto"),
        integer("gap", 3, 0, 64, group="Shape", doc="Pixels between bars"),
        number("inner", 0.35, 0.0, 0.95, group="Shape",
               when=lambda v: v.type == "radial",
               doc="Hub radius as a fraction of the ring"),
        integer("segments", 12, 2, 128, group="Shape",
                when=lambda v: v.type == "segmented"),
        integer("segment_gap", 3, 0, 32, group="Shape",
                when=lambda v: v.type == "segmented"),
        boolean("peaks", False, group="Peaks",
                doc="Caps that hold at each bar's peak, then fall"),
        number("peak_hold", 0.35, 0.0, 10.0, group="Peaks",
               when=lambda v: v.peaks, doc="Seconds a cap waits before falling"),
        number("peak_gravity", 2.6, 0.01, 100.0, group="Peaks",
               when=lambda v: v.peaks, doc="How fast a falling cap speeds up"),
        integer("peak_size", 4, 1, 64, group="Peaks", when=lambda v: v.peaks),
        integer("peak_gap", 4, 0, 64, group="Peaks", when=lambda v: v.peaks,
                doc="Clear space needed under a cap before it is drawn"),
        color("origin", None, group="Colour",
              doc="Hub, centre line, or stubs the bars grow from; empty "
                  "for none"),
        integer("origin_width", 1, 1, 32, group="Colour",
                when=lambda v: v.origin is not None),
        colors("gradient", ("#00ff00", "#ffff00", "#ff0000"), group="Colour"),
        choice("when_silent", ("sweep", "none"), "sweep", group="Audio",
               doc="After a few seconds of silence: sweep runs a peak across "
                   "the bars until sound returns; none leaves them flat"),
        sections("cava", group="Advanced",
                 doc="Extra cava settings, passed through verbatim"),
    )

    __slots__ = (
        "_heard", "_sweep",
        "_bars", "_box", "_cap", "_clearance", "_fills", "_gap", "_gradient",
        "_gravity", "_held", "_hold", "_inner", "_last", "_lut", "_mask",
        "_mask_box", "_mask_draw", "_paint", "_peaks", "_segment_gap",
        "_segments", "_span", "_stream", "_surface_draw", "_threshold",
        "_time", "_velocity",
    )

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        o = self.schema.read(options, spec)
        kind = o.type
        # Radial reads better with a flat colour per spoke: a radial
        # position gradient shades every spoke the same way regardless of
        # how loud it is (see color_by).
        by = o.color_by
        self._bars = o.bars
        # Stereo, cava's own default, mirrors the channels with the
        # low frequencies in the middle -- on a ring that gives a shape
        # symmetric about the vertical, which is usually what you want.
        channels = o.channels
        if channels == "stereo" and self._bars % 2:
            raise PluginError(
                f"widget {spec.id!r}: stereo splits the bars between two "
                f"channels, so bars must be even; got {self._bars}"
            )
        if channels == "stereo":
            named: dict[str, dict] = {"output": {"channels": "stereo"}}
        else:
            named = {"output": {"channels": "mono", "mono_option": _MONO[channels]}}
        if o.reverse:
            named["output"]["reverse"] = 1
        if o.smoothing is not None:
            named["smoothing"] = {"noise_reduction": o.smoothing}
        method, source = o.input, o.source
        if method or source:
            named["input"] = {
                key: value
                for key, value in (("method", method), ("source", source))
                if value is not None
            }
        self._gap = o.gap
        self._inner = o.inner
        self._segments = o.segments
        self._segment_gap = o.segment_gap
        self._peaks: list[float] | None = [0.0] * self._bars if o.peaks else None
        self._hold = o.peak_hold
        self._gravity = o.peak_gravity
        self._cap = o.peak_size
        # Minimum clear space between bar and cap before the cap is drawn
        # at all. A cap riding on top of its own bar is not information,
        # and a gap under a rising bar looks like a mistake.
        self._clearance = o.peak_gap
        origin = o.origin
        origin_width = o.origin_width
        stops = o.gradient
        sections = cava.merge(named, o.cava, spec.id)
        options.reject_unknown()

        width, height = spec.rect.size
        self._paint = getattr(self, f"_paint_{kind}")
        self._span = self._span_for(kind, width, height)
        # How far a bar must sit below its cap before the cap is shown.
        # Segments are already discrete, so one empty segment is enough.
        self._threshold = 2 if kind == "segmented" else self._cap + self._clearance
        self._lut = gradient.lut(stops)
        if by == "level":
            # Coloured shapes go straight onto the surface: no mask, no
            # gradient image, no composite.
            self._fills = None
            self._gradient = None
            self._mask = self._mask_draw = self._mask_box = None
            self._surface_draw = ImageDraw.Draw(self.surface)
        else:
            self._fills = (255,) * self._bars
            self._gradient = _gradient(POSITION_MAP[kind], width, height, self._lut)
            self._mask = Image.new("L", (width, height), 0)
            self._mask_box = (0, 0, width - 1, height - 1)  # inclusive
            self._mask_draw = ImageDraw.Draw(self._mask)
            self._surface_draw = None
        if origin is not None:
            self._paint_origin(origin, origin_width, kind)
        self._last: tuple | None = None
        self._held = [0.0] * self._bars      # monotonic time each cap may fall
        self._velocity = [0.0] * self._bars
        self._time: float | None = None
        # When sound was last heard, for the sweep that fills a silence.
        self._sweep = (Sweep(self._bars, channels == "stereo")
                       if o.when_silent == "sweep" else None)
        self._heard: float | None = None

        self._stream: cava.Stream | None = None
        text = cava.config(
            self._bars, max(1, round(spec.fps or self.default_fps)), sections
        )
        if not cava.installed() and not preview():
            raise PluginError(
                f"widget {spec.id!r}: cava is not installed or not on PATH"
            )
        if dry_run():
            return  # --check: everything above is validated, nothing started
        # Starting the process is the last thing __init__ does, so nothing
        # can raise after the child exists and leave it with no owner.
        self._stream = cava.Stream(self._bars, text, spec.id)

    def _span_for(self, kind: str, width: int, height: int) -> int:
        """Full-scale in the units the change test counts."""
        if kind == "segmented":
            return self._segments
        if kind == "mirrored":
            return height // 2
        if kind == "radial":
            return max(1, int(min(width, height) / 2 * (1.0 - self._inner)))
        return height

    # -- peaks --

    def _fall(self, values: list[float], now: float) -> None:
        """Hold each cap, then let it accelerate away.

        Holding is what makes a peak readable, and it is free: a cap that
        has not moved changes no pixels. Gravity then clears it quickly
        rather than trailing the bar the way a constant speed does.
        """
        peaks = self._peaks
        assert peaks is not None
        delta = 0.0 if self._time is None else now - self._time
        self._time = now
        velocity, held = self._velocity, self._held
        for index, value in enumerate(values):
            if value >= peaks[index]:
                peaks[index] = value
                velocity[index] = 0.0
                held[index] = now + self._hold
            elif now >= held[index]:
                velocity[index] += self._gravity * delta
                peaks[index] = max(value, peaks[index] - velocity[index] * delta)

    # -- drawing --

    def render(self, now: float) -> bool:
        frame = self._stream.read() if self._stream is not None else None
        if self._heard is None:
            self._heard = now
        if frame is not None and max(frame, default=0) > SILENCE:
            self._heard = now
            if self._sweep is not None:
                self._sweep.reset()
        silent_for = now - self._heard
        if self._sweep is not None and silent_for >= SILENT_SEC:
            # Nothing heard for a while: a sweep, through the same drawing
            # as sound -- peaks and colours work on it unchanged. The first
            # real sound takes straight back over.
            frame = self._sweep.frame(now, silent_for - SILENT_SEC)
        elif frame is None:
            return False  # no new audio: nothing can have changed
        span = self._span
        values = [value / cava.MAX for value in frame]
        levels = tuple(int(value * span) for value in values)
        if self._peaks is None:
            state: tuple = levels
            peaks: tuple[int, ...] = ()
        else:
            self._fall(values, now)
            # -1 means "not drawn". Folding visibility into the state keeps
            # a cap that is still hidden from forcing a redraw as it moves.
            peaks = tuple(
                position if position - level >= self._threshold else -1
                for position, level in (
                    (int(peak * span), levels[index])
                    for index, peak in enumerate(self._peaks)
                )
            )
            state = (levels, peaks)
        if state == self._last:
            return False
        self._last = state

        self.clear()
        if self._fills is None:  # colour by level, straight to the surface
            lut = self._lut
            fills = tuple(gradient.colour_at(lut, value) for value in values)
            caps = tuple(gradient.colour_at(lut, peak) for peak in self._peaks or ())
            self._paint(self._surface_draw, levels, fills, peaks, caps)
        else:  # colour by position: shapes into a mask, one paste
            self._mask_draw.rectangle(self._mask_box, fill=0)
            self._paint(self._mask_draw, levels, self._fills, peaks, self._fills)
            self.surface.paste(self._gradient, (0, 0), self._mask)
        return True

    def _paint_origin(self, colour, width: int, kind: str) -> None:
        """The line the bars grow from, drawn once into the backdrop.

        Free per frame, and the only thing on screen during silence --
        without it a paused player leaves an empty rectangle, which reads
        as broken rather than idle. Each type gets the shape its own
        geometry implies: a hub circle, a centre line, or per-bar stubs.
        A rule along the bottom edge of a bars widget would just look
        like a border, so bars get stubs.
        """
        w, h = self.surface.size
        draw = ImageDraw.Draw(self.backdrop)
        if kind == "radial":
            radius = min(w, h) / 2 * self._inner
            draw.ellipse(
                (w / 2 - radius, h / 2 - radius, w / 2 + radius, h / 2 + radius),
                outline=colour, width=width,
            )
        elif kind == "mirrored":
            top = h // 2 - width // 2
            draw.rectangle((0, top, w - 1, top + width - 1), fill=colour)
        else:  # bars, segmented
            for _, x0, x1 in self._columns():
                draw.rectangle((x0, h - width, x1, h - 1), fill=colour)
        self.surface.paste(self.backdrop)

    def _columns(self):
        """(x0, x1) for each bar, skipping any too narrow to draw."""
        width = self.surface.width
        step = width / self._bars
        gap = self._gap
        for index in range(self._bars):
            x0 = int(index * step)
            x1 = int((index + 1) * step) - 1 - gap
            if x1 >= x0:
                yield index, x0, x1

    def _paint_bars(self, draw, levels, fills, peaks, caps) -> None:
        height = self.surface.height
        for index, x0, x1 in self._columns():
            level = levels[index]
            if level > 0:
                draw.rectangle((x0, height - level, x1, height - 1), fill=fills[index])
            if peaks and peaks[index] >= 0:
                top = height - peaks[index]
                draw.rectangle((x0, top, x1, top + self._cap - 1), fill=caps[index])

    def _paint_mirrored(self, draw, levels, fills, peaks, caps) -> None:
        middle = self.surface.height // 2
        for index, x0, x1 in self._columns():
            half = levels[index]
            if half > 0:
                draw.rectangle((x0, middle - half, x1, middle + half), fill=fills[index])
            if peaks and peaks[index] >= 0:
                offset, fill = peaks[index], caps[index]
                draw.rectangle((x0, middle - offset, x1,
                                middle - offset + self._cap - 1), fill=fill)
                draw.rectangle((x0, middle + offset - self._cap + 1, x1,
                                middle + offset), fill=fill)

    def _paint_segmented(self, draw, levels, fills, peaks, caps) -> None:
        height = self.surface.height
        block = height / self._segments
        for index, x0, x1 in self._columns():
            fill = fills[index]
            for segment in range(levels[index]):
                y1 = height - segment * block - self._segment_gap
                y0 = height - (segment + 1) * block
                if y1 > y0:
                    draw.rectangle((x0, int(y0), x1, int(y1)), fill=fill)
            if peaks and peaks[index] > 0:
                segment = min(peaks[index], self._segments) - 1
                y1 = height - segment * block - self._segment_gap
                y0 = height - (segment + 1) * block
                if y1 > y0:
                    draw.rectangle((x0, int(y0), x1, int(y1)), fill=caps[index])

    def _paint_radial(self, draw, levels, fills, peaks, caps) -> None:
        width, height = self.surface.size
        cx, cy = width / 2, height / 2
        inner = min(width, height) / 2 * self._inner
        # Fixed pixel width, not a wedge: a wedge widens with radius, so a
        # loud bar becomes a fat slice and the ring reads as a blob.
        # Sized from the inner circumference so spokes never meet at the hub.
        spoke = max(2.0, 2 * math.pi * inner / self._bars - self._gap)
        step = 360.0 / self._bars
        for index in range(self._bars):
            angle = index * step - 90.0
            level = levels[index]
            if level > 0:
                _spoke(draw, cx, cy, angle, inner, inner + level, spoke, fills[index])
            if peaks and peaks[index] >= 0:
                end = inner + peaks[index]
                _spoke(draw, cx, cy, angle, end - self._cap, end, spoke,
                       caps[index])

    # -- teardown --

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None


def _spoke(draw, cx, cy, angle, r0, r1, width, fill) -> None:
    """A rectangle of constant width, pointing out from the hub."""
    radians = math.radians(angle)
    ux, uy = math.cos(radians), math.sin(radians)
    px, py = -uy, ux
    half = width / 2
    draw.polygon(
        [
            (cx + r0 * ux + half * px, cy + r0 * uy + half * py),
            (cx + r1 * ux + half * px, cy + r1 * uy + half * py),
            (cx + r1 * ux - half * px, cy + r1 * uy - half * py),
            (cx + r0 * ux - half * px, cy + r0 * uy - half * py),
        ],
        fill=fill,
    )


def _gradient(kind: str, width: int, height: int, tables) -> Image.Image:
    """The colour field a frame is pasted from, built once at load."""
    position = gradient.position_map(kind, width, height)
    return Image.merge("RGB", tuple(position.point(table) for table in tables))
