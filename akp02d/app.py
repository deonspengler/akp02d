"""The main loop.

Shape of a frame, and why:

* One full-screen draw happens at startup and never again. A full frame
  is a ~886k-pixel transpose, a JPEG encode of the same, and something
  like a hundred 1024-byte HID writes; and the library must then insert
  FULL_TO_REGION_SETTLE_SEC (20 ms, over half a frame at 30 FPS) before
  the next region. Both costs are paid once, before the loop starts.

* After that, only regions. Widgets do not overlap -- layout.validate
  guarantees it -- so each widget's box is an independent push and there
  is no compositing step at all.

* 30 FPS is the scheduling quantum, not a redraw rate. A widget is
  visited only when its own interval is due, and pushed only when it
  reports a change. A theme of 1 Hz widgets does real work on one frame
  in thirty and sleeps through the other twenty-nine.

* Encode and push are separate. encode_region() takes no lock and
  touches no handle, so its result caches on the widget and a push is
  just bytes the library sends untouched. It also means the encode can
  later move to a worker thread without the push path changing.

* Everything dirty in a frame goes out as one region. The driver makes
  each push wait out the panel's settle first -- about 10 ms, and per
  push rather than per byte -- so a second region in the same frame
  costs more than the whole of a big one. Widgets given the same rate
  come due together, so that collision is the ordinary case for a panel
  of stats, not a rare one.

  Combining means cropping the box that covers them all out of a
  panel-sized composite every widget paints into, which sends the
  unchanged pixels between them too. That is far cheaper than it
  sounds: one saved settle buys about 80 KiB of payload, and the whole
  panel encodes to around 20. Measured on a five-widget theme, a frame
  where every widget came due cost 42.9 ms as separate pushes and
  2.6 ms combined.

  So there is nothing to ration and no queue: one push per frame is not
  a compromise, it is the cheapest thing available.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from PIL import Image

from akp02 import AKP02, Orientation

from . import layout, plugins
from .config import Config
from .plugins.base import Widget
from .theme import Theme

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Stats:
    """Cheap counters. Two perf_counter calls per dirty widget."""

    frames: int = 0
    ticks: int = 0
    pushes: int = 0
    payload_bytes: int = 0
    render_sec: float = 0.0
    encode_sec: float = 0.0
    push_sec: float = 0.0
    late_frames: int = 0
    worst_frame_sec: float = 0.0
    since: float = field(default_factory=time.monotonic)

    def report(self, now: float) -> str:
        elapsed = max(now - self.since, 1e-9)
        return (
            f"{self.frames / elapsed:5.1f} fps  "
            f"{self.pushes / elapsed:5.1f} push/s  "
            f"{self.payload_bytes / elapsed / 1024:6.1f} KiB/s  "
            f"render {_us(self.render_sec, self.ticks)}  "
            f"encode {_us(self.encode_sec, self.pushes)}  "
            f"push {_us(self.push_sec, self.pushes)}  "
            f"worst frame {self.worst_frame_sec * 1e3:.1f} ms  "
            f"late {self.late_frames}"
        )

    def reset(self, now: float) -> None:
        self.frames = self.ticks = self.pushes = self.payload_bytes = 0
        self.render_sec = self.encode_sec = self.push_sec = 0.0
        self.late_frames = 0
        self.worst_frame_sec = 0.0
        self.since = now


def _us(total: float, count: int) -> str:
    return f"{total / count * 1e6:6.0f}us" if count else "     --"


class App:
    """Owns the panel, the widgets, and the loop."""

    def __init__(
        self,
        config: Config,
        theme: Theme,
        panel: AKP02,
        *,
        strict: bool = False,
        stats_interval: float = 0.0,
    ) -> None:
        self.config = config
        self.theme = theme
        self.panel = panel
        self.strict = strict
        self.stats_interval = stats_interval
        self.stats = Stats()
        self.widgets: tuple[Widget, ...] = ()
        self.screen: Image.Image | None = None
        self._stop = False

    # -- setup --

    def prepare(self) -> None:
        """Configure the device, validate the layout, build the widgets.

        Everything that can fail because of a bad theme fails here,
        before a single frame is drawn.
        """
        device = self.config.device
        # orientation() also sends SET, which persists on the device and
        # changes its power-on splash. LANDSCAPE is already the library's
        # internal default, so leaving it alone avoids overwriting a
        # setting the user never asked us to touch.
        if self.theme.orientation is not Orientation.LANDSCAPE:
            self.panel.orientation(self.theme.orientation)
        self.panel.inverted = device.inverted
        # Brightness first, then screen_on: the device resets its
        # backlight when the screen comes on, and screen_on() re-applies
        # whatever set_brightness() last recorded.
        self.panel.set_brightness(device.brightness)
        self.panel.screen_on()

        period = 1.0 / self.config.fps
        specs = self.theme.widgets
        layout.validate(
            [(spec.id, spec.rect) for spec in specs],
            self.panel.size,
            self.theme.orientation,
            device.inverted,
        )
        # Built one at a time and closed on failure. A widget may own a
        # child process, and a Popen is not reaped by garbage collection
        # or by exit -- so a bad fifth widget would otherwise leave four
        # cava processes holding the audio input, once per retry.
        built: list[Widget] = []
        try:
            for spec in specs:
                built.append(plugins.create(spec, self.theme))
        except Exception:
            for widget in built:
                try:
                    widget.close()
                except Exception:
                    log.exception("widget %r failed to close", widget.id)
            raise
        self.widgets = tuple(built)
        for widget in self.widgets:
            if widget.period < period:
                log.warning(
                    "%s asks for %g fps but the loop runs at %g; it will "
                    "update every frame",
                    widget.id, widget.fps, self.config.fps,
                )
                widget.period = period
            log.debug("%s: (%d,%d) %dx%d at %g fps", widget.id, *widget.rect, widget.fps)
        # What the panel is showing, kept in step as widgets draw into
        # it. A combined push is a crop of this, so the pixels between
        # the widgets it covers are the ones already on the glass.
        self.screen = self.theme.background.copy()
        for widget in self.widgets:
            self.screen.paste(widget.surface, widget.rect.at)
        # The one and only full-screen draw.
        started = time.perf_counter()
        self.panel.show(self.screen)
        log.debug("background painted in %.1f ms", (time.perf_counter() - started) * 1e3)

    # -- loop --

    def stop(self) -> None:
        """Ask the loop to finish the current frame and return."""
        self._stop = True

    def run(self) -> None:
        period = 1.0 / self.config.fps
        widgets: Sequence[Widget] = self.widgets
        panel = self.panel
        stats = self.stats
        strict = self.strict
        monotonic = time.monotonic
        counter = time.perf_counter

        start = monotonic()
        for widget in widgets:
            widget.due = start
        frame = 0
        next_stats = start + self.stats_interval if self.stats_interval else None
        screen = self.screen
        assert screen is not None
        orientation = self.theme.orientation
        inverted = self.config.device.inverted

        while not self._stop:
            frame += 1
            deadline = start + frame * period
            now = monotonic()
            box: tuple[int, int, int, int] | None = None

            for widget in widgets:
                if now < widget.due:
                    continue
                widget.due += widget.period
                if widget.due <= now:
                    # Fell behind (a slow render, or the machine slept).
                    # Resync rather than trying to catch up: a burst of
                    # back-to-back renders helps nobody.
                    widget.due = now + widget.period
                mark = counter()
                changed = widget.render(now)
                stats.render_sec += counter() - mark
                stats.ticks += 1
                if changed:
                    screen.paste(widget.surface, widget.rect.at)
                    rect = widget.rect
                    here = (rect.x, rect.y, rect.x + rect.width, rect.y + rect.height)
                    box = here if box is None else (
                        min(box[0], here[0]), min(box[1], here[1]),
                        max(box[2], here[2]), max(box[3], here[3]),
                    )

            if box is not None:
                # Grow the box until its offset is one the panel renders
                # in true colour. Outward only, and the extra pixels are
                # already in the composite, so it costs bytes and nothing
                # else.
                #box = layout.snap(box, orientation, inverted)
                mark = counter()
                jpeg = panel.encode_region(screen.crop(box))
                stats.encode_sec += counter() - mark
                mark = counter()
                at = (box[0], box[1])
                if strict:
                    # Asserts the bytes still match the box: catches a
                    # stale composite or a wrong rotation, which
                    # otherwise look like correct bytes for another shape.
                    panel.show(jpeg, at=at, size=(box[2] - box[0], box[3] - box[1]))
                else:
                    panel.show(jpeg, at=at)
                stats.push_sec += counter() - mark
                stats.payload_bytes += len(jpeg)
                stats.pushes += 1

            stats.frames += 1
            spent = monotonic()
            stats.worst_frame_sec = max(stats.worst_frame_sec, spent - (deadline - period))

            if next_stats is not None and spent >= next_stats:
                log.info("%s", stats.report(spent))
                stats.reset(spent)
                next_stats = spent + self.stats_interval

            remaining = deadline - spent
            if remaining > 0:
                time.sleep(remaining)
            else:
                stats.late_frames += 1
                if remaining < -period:
                    # More than a whole frame behind: drop the missed
                    # frames instead of running a catch-up burst that
                    # would only fall further behind.
                    frame = int((spent - start) / period)

    # -- teardown --

    def close(self) -> None:
        for widget in self.widgets:
            try:
                widget.close()
            except Exception:
                log.exception("widget %r failed to close", widget.id)
        if self.config.device.clear_on_exit:
            try:
                self.panel.clear()
            except Exception:
                log.debug("could not clear the panel on exit", exc_info=True)
