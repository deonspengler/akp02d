"""The preview: the daemon's own widgets, drawing into a picture.

No Qt here. The engine builds a Theme from the document's text as the
daemon would from the file, constructs every widget and keeps the same
panel-sized composite. It differs where a half-made theme needs it to:
a bad widget is reported and left blank while the rest draw; a bad
[theme] keeps the last good background; a setting that only fails on
this machine (a locale, an interface) is previewed without it as a
warning; and only widgets whose entry changed are rebuilt, so a graph
keeps its history while a label is edited.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from akp02 import Orientation

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
MACHINE_KEYS = {"locale", "interface", "path", "sensor", "sensor_label"}


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


class Engine:
    """Builds, ticks and composites a theme's widgets, no panel needed."""

    LOOP_FPS = 30.0

    def __init__(self, *, inverted: bool = False) -> None:
        self.inverted = inverted
        self.theme: theme_module.Theme | None = None
        self.theme_error: str | None = None
        self.slots: list[Slot] = []
        self.screen: Image.Image | None = None
        self._theme_key: tuple | None = None
        self._locales: tuple = ()
        # Simulated readings (the default) or this machine's real ones.
        self.live = False

    def set_live(self, live: bool) -> None:
        """Switch between simulated and real readings. Every widget is
        rebuilt on the next load, since its source is made at build."""
        if live != self.live:
            self.live = live
            for slot in self.slots:
                slot.key = None

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
        locales = tuple(
            entry.get("locale") for ref, entry in entries.items() if ref.plugin == "label"
        )
        locales_changed = locales != self._locales
        if locales_changed:
            reset_locale()
            self._locales = locales

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
        if self.live:
            error = self._try_build_as(slot, entry, simulated=False)
            if error is None:
                return None
            # Something this machine lacks (a sensor, cava, playerctl):
            # shown with simulated readings, and said so.
            if self._try_build_as(slot, entry, simulated=True) is None:
                slot.warning = f"{error} -- shown with simulated readings"
                return None
            return error
        return self._try_build_as(slot, entry, simulated=True)

    def _try_build_as(self, slot: Slot, entry: dict[str, Any], *, simulated: bool) -> str | None:
        assert self.theme is not None
        index = slot.ref.index - 1
        table = Table(entry, f"widget.{slot.plugin}[{index}]")
        previous_wrap = sources.wrap
        sources.wrap = simulate.StandIn if simulated else None
        plugin_base.set_preview(simulated)
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
            widget._stream = simulate.Audio(widget._bars, channels == "stereo",
                                            entry.get("reverse") is True)
        widget.period = max(widget.period, 1.0 / self.LOOP_FPS)
        slot.widget = widget
        if simulated:
            # Real sources only know now: a live graph fills as it goes.
            self._seed(widget)
        slot.due = 0.0
        slot.error = None
        return None

    def _seed(self, widget: Widget) -> None:
        """Give a history-keeping widget its past at once: the data is
        made up, so any earlier moment can be read."""
        seed = getattr(widget, "seed", None)
        if seed is None:
            return
        try:
            seed(time.monotonic(), widget.period)
        except Exception:  # noqa: BLE001 -- a preview nicety, never fatal
            log.debug("seeding %r failed", widget, exc_info=True)

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

    def frame(self) -> Image.Image:
        """The panel as it stands."""
        assert self.screen is not None
        return self.screen.copy()

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
            real = getattr(getattr(slot.widget, "_source", None), "real", None)
            missing = getattr(real, "unavailable", None)
            if missing:
                out.append(Problem(
                    WARNING, f"{missing} -- previewed with simulated readings; it "
                    f"may well exist on the machine the panel is on", slot.id))
            if (slot.plugin == "spectrum" and slot.widget is not None and not self.live
                    and not cava.installed()):
                out.append(Problem(
                    WARNING,
                    "cava is not installed on this machine; the daemon needs it "
                    "for a spectrum (the preview uses made-up music either way)",
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
    for index, (name, rect) in enumerate(rects):
        for other_name, other in rects[index + 1:]:
            if rect.overlaps(other):
                out.append(Problem(ERROR, f"overlaps {other_name!r}", name))
    return out


# -- placing boxes: snapping --

# The grid boxes snap to. A whole JPEG block of the panel's buffer, so a
# box on it is also a box the daemon pushes without widening.
GRID = 8
# How close, in panel pixels, an edge has to come to snap to something.
REACH = 6.0


@dataclass(frozen=True, slots=True)
class Guide:
    """A line something lined up on: `axis` "x" is a vertical line at x =
    `at`, drawn from `start` to `end` down the panel; "y" is horizontal."""

    axis: str
    at: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Gap:
    """The space between the box and a neighbour, along `axis`, from
    `start` to `end`, drawn across at `across`. `equal` when it matches
    the gap on the other side."""

    axis: str
    start: int
    end: int
    across: int
    equal: bool = False


@dataclass(frozen=True, slots=True)
class Placement:
    rect: Rect
    guides: tuple[Guide, ...] = ()
    gaps: tuple[Gap, ...] = ()


def place(raw: Rect, mode: str, others: list[Rect], screen: tuple[int, int], *,
          grid: int | None = GRID, reach: float = REACH, snapping: bool = True) -> Placement:
    """Where a box being moved or resized lands, and what it lined up on.

    `mode` is "move", or the edges being dragged as a string of l/t/r/b.
    Each axis is snapped on its own, and only what is being dragged moves:
    a whole box, or just the edges in `mode`. Within `reach`, an edge or
    centre snaps to another box's edges and centres, or the panel's edges
    and centre lines; a moved box also snaps to equal gaps between its two
    neighbours. Failing those, the dragged edges go to the `grid`. With
    `snapping` off, the box goes where it was put, kept on the panel.
    """
    rect = _fit(raw, mode, screen)
    if not snapping:
        return Placement(rect, (), _gaps(rect, others))
    for axis in ("x", "y"):
        edges = _edges_on(axis, mode)
        if not edges:
            continue
        shift = _best_shift(rect, axis, edges, others, screen, reach, mode == "move")
        if shift is None and grid:
            shift = _grid_shift(rect, axis, edges, grid)
        if shift:
            rect = _fit(_shifted(rect, axis, edges, shift), mode, screen)
    return Placement(rect, _guides(rect, mode, others, screen), _gaps(rect, others))


def keep_ratio(start: Rect, raw: Rect, mode: str) -> tuple[Rect, str]:
    """`raw` reshaped to `start`'s proportions, and the edges that lead.

    A corner follows whichever dimension moved more; a side drags the
    other dimension along, centred, so the box grows evenly. The leading
    edges are the ones worth snapping: the rest follow from them.
    """
    if mode == "move" or start.width < 1 or start.height < 1:
        return raw, mode
    ratio = start.width / start.height
    horizontal = "".join(e for e in mode if e in "lr")
    vertical = "".join(e for e in mode if e in "tb")
    if horizontal and vertical:
        by_width = abs(raw.width / start.width - 1) >= abs(raw.height / start.height - 1)
        lead = horizontal if by_width else vertical
    else:
        lead = horizontal or vertical
    return _with_ratio(raw, mode, lead, ratio), lead


def follow_ratio(start: Rect, rect: Rect, mode: str, lead: str) -> Rect:
    """After the leading edges snapped, bring the other dimension along."""
    if mode == "move" or start.height < 1:
        return rect
    return _with_ratio(rect, mode, lead, start.width / start.height)


def _with_ratio(rect: Rect, mode: str, lead: str, ratio: float) -> Rect:
    x, y, w, h = rect
    if lead in ("l", "r"):
        new_h = max(1, round(w / ratio))
        if "t" in mode:
            y += h - new_h
        elif "b" not in mode:
            y += (h - new_h) // 2
        h = new_h
    else:
        new_w = max(1, round(h * ratio))
        if "l" in mode:
            x += w - new_w
        elif "r" not in mode:
            x += (w - new_w) // 2
        w = new_w
    return Rect(x, y, w, h)


def _edges_on(axis: str, mode: str) -> str:
    """What moves along `axis`: "lr"/"tb" for a whole box, else the
    dragged edges on that axis."""
    if mode == "move":
        return "lr" if axis == "x" else "tb"
    return "".join(e for e in mode if e in ("lr" if axis == "x" else "tb"))


def _span(rect: Rect, axis: str) -> tuple[int, int]:
    return (rect.x, rect.x + rect.width) if axis == "x" else (rect.y, rect.y + rect.height)


def _features(rect: Rect, axis: str, edges: str) -> list[float]:
    """The positions that can line up: both edges and the centre of a
    moving box, or the one edge being dragged."""
    low, high = _span(rect, axis)
    if len(edges) == 2:
        return [low, (low + high) / 2, high]
    return [low if edges in "lt" else high]


def _targets(axis: str, others: list[Rect], screen: tuple[int, int]) -> list[float]:
    size = screen[0] if axis == "x" else screen[1]
    out = [0.0, size / 2, float(size)]
    for other in others:
        low, high = _span(other, axis)
        out += [low, (low + high) / 2, high]
    return out


def _across(rect: Rect, other: Rect, axis: str) -> bool:
    """Whether two boxes share some of the other axis: neighbours in a
    row (for x) or a column (for y)."""
    a0, a1 = _span(rect, "y" if axis == "x" else "x")
    b0, b1 = _span(other, "y" if axis == "x" else "x")
    return a0 < b1 and b0 < a1


def _neighbours(rect: Rect, axis: str, others: list[Rect]) -> tuple[Rect | None, Rect | None]:
    """The nearest box before and after `rect` along `axis`, in its row."""
    low, high = _span(rect, axis)
    before = [o for o in others if _across(rect, o, axis) and _span(o, axis)[1] <= low]
    after = [o for o in others if _across(rect, o, axis) and _span(o, axis)[0] >= high]
    first = max(before, key=lambda o: _span(o, axis)[1], default=None)
    last = min(after, key=lambda o: _span(o, axis)[0], default=None)
    return first, last


def _best_shift(rect: Rect, axis: str, edges: str, others: list[Rect],
                screen: tuple[int, int], reach: float, moving: bool) -> int | None:
    best: float | None = None
    targets = _targets(axis, others, screen)
    for feature in _features(rect, axis, edges):
        for target in targets:
            shift = target - feature
            if abs(shift) <= reach and (best is None or abs(shift) < abs(best)):
                best = shift
    if moving:
        # Even spacing: centred in the room between the two neighbours.
        # Searched with the box where it is now, so the neighbours are
        # the ones it sits between.
        first, last = _neighbours(rect, axis, others)
        if first is not None and last is not None:
            low, high = _span(rect, axis)
            room = _span(last, axis)[0] - _span(first, axis)[1]
            want = _span(first, axis)[1] + (room - (high - low)) / 2
            shift = want - low
            if abs(shift) <= reach and (best is None or abs(shift) < abs(best)):
                best = shift
    return None if best is None else round(best)


def _grid_shift(rect: Rect, axis: str, edges: str, grid: int) -> int:
    """To the nearest grid line: the leading edge of a moved box, or the
    dragged edge."""
    low, high = _span(rect, axis)
    edge = high if edges in ("r", "b") else low
    return round(edge / grid) * grid - edge


def _shifted(rect: Rect, axis: str, edges: str, shift: int) -> Rect:
    x, y, w, h = rect
    if axis == "x":
        if len(edges) == 2:
            return Rect(x + shift, y, w, h)
        return Rect(x + shift, y, w - shift, h) if edges == "l" else Rect(x, y, w + shift, h)
    if len(edges) == 2:
        return Rect(x, y + shift, w, h)
    return Rect(x, y + shift, w, h - shift) if edges == "t" else Rect(x, y, w, h + shift)


def _fit(rect: Rect, mode: str, screen: tuple[int, int]) -> Rect:
    """On the panel: a moved box slides back on whole; a resized one has
    its dragged edges stopped at the panel's edge, at least 1 px wide."""
    if mode == "move":
        return _clamp(rect, screen)
    width, height = screen
    x0, y0 = rect.x, rect.y
    x1, y1 = rect.x + rect.width, rect.y + rect.height
    if "l" in mode:
        x0 = min(max(x0, 0), x1 - 1)
    if "r" in mode:
        x1 = max(min(x1, width), x0 + 1)
    if "t" in mode:
        y0 = min(max(y0, 0), y1 - 1)
    if "b" in mode:
        y1 = max(min(y1, height), y0 + 1)
    return _clamp(Rect(x0, y0, x1 - x0, y1 - y0), screen)


def _guides(rect: Rect, mode: str, others: list[Rect],
            screen: tuple[int, int]) -> tuple[Guide, ...]:
    """Every line the box's moving features now sit exactly on."""
    out: list[Guide] = []
    for axis in ("x", "y"):
        edges = _edges_on(axis, mode)
        if not edges:
            continue
        cross = "y" if axis == "x" else "x"
        size = screen[1] if axis == "x" else screen[0]
        mine = _span(rect, cross)
        for feature in _features(rect, axis, edges):
            if feature != int(feature):
                continue
            at = int(feature)
            # The panel's own edges are its outline already; its centre
            # line is worth showing, the full length.
            middle = (screen[0] if axis == "x" else screen[1]) / 2
            if at == middle:
                out.append(Guide(axis, at, 0, size))
            for other in others:
                low, high = _span(other, axis)
                if at in (low, high, (low + high) / 2):
                    theirs = _span(other, cross)
                    out.append(Guide(axis, at, min(mine[0], theirs[0]), max(mine[1], theirs[1])))
    return tuple(dict.fromkeys(out))


def _gaps(rect: Rect, others: list[Rect]) -> tuple[Gap, ...]:
    """The space to the nearest neighbour on each side, in its row and
    column, marked equal where the two sides match."""
    out: list[Gap] = []
    for axis in ("x", "y"):
        first, last = _neighbours(rect, axis, others)
        low, high = _span(rect, axis)
        found = []
        for other, start, end in ((first, None, low), (last, high, None)):
            if other is None:
                continue
            a, b = _span(other, axis)
            span = (b, end) if start is None else (start, a)
            o0, o1 = _span(other, "y" if axis == "x" else "x")
            m0, m1 = _span(rect, "y" if axis == "x" else "x")
            across = (max(o0, m0) + min(o1, m1)) // 2
            found.append((span, across))
        equal = len(found) == 2 and (found[0][0][1] - found[0][0][0]
                                     == found[1][0][1] - found[1][0][0])
        out += [Gap(axis, s, e, across, equal) for (s, e), across in found if e > s]
    return tuple(out)


def _clamp(rect: Rect, screen: tuple[int, int]) -> Rect:
    width, height = screen
    w = max(1, min(rect.width, width))
    h = max(1, min(rect.height, height))
    x = max(0, min(rect.x, width - w))
    y = max(0, min(rect.y, height - h))
    return Rect(x, y, w, h)


__all__ = [
    "ERROR", "GRID", "WARNING", "Engine", "Gap", "Guide", "Placement", "Problem", "Slot",
    "follow_ratio", "keep_ratio", "layout_problems", "place",
]
