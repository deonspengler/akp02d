"""The preview: the daemon's own widgets, drawing into a picture.

No Qt in here, so it can be tested and reused. The engine takes a
ThemeDocument, builds a Theme from its text exactly as the daemon would
build one from the saved file, constructs every widget, and keeps a
panel-sized composite they paint into -- the same composite app.py
crops its pushes from. A tick renders whichever widgets are due.

Where it differs from the daemon, it differs to stay useful while a
theme is half-made:

* A bad widget does not stop the others. It is recorded with its error
  and left blank, and the rest keep drawing.
* A bad [theme] table keeps the last good background rather than going
  black, and reports why.
* A setting that only fails on this machine -- a locale that is not
  generated here, an interface named for the panel's own computer --
  is previewed without it and reported as a warning, since it may well
  be right where the daemon runs.
* Widgets are rebuilt only when their own entry changed, so editing one
  label does not reset a graph's history.
"""

from __future__ import annotations

import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from akp02 import AKP02, Orientation

from .. import layout, sources
from ..layout import Rect
from ..plugins import base as plugin_base, cava, create
from ..plugins.base import Widget, WidgetSpec
from ..plugins.label import reset_locale
from ..plugins.spectrum import SpectrumWidget
from ..table import Table
from .. import theme as theme_module
from .document import Ref, ThemeDocument
from . import simulate

log = logging.getLogger(__name__)

ERROR, WARNING = "error", "warning"

# Keys whose values describe the machine the daemon runs on rather than
# the look. If a widget fails to build here, it is retried without them.
MACHINE_KEYS = {"locale", "interface", "path"}


@dataclass(slots=True)
class Problem:
    level: str           # ERROR or WARNING
    message: str
    widget: str | None = None   # widget id, or None for the theme

    def __post_init__(self) -> None:
        # The daemon's messages name the widget because a log line has
        # nothing else to; here the problem is already filed under it.
        if self.widget is not None:
            prefix = f"widget {self.widget!r}: "
            self.message = self.message.replace(prefix, "", 1)


@dataclass(slots=True)
class Slot:
    """One widget entry: what was asked for, and what came of it."""

    ref: Ref
    id: str
    plugin: str
    rect: Rect | None
    key: tuple
    spec: WidgetSpec | None = None
    widget: Widget | None = None
    error: str | None = None
    warning: str | None = None
    due: float = 0.0

    @property
    def fps(self) -> float | None:
        return self.widget.fps if self.widget is not None else None


@dataclass(slots=True)
class Encoding:
    quality: int = AKP02.JPEG_QUALITY
    subsampling: int = AKP02.JPEG_SUBSAMPLING


class Engine:
    """Builds, ticks and composites a theme's widgets, no panel needed."""

    LOOP_FPS = 30.0

    def __init__(self, *, inverted: bool = False, encoding: Encoding | None = None,
                 settings: simulate.Settings | None = None) -> None:
        self.inverted = inverted
        self.encoding = encoding or Encoding()
        self.settings = settings or simulate.Settings()
        self.theme: theme_module.Theme | None = None
        self.theme_error: str | None = None
        self.slots: list[Slot] = []
        self.screen: Image.Image | None = None
        self._theme_key: tuple | None = None
        self._locales: tuple = ()
        self._audio_key: str = ""
        self._entries: dict[Ref, dict[str, Any]] = {}

    # -- building --

    @property
    def orientation(self) -> Orientation:
        return self.theme.orientation if self.theme else Orientation.PORTRAIT

    @property
    def size(self) -> tuple[int, int]:
        return layout.screen_size(self.orientation)

    def load(self, document: ThemeDocument) -> None:
        """Bring the preview in line with `document`, rebuilding only
        what changed."""
        path = document.directory or Path.cwd()
        data = document.data
        theme_table = data.get("theme", {})
        theme_key = (str(path), repr(theme_table), _wallpaper_state(path, theme_table))
        theme_changed = theme_key != self._theme_key
        if theme_changed:
            try:
                info = theme_module.meta(Table(theme_table, "theme"), path)
            except Exception as exc:  # noqa: BLE001 -- shown, not raised
                self.theme_error = str(exc)
                if self.theme is None:
                    blank = Image.new("RGB", layout.screen_size(Orientation.PORTRAIT))
                    info = theme_module.Meta(Orientation.PORTRAIT, blank, None)
                else:
                    info = None
            else:
                self.theme_error = None
            if info is not None:
                self.theme = theme_module.Theme(
                    name=path.name, path=path, orientation=info.orientation,
                    background=info.background, font_default=info.font_default,
                    widgets=(), wallpaper=info.wallpaper,
                )
            self._theme_key = theme_key
        assert self.theme is not None

        refs = document.refs()
        entries = {ref: document.entry(ref) for ref in refs}
        self._entries = entries
        locales = tuple(
            entry.get("locale") for ref, entry in entries.items() if ref.plugin == "label"
        )
        locales_changed = locales != self._locales
        if locales_changed:
            reset_locale()
            self._locales = locales
        audio_key = self.settings.audio if self.settings.audio == simulate.CAVA else "sim"
        audio_changed = audio_key != self._audio_key
        self._audio_key = audio_key

        old = {slot.ref: slot for slot in self.slots}
        new_slots: list[Slot] = []
        seen_ids: set[str] = set()
        for ref in refs:
            entry = entries[ref]
            key = (repr(entry), theme_key)
            previous = old.pop(ref, None)
            reuse = (
                previous is not None
                and previous.key == key
                and not theme_changed
                and not (locales_changed and ref.plugin == "label")
                and not (audio_changed and ref.plugin == "spectrum")
            )
            if reuse:
                assert previous is not None
                slot = previous
            else:
                if previous is not None:
                    _close(previous)
                slot = self._build(ref, entry, key)
            if slot.id in seen_ids:
                _close(slot)
                slot.widget = None
                slot.error = f"duplicate widget id {slot.id!r}"
            seen_ids.add(slot.id)
            new_slots.append(slot)
        for leftover in old.values():
            _close(leftover)
        self.slots = new_slots
        self._composite()

    def rebuild(self, slot_id: str | None = None) -> None:
        """Rebuild one widget (or all) from its current entry, e.g. after
        switching between simulated and real audio."""
        for index, slot in enumerate(self.slots):
            if slot_id is not None and slot.id != slot_id:
                continue
            _close(slot)
            entry = self._entries.get(slot.ref)
            if entry is None:
                continue
            self.slots[index] = self._build(slot.ref, entry, slot.key)
        self._composite()

    def _build(self, ref: Ref, entry: dict[str, Any], key: tuple) -> Slot:
        assert self.theme is not None
        widget_id = entry.get("id") if isinstance(entry.get("id"), str) else None
        slot = Slot(ref=ref, id=widget_id or ref.default_id, plugin=ref.plugin,
                    rect=_rect_of(entry), key=key)
        error = self._try_build(slot, entry)
        if error is None:
            return slot
        machine = MACHINE_KEYS & set(entry)
        if machine:
            trimmed = {k: v for k, v in entry.items() if k not in machine}
            if self._try_build(slot, trimmed) is None:
                slot.warning = (
                    f"{error} -- previewed without {', '.join(sorted(machine))}; "
                    f"it may still work on the machine the panel is on"
                )
                return slot
        slot.error = error
        return slot

    def _try_build(self, slot: Slot, entry: dict[str, Any]) -> str | None:
        assert self.theme is not None
        index = slot.ref.index - 1
        table = Table(entry, f"widget.{slot.plugin}[{index}]")
        settings = self.settings
        use_cava = slot.plugin == "spectrum" and settings.audio == simulate.CAVA
        previous_wrap = sources.wrap
        sources.wrap = lambda real, name: simulate.StandIn(real, name, settings)
        plugin_base.set_preview(not use_cava)
        try:
            spec = theme_module.widget_spec(slot.plugin, slot.ref.index, table)
            slot.spec = spec
            slot.rect = spec.rect
            slot.id = spec.id
            widget = create(spec, self.theme)
        except Exception as exc:  # noqa: BLE001 -- shown, not raised
            return str(exc) or type(exc).__name__
        finally:
            sources.wrap = previous_wrap
            plugin_base.set_preview(False)
        if isinstance(widget, SpectrumWidget) and widget._stream is None:
            channels = entry.get("channels", "stereo")
            widget._stream = simulate.Audio(widget._bars, channels == "stereo", settings)
        widget.period = max(widget.period, 1.0 / self.LOOP_FPS)
        slot.widget = widget
        self._seed(widget)
        slot.due = 0.0
        slot.error = None
        return None

    def _seed(self, widget: Widget) -> None:
        """Give a history-keeping widget its past at once, when the data
        is made up and so has a past to read."""
        seed = getattr(widget, "seed", None)
        if seed is None or self.settings.data == simulate.LIVE:
            return
        try:
            seed(time.monotonic(), widget.period)
        except Exception:  # noqa: BLE001 -- a preview nicety, never fatal
            log.debug("seeding %r failed", widget, exc_info=True)

    def reseed(self) -> None:
        """After switching data mode, refill histories from the new data."""
        for slot in self.slots:
            if slot.widget is not None:
                self._seed(slot.widget)
                slot.due = 0.0

    # -- running --

    def tick(self, now: float | None = None) -> bool:
        """Render every widget that is due. True if the picture changed."""
        if self.screen is None:
            return False
        now = time.monotonic() if now is None else now
        changed = False
        for slot in self.slots:
            widget = slot.widget
            if widget is None or now < slot.due:
                continue
            slot.due = now + widget.period
            try:
                drew = widget.render(now)
            except Exception as exc:  # noqa: BLE001 -- shown, not raised
                slot.error = str(exc) or type(exc).__name__
                _close(slot)
                slot.widget = None
                self._paste_backdrop(slot)
                changed = True
                continue
            if drew:
                self.screen.paste(widget.surface, widget.rect.at)
                changed = True
        return changed

    def _composite(self) -> None:
        assert self.theme is not None
        self.screen = self.theme.background.copy()
        for slot in self.slots:
            if slot.widget is not None:
                self.screen.paste(slot.widget.surface, slot.widget.rect.at)

    def _paste_backdrop(self, slot: Slot) -> None:
        if self.screen is None or self.theme is None or slot.rect is None:
            return
        self.screen.paste(self.theme.backdrop(slot.rect), slot.rect.at)

    def frame(self, as_sent: bool = False) -> Image.Image:
        """The panel as it stands; `as_sent` runs it through the JPEG the
        daemon would send."""
        assert self.screen is not None
        if not as_sent:
            return self.screen.copy()
        return as_sent_image(self.screen, self.orientation, self.inverted,
                             self.encoding)[0]

    def close(self) -> None:
        for slot in self.slots:
            _close(slot)
        self.slots = []

    # -- checks --

    def problems(self) -> list[Problem]:
        """Everything the daemon would refuse, plus preview warnings."""
        out: list[Problem] = []
        if self.theme_error:
            out.append(Problem(ERROR, self.theme_error))
        for slot in self.slots:
            if slot.error:
                out.append(Problem(ERROR, slot.error, slot.id))
            if slot.warning:
                out.append(Problem(WARNING, slot.warning, slot.id))
            if slot.plugin == "spectrum" and slot.widget is not None and not cava.installed():
                out.append(Problem(
                    WARNING,
                    "cava is not installed here; the preview uses simulated "
                    "audio, and the daemon will refuse this widget until it is",
                    slot.id,
                ))
        out += layout_problems(
            [(slot.id, slot.rect) for slot in self.slots if slot.rect is not None],
            self.size, self.orientation, self.inverted,
        )
        return out


def _close(slot: Slot) -> None:
    if slot.widget is not None:
        try:
            slot.widget.close()
        except Exception:  # noqa: BLE001
            log.exception("widget %r failed to close", slot.id)
        slot.widget = None


def _rect_of(entry: dict[str, Any]) -> Rect | None:
    try:
        values = [entry[k] for k in ("x", "y", "width", "height")]
    except KeyError:
        return None
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return Rect(*values)
    return None


def _wallpaper_state(path: Path, theme_table: Any) -> tuple:
    """So a re-saved wallpaper file triggers a rebuild."""
    name = theme_table.get("wallpaper") if isinstance(theme_table, dict) else None
    if not isinstance(name, str):
        return ()
    try:
        stat = (path / name).stat()
    except OSError:
        return (name,)
    return (name, stat.st_mtime_ns, stat.st_size)


# -- layout --


def layout_problems(
    rects: list[tuple[str, Rect]], screen: tuple[int, int],
    orientation: Orientation, inverted: bool,
) -> list[Problem]:
    """layout.validate's checks, all of them rather than the first."""
    out: list[Problem] = []
    width, height = screen
    for name, rect in rects:
        if (rect.x < 0 or rect.y < 0 or rect.x + rect.width > width
                or rect.y + rect.height > height):
            out.append(Problem(ERROR, f"does not fit the {width}x{height} screen", name))
        elif not layout.is_aligned(rect, orientation, inverted):
            axis, value, extent, _ = layout.bound_axis(rect, orientation, inverted)
            options = layout.nearest_aligned(rect, orientation, inverted)
            fix = (" or ".join(map(str, options)) if options else "none for this size")
            out.append(Problem(
                ERROR, f"{axis}={value} is not colour-safe; nearest: {fix}", name))
    for index, (name, rect) in enumerate(rects):
        for other_name, other in rects[index + 1:]:
            if rect.overlaps(other):
                out.append(Problem(ERROR, f"overlaps {other_name!r}", name))
    return out


def snap(rect: Rect, orientation: Orientation, inverted: bool,
         screen: tuple[int, int], moving: str = "move") -> Rect:
    """The nearest colour-safe rect, changing only what is being dragged.

    `moving` is "move" (shift the box), or the edges being dragged as a
    string of l/t/r/b. Only the coordinate the panel's rule binds can be
    wrong, so this searches shifts of the dragged edges along that axis
    and keeps the smallest one that lands on a safe offset.
    """
    width, height = screen
    rect = _clamp(rect, screen)
    if layout.is_aligned(rect, orientation, inverted):
        return rect
    axis, _, _, _ = layout.bound_axis(rect, orientation, inverted)
    modulus = AKP02.SHORT_AXIS_ALIGN_MODULUS
    candidates = []
    for distance in range(1, modulus + 1):
        for shift in (-distance, distance):
            candidate = _shift(rect, axis, shift, moving)
            if candidate is None:
                continue
            if (candidate.width < 1 or candidate.height < 1 or candidate.x < 0
                    or candidate.y < 0 or candidate.x + candidate.width > width
                    or candidate.y + candidate.height > height):
                continue
            if layout.is_aligned(candidate, orientation, inverted):
                candidates.append(candidate)
        if candidates:
            return candidates[0]
    return rect


def _shift(rect: Rect, axis: str, d: int, moving: str) -> Rect | None:
    x, y, w, h = rect
    if moving == "move":
        return Rect(x + d, y, w, h) if axis == "x" else Rect(x, y + d, w, h)
    if axis == "x":
        if "l" in moving:
            return Rect(x + d, y, w - d, h)
        if "r" in moving:
            return Rect(x, y, w + d, h)
        return None
    if "t" in moving:
        return Rect(x, y + d, w, h - d)
    if "b" in moving:
        return Rect(x, y, w, h + d)
    return None


def _clamp(rect: Rect, screen: tuple[int, int]) -> Rect:
    width, height = screen
    w = max(1, min(rect.width, width))
    h = max(1, min(rect.height, height))
    x = max(0, min(rect.x, width - w))
    y = max(0, min(rect.y, height - h))
    return Rect(x, y, w, h)


# -- encoding --

_INVERSE = {
    Image.Transpose.ROTATE_90: Image.Transpose.ROTATE_270,
    Image.Transpose.ROTATE_270: Image.Transpose.ROTATE_90,
    Image.Transpose.ROTATE_180: Image.Transpose.ROTATE_180,
}


def as_sent_image(
    image: Image.Image, orientation: Orientation, inverted: bool, encoding: Encoding
) -> tuple[Image.Image, int]:
    """`image` after the JPEG round trip the daemon's pushes go through,
    and the size of a full frame in bytes.

    Encoded in the panel's own buffer orientation, as the library does,
    so the 8x8 block grid falls where it will on the glass.
    """
    try:
        from akp02 import _TRANSPOSE  # the library's own table
        transpose = _TRANSPOSE.get((orientation, bool(inverted)))
    except ImportError:
        transpose = None
    work = image.transpose(transpose) if transpose is not None else image
    buffer = io.BytesIO()
    work.save(buffer, format="JPEG", quality=encoding.quality,
              subsampling=encoding.subsampling)
    size = buffer.tell()
    buffer.seek(0)
    with Image.open(buffer) as decoded:
        result = decoded.convert("RGB")
    if transpose is not None:
        result = result.transpose(_INVERSE[transpose])
    return result, size


__all__ = [
    "ERROR", "WARNING", "Encoding", "Engine", "Problem", "Slot",
    "as_sent_image", "layout_problems", "snap",
]
