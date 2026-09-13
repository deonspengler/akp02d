"""The plugin contract.

A widget owns one non-overlapping box on the panel and one persistent
RGB surface of exactly that size. Each tick it is asked to render; it
returns True only if the pixels actually changed. Everything downstream
-- the JPEG encode, the USB transfer -- hangs off that boolean, so
answering it accurately is the single most important thing a plugin
does. Compare at the source (the formatted string, the reading, the
value) rather than comparing pixels: it is cheaper and it is exact.

Widgets never touch the device and never encode. They draw, and say
whether they drew anything new.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PIL import Image

from ..layout import Rect
from ..table import SettingsError, Table, parse_color

if TYPE_CHECKING:  # theme imports this module, so only for types
    from ..theme import Theme


class PluginError(SettingsError):
    """A widget is misconfigured, or its plugin is unknown."""


@dataclass(frozen=True, slots=True)
class WidgetSpec:
    """One [[widget.<plugin>]] entry, before instantiation.

    `options` is the same Table the geometry was read from, with the
    reserved keys already consumed, so a plugin reading its own keys and
    then calling reject_unknown() catches a typo in either.

    Widget itself reads `background` and `alpha`, so every plugin gets
    them without asking.
    """

    id: str
    plugin: str
    rect: Rect
    fps: float | None
    options: Table


class Widget:
    """Base class for plugins. Subclasses implement render()."""

    # Update rate when the theme does not give one.
    default_fps = 1.0

    __slots__ = ("backdrop", "due", "fps", "id", "jpeg", "period", "rect", "surface")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        self.id = spec.id
        self.rect = spec.rect
        self.fps = spec.fps if spec.fps is not None else self.default_fps
        # The loop schedules on a period; fps is what the theme writes.
        self.period = 1.0 / self.fps
        # The slice of the theme background under this box. Clearing is
        # a paste of it, so a picture background costs a widget nothing
        # extra at draw time.
        #
        # `background` at `alpha` opacity is laid over that slice, once,
        # at load. At alpha 1 it is simply the widget's background
        # colour; below that it dims whatever is behind, which is also
        # the cheapest way to shrink a busy widget's payload -- a region
        # push re-sends the whole rectangle every frame, and photographic
        # detail is what JPEG spends its bytes on. Either way it costs
        # nothing per frame, because the blend has already happened.
        self.backdrop = theme.backdrop(spec.rect)
        # Both keys are read unconditionally, so reject_unknown never
        # trips on one that happens not to be used. Naming a background
        # and no alpha means an opaque one -- the alternative is a
        # colour that silently does nothing.
        colour = spec.options.string("background", None)
        alpha = spec.options.number("alpha", 1.0 if colour else 0.0, 0.0, 1.0)
        if alpha:
            flat = Image.new(
                "RGB", self.backdrop.size, parse_color(colour or "#000000",
                                                       spec.options.path("background"))
            )
            self.backdrop = Image.blend(self.backdrop, flat, alpha)
        # The widget's own back buffer, allocated once and drawn over in
        # place. Its size is the region's, so it is also exactly what
        # gets encoded and pushed.
        self.surface: Image.Image = self.backdrop.copy()
        # Last encoded bytes, refreshed by the loop only when render()
        # reports a change.
        self.jpeg: bytes | None = None
        # Next monotonic time this widget is due; set by the loop.
        self.due = 0.0

    def clear(self) -> None:
        """Reset the surface to the background under this box."""
        self.surface.paste(self.backdrop, (0, 0))

    def render(self, now: float) -> bool:
        """Draw into self.surface. Return True only if pixels changed."""
        raise NotImplementedError

    def close(self) -> None:
        """Release anything the widget opened. Optional."""


# --check builds every widget to validate it, but a widget that starts a
# child process should not do so just to be inspected. Dry run means
# "check everything you can without side effects"; it deliberately does
# not promise the child would have worked, since a process that survives
# construction can still die a moment later.
_DRY_RUN = False


def set_dry_run(value: bool) -> None:
    global _DRY_RUN
    _DRY_RUN = value


def dry_run() -> bool:
    return _DRY_RUN


_REGISTRY: dict[str, type[Widget]] = {}


def register(name: str) -> Callable[[type[Widget]], type[Widget]]:
    """Class decorator adding a plugin to the registry under `name`."""

    def decorate(cls: type[Widget]) -> type[Widget]:
        if _REGISTRY.setdefault(name, cls) is not cls:
            raise PluginError(f"plugin {name!r} is already registered")
        return cls

    return decorate


def create(spec: WidgetSpec, theme: Theme) -> Widget:
    """Instantiate the plugin a WidgetSpec names."""
    cls = _REGISTRY.get(spec.plugin)
    if cls is None:
        raise PluginError(
            f"unknown plugin {spec.plugin!r} (in [[widget.{spec.plugin}]]); "
            f"available: {', '.join(sorted(_REGISTRY)) or 'none'}"
        )
    return cls(spec, theme)


def available() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
