"""The main loop.

* One full-screen draw at startup (and after a theme change); regions
  after that. A full frame costs a big encode, ~100 HID writes and a
  20 ms settle before the next region.
* The loop ticks at `fps`; each widget renders only when its own
  interval is due and is pushed only when it reports a change.
* Everything that changed in a frame goes out as one region, cropped
  from a panel-sized composite every widget paints into. Each push
  waits out a ~10 ms settle, so one larger region beats several small
  ones (measured: 2.6 ms against 42.9 ms for five widgets).
* The theme is watched: once a second its files' state is compared,
  and a theme that loads and validates completely replaces the running
  one. One that does not is logged and the panel keeps what it had.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from akp02 import AKP02, Orientation

from typing import TYPE_CHECKING

from . import __version__, config as config_module, layout, paths, plugins
from . import theme as theme_module
from .config import Config
from .plugins.base import Widget
from .plugins.label import reset_locale
from .schema import IMAGE
from .table import SettingsError
from .theme import Theme

if TYPE_CHECKING:
    from .control import Server

WATCH_INTERVAL_SEC = 1.0

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
        watch: bool = True,
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
        self.watch = watch
        self._stamp = _stamp(theme)
        # The control socket, if the daemon runs one (see control.py),
        # and whether a command swapped the theme under the loop.
        self.control: Server | None = None
        self._swapped = False
        self.brightness = config.device.brightness
        self._started = time.monotonic()

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

        self.widgets, self.screen = self._build(self.theme)
        self._paint()

    def _build(self, theme: Theme) -> tuple[tuple[Widget, ...], Image.Image]:
        """Validate a theme and construct its widgets and composite.

        Everything that can fail because of a bad theme fails here,
        before anything on the panel changes.
        """
        period = 1.0 / self.config.fps
        specs = theme.widgets
        layout.validate(
            [(spec.id, spec.rect) for spec in specs],
            layout.screen_size(theme.orientation),
            theme.orientation,
            self.config.device.inverted,
        )
        # Built one at a time and closed on failure. A widget may own a
        # child process, and a Popen is not reaped by garbage collection
        # or by exit -- so a bad fifth widget would otherwise leave four
        # cava processes holding the audio input, once per retry.
        built: list[Widget] = []
        try:
            for spec in specs:
                built.append(plugins.create(spec, theme))
        except Exception:
            for widget in built:
                try:
                    widget.close()
                except Exception:
                    log.exception("widget %r failed to close", widget.id)
            raise
        widgets = tuple(built)
        for widget in widgets:
            if widget.period < period:
                # A rate the widget picked itself (an animation's, a
                # gliding needle's) is simply capped; only one the theme
                # asked for is news.
                if not (getattr(widget, "fps_auto", False)
                        or getattr(widget, "animating", False)):
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
        screen = theme.background.copy()
        for widget in widgets:
            screen.paste(widget.surface, widget.rect.at)
        return widgets, screen

    def _paint(self) -> None:
        """The full-screen draw: at startup, and after a reload."""
        assert self.screen is not None
        started = time.perf_counter()
        self.panel.show(self.screen)
        log.debug("background painted in %.1f ms", (time.perf_counter() - started) * 1e3)

    # -- reload --

    def reload_if_changed(self) -> bool:
        """Load the theme again if its files changed. True if swapped."""
        stamp = _stamp(self.theme)
        if stamp == self._stamp:
            return False
        # Remembered whether or not the load works, so a broken save is
        # reported once rather than every second until it is fixed.
        self._stamp = stamp
        try:
            self.switch(self.theme.path)
        except (SettingsError, layout.LayoutError, OSError) as exc:
            log.error("theme changed but did not load; keeping the current one: %s", exc)
            return False
        return True

    def switch(self, path: Path) -> None:
        """Run the theme at `path` instead.

        Loaded, laid out and every widget built before anything on the
        panel changes: a theme with a mistake raises, and the panel keeps
        showing the current one.
        """
        theme = theme_module.load(path)
        # The locale check is per build: a label may change its own.
        reset_locale()
        widgets, screen = self._build(theme)
        for widget in self.widgets:
            try:
                widget.close()
            except Exception:
                log.exception("widget %r failed to close", widget.id)
        if theme.orientation is not self.theme.orientation:
            self.panel.orientation(theme.orientation)
        self.theme, self.widgets, self.screen = theme, widgets, screen
        self._stamp = _stamp(theme)
        self._swapped = True
        self._paint()
        log.info("theme %r from %s (%d widget(s))", theme.name, path, len(widgets))

    # -- the control socket --

    def command(self, request: dict) -> dict:
        """Answer one control request (see control.py)."""
        cmd = request.get("cmd")
        if cmd == "status":
            return {
                "ok": True, "theme": self.theme.name, "path": str(self.theme.path),
                "orientation": self.theme.orientation.name.lower(),
                "widgets": len(self.widgets), "fps": self.config.fps,
                "brightness": self.brightness, "pid": os.getpid(),
                "uptime": round(time.monotonic() - self._started, 1),
                "version": __version__,
            }
        if cmd == "themes":
            names = sorted({n for base in self.config.theme_paths
                            for n in paths.theme_names(base)})
            return {"ok": True, "themes": names, "current": self.theme.name,
                    "folder": str(self.config.theme_paths[0])}
        if cmd == "theme":
            name = request.get("name")
            if not isinstance(name, str) or not name:
                return {"ok": False, "error": "theme needs a name"}
            try:
                path = theme_module.find(name, self.config.theme_paths)
                self.switch(path)
            except (SettingsError, layout.LayoutError, OSError) as exc:
                return {"ok": False, "error": f"{exc}; still showing {self.theme.name!r}"}
            saved = None
            if request.get("save"):
                try:
                    saved = str(config_module.save_theme(name, self.config.source))
                except OSError as exc:
                    return {"ok": False, "error": f"switched, but not saved: {exc}"}
            return {"ok": True, "theme": self.theme.name, "saved": saved}
        if cmd == "brightness":
            value = request.get("value")
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not AKP02.BRIGHTNESS_MIN <= value <= AKP02.BRIGHTNESS_MAX:
                return {"ok": False, "error": f"brightness is a whole number "
                        f"{AKP02.BRIGHTNESS_MIN}-{AKP02.BRIGHTNESS_MAX}"}
            self.panel.set_brightness(value)
            self.brightness = value
            return {"ok": True, "brightness": value}
        if cmd == "reload":
            try:
                self.switch(self.theme.path)
            except (SettingsError, layout.LayoutError, OSError) as exc:
                return {"ok": False, "error": f"{exc}; still showing {self.theme.name!r}"}
            return {"ok": True, "theme": self.theme.name}
        return {"ok": False, "error": f"unknown command {cmd!r}; try status, themes, "
                "theme, brightness or reload"}

    def _wait(self, seconds: float) -> None:
        """The pause between frames: on the socket if there is one.

        A request wakes it early, but the frame still waits out its time,
        so requests never shift the schedule (and stall anything moving).
        Only a switched theme or a stop ends the wait sooner.
        """
        if self.control is None:
            if seconds > 0:
                time.sleep(seconds)
            return
        until = time.monotonic() + seconds
        while True:
            self.control.serve(max(0.0, until - time.monotonic()))
            if self._swapped or self._stop or time.monotonic() >= until:
                return

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
        next_watch = start + WATCH_INTERVAL_SEC if self.watch else None
        screen = self.screen
        assert screen is not None

        while not self._stop:
            frame += 1
            deadline = start + frame * period
            now = monotonic()
            box: tuple[int, int, int, int] | None = None

            if next_watch is not None and now >= next_watch:
                next_watch = now + WATCH_INTERVAL_SEC
                if self.reload_if_changed():
                    self._swapped = False
                    widgets, screen = self.widgets, self.screen
                    assert screen is not None
                    now = monotonic()
                    for widget in widgets:
                        widget.due = now

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
                # Widened to whole JPEG blocks of the buffer, so a widget's
                # edge pixels encode the same whether it goes out alone or
                # with a neighbour; otherwise they flicker as the two
                # alternate. This also keeps the region colour-safe.
                box = layout.on_block_grid(box, self.theme.orientation, self.panel.inverted)
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
            # Waiting on the control socket, when there is one: a command
            # is answered within a frame, and a late frame still looks.
            self._wait(max(0.0, remaining))
            if self._swapped:
                # A command switched the theme: draw the new one's widgets.
                self._swapped = False
                widgets, screen = self.widgets, self.screen
                assert screen is not None
                now = monotonic()
                for widget in widgets:
                    widget.due = now
            if remaining <= 0:
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


def _stamp(theme: Theme) -> tuple:
    """The file state a reload compares: theme.toml, the wallpaper, and
    every picture a widget shows.

    The files are found from the theme as loaded, so a theme that
    switches to a new file is caught by theme.toml changing, and a
    picture edited in place by its own stamp.
    """
    out = []
    files = [theme.path / "theme.toml"]
    if theme.wallpaper:
        files.append(theme.path / theme.wallpaper)
    files.extend(theme.path / Path(name).expanduser() for name in _pictures(theme))
    for file in files:
        try:
            info = file.stat()
        except OSError:
            out.append(None)
        else:
            out.append((info.st_mtime_ns, info.st_size))
    return tuple(out)


def _pictures(theme: Theme) -> list[str]:
    """The files widgets name in image options (an image widget's path)."""
    names = []
    for spec in theme.widgets:
        cls = plugins.lookup(spec.plugin)
        for option in (cls.schema if cls is not None else ()):
            if option.kind == IMAGE:
                value = spec.options.peek(option.key)
                if isinstance(value, str):
                    names.append(value)
    return names
