"""Themes: a directory with a theme.toml holding orientation, a
background, a default font, and the widgets.

    [theme]
    orientation = "landscape"
    background = "#000000"        # a colour
    wallpaper = "panel.jpg"       # optional image, drawn over it
    font = "DejaVuSans-Bold.ttf"

    [[widget.label]]
    x = 40; y = 24; width = 600; height = 60
    format = "Hello, world"
    size = 44

The plugin name is the array header, so a widget's own keys sit flat
beside its geometry. x, y, width, height, interval, and id are reserved;
everything else is the plugin's.

Colours are whatever Pillow's ImageColor accepts -- "#rrggbb", "#rgb",
or a CSS name like "white". Background and wallpaper are resolved to one
image of the panel's exact size at load, and each widget keeps the crop
of it under its own box, so clearing is a paste rather than a special
case for pictures.

Fonts are loaded once, here. Building a FreeTypeFont costs far more
than drawing with one, so no widget is ever allowed to do it per frame.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from akp02 import Orientation
from PIL import Image, ImageFont

from .layout import Rect, screen_size
from .plugins.base import WidgetSpec
from .table import REQUIRED, SettingsError, Table, parse_color

log = logging.getLogger(__name__)

ORIENTATIONS = {"landscape": Orientation.LANDSCAPE, "portrait": Orientation.PORTRAIT}


class ThemeError(SettingsError):
    """A theme is missing, malformed, or refers to something absent."""


@dataclass(slots=True)
class Theme:
    """A loaded theme: everything drawing needs, already resolved."""

    name: str
    path: Path
    orientation: Orientation
    background: Image.Image
    font_default: str | None
    widgets: tuple[WidgetSpec, ...]
    _fonts: dict[tuple[str | None, int], ImageFont.FreeTypeFont] = field(
        default_factory=dict
    )

    def font(self, name: str | None, size: int) -> ImageFont.FreeTypeFont:
        """The theme's default font, or `name`, at `size`. Cached."""
        name = name or self.font_default
        key = (name, size)
        cached = self._fonts.get(key)
        if cached is not None:
            return cached
        self._fonts[key] = font = self._load(name, size)
        return font

    def _load(self, name: str | None, size: int) -> ImageFont.FreeTypeFont:
        if name is None:
            return ImageFont.load_default(size)
        # A theme-relative file wins over a system family of the same
        # name, so a theme that ships its fonts is self-contained.
        candidates = [str(self.path / name), name]
        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        log.warning(
            "font %r not found (tried %s); using Pillow's built-in font",
            name,
            " then ".join(candidates),
        )
        return ImageFont.load_default(size)

    def backdrop(self, rect: Rect) -> Image.Image:
        """The slice of the background under `rect`, for clearing."""
        return self.background.crop(
            (rect.x, rect.y, rect.x + rect.width, rect.y + rect.height)
        ).copy()


def find(name: str, search_paths: tuple[Path, ...]) -> Path:
    """Locate a theme directory by name, first match wins.

    A name containing a path separator is taken as the directory
    itself, which is handy for developing one in place.
    """
    if "/" in name or name.startswith("."):
        path = Path(name).expanduser()
        if (path / "theme.toml").is_file():
            return path
        raise ThemeError(f"no theme.toml in {path}")
    for base in search_paths:
        candidate = base.expanduser() / name
        if (candidate / "theme.toml").is_file():
            return candidate
    raise ThemeError(
        f"theme {name!r} not found; looked in: "
        f"{', '.join(str(p) for p in search_paths)}"
    )


def load(path: Path) -> Theme:
    """Parse a theme directory into a Theme."""
    file = path / "theme.toml"
    try:
        top = Table(tomllib.loads(file.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ThemeError(f"{file}: {exc}") from exc
    try:
        return _build(top, path)
    except SettingsError as exc:
        # Table messages carry the key path; the file name goes here, so
        # a theme error reads "<file>: widget.label[0].color: ...".
        raise ThemeError(f"{file}: {exc}") from exc


def _build(top: Table, path: Path) -> Theme:
    meta = top.table("theme")
    orientation = meta.choice("orientation", "landscape", ORIENTATIONS)
    screen = screen_size(orientation)
    background = _background(
        meta.string("background", "#000000"),
        meta.string("wallpaper", None),
        path,
        screen,
    )
    font_default = meta.string("font", None)
    meta.reject_unknown()

    widgets = _widgets(top.table("widget"))
    top.reject_unknown()
    return Theme(
        name=path.name,
        path=path,
        orientation=orientation,
        background=background,
        font_default=font_default,
        widgets=widgets,
    )


def _background(
    colour: str, wallpaper: str | None, path: Path, screen: tuple[int, int]
) -> Image.Image:
    """The panel-sized image everything is drawn over.

    A colour, optionally covered by a wallpaper file. Resolved to the
    full panel size once, so the startup full-screen draw and every
    widget's backdrop crop come from the same pixels.

    Two keys rather than one because they fail differently: a bad colour
    is a bad colour and a missing file is a missing file, where a single
    key would have to guess which was meant. Anything richer than a flat
    colour is a wallpaper -- an image says exactly what it is, where a
    generated one needs a vocabulary of its own to describe.
    """
    base = Image.new("RGB", screen, parse_color(colour, "theme.background"))
    if wallpaper is None:
        return base
    file = path / wallpaper
    if not file.is_file():
        raise ThemeError(f"theme.wallpaper: no file {wallpaper!r} in {path}")
    try:
        with Image.open(file) as image:
            source = image.convert("RGB")
    except OSError as exc:
        raise ThemeError(f"theme.wallpaper: cannot read {file}: {exc}") from exc
    if source.size != screen:
        source = source.resize(screen, Image.Resampling.LANCZOS)
    return source


def _widgets(table: Table) -> tuple[WidgetSpec, ...]:
    """Every [[widget.<plugin>]] entry, in file order."""
    specs: list[WidgetSpec] = []
    seen: set[str] = set()
    for plugin in table.names():
        for index, entry in enumerate(table.tables(plugin), start=1):
            widget_id = entry.string("id", None) or f"{plugin}#{index}"
            if widget_id in seen:
                raise ThemeError(f"duplicate widget id {widget_id!r}")
            seen.add(widget_id)
            rect = Rect(
                entry.integer("x", REQUIRED, 0, 4096),
                entry.integer("y", REQUIRED, 0, 4096),
                entry.integer("width", REQUIRED, 1, 4096),
                entry.integer("height", REQUIRED, 1, 4096),
            )
            # fps rather than a period: "fps = 1" is a plainer way to
            # say once a second, and "fps = 30" says every frame without
            # needing a zero to mean something special.
            fps = (
                entry.number("fps", REQUIRED, 0.001, 240.0)
                if "fps" in entry
                else None
            )
            specs.append(
                WidgetSpec(
                    id=widget_id,
                    plugin=plugin,
                    rect=rect,
                    fps=fps,
                    options=entry,
                )
            )
    if not specs:
        raise ThemeError("no [[widget.*]] entries; nothing to draw")
    return tuple(specs)
