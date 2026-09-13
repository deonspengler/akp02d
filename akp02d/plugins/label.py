"""label: one line of text, from a source or written out.

    [[widget.label]]
    x = 40; y = 26; width = 400; height = 60
    source = "time"
    format = "{value:%H:%M:%S}"
    size = 44

    [[widget.label]]
    source = "cpu"
    format = "CPU {percent:.0f}%"

    [[widget.label]]
    format = "Hello, Delia"        # no source: drawn literally

One format language rather than two: Python's format spec already
handles datetimes, so "{value:%H:%M}" and "{percent:.0f}%" are the same
mechanism and compose in one string. Without a source the format is not
substituted at all, which is what makes static text need no plugin of
its own.

Text is centred on its cap-height band -- baseline to the top of a
capital -- rather than on the font's line box. Line-box centring makes
the result depend on which glyphs a string happens to contain, so caps
look right while x-height text sits low and descenders drag it lower.
The cap band depends only on the font and size, so it is correct for
digits and stable as the text changes.
"""

from __future__ import annotations

import locale as locale_module
import time
from typing import TYPE_CHECKING

from PIL import ImageDraw

from .. import sources
from .base import PluginError, Widget, WidgetSpec, register

if TYPE_CHECKING:
    from ..theme import Theme

# setlocale is process-wide, so two widgets asking for different ones
# would silently fight. Remember who asked and refuse instead.
_LOCALE: tuple[str, str] | None = None


@register("label")
class LabelWidget(Widget):
    """Options: source, format, locale, size, color, font."""

    __slots__ = ("_anchor_xy", "_draw", "_fill", "_font", "_format", "_last",
                 "_source")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        self._source = sources.resolve(
            options, spec.id, (sources.SCALAR, sources.TEXT, sources.MOMENT)
        )
        self._format = options.string("format", "")
        _set_locale(options.string("locale", None), spec.id)
        self._fill = options.color("color", "#ffffff")
        # Resolved once: building a font per frame would cost more than
        # everything else this widget does put together.
        self._font = theme.font(
            options.string("font", None), options.integer("size", 24, 1, 512)
        )
        options.reject_unknown()

        width, height = spec.rect.size
        ascent, _ = self._font.getmetrics()
        cap = self._font.getbbox("H")
        baseline = (height + (cap[3] - cap[1])) // 2
        # "ma" anchors on the ascender line, which is `ascent` above the
        # baseline -- Pillow cannot anchor on the baseline directly.
        self._anchor_xy = (width // 2, baseline - ascent)
        self._draw = ImageDraw.Draw(self.surface)
        self._last: str | None = None
        # Format against a real sample now, so a format that does not fit
        # its source fails at load rather than on the first frame.
        self.text(time.monotonic())

    def text(self, now: float) -> str:
        if self._source is None:
            return self._format
        _, fields = self._source.read(now)
        try:
            return self._format.format(**fields)
        except (KeyError, ValueError, TypeError) as exc:
            raise PluginError(
                f"widget {self.id!r}: format {self._format!r} does not fit "
                f"this source ({exc}); it offers "
                f"{', '.join(sorted(fields))}"
            ) from exc

    def render(self, now: float) -> bool:
        text = self.text(now)
        if text == self._last:
            return False
        self._last = text
        self.clear()
        self._draw.text(
            self._anchor_xy, text, font=self._font, fill=self._fill, anchor="ma"
        )
        return True


def _set_locale(name: str | None, widget: str) -> None:
    """Apply LC_TIME once, and refuse a second, different request."""
    global _LOCALE
    if name is None:
        return
    if _LOCALE is not None and _LOCALE[1] != name:
        raise PluginError(
            f"widget {widget!r}: locale {name!r} conflicts with {_LOCALE[1]!r} "
            f"set by widget {_LOCALE[0]!r}; the locale is process-wide, so "
            f"every widget must agree"
        )
    try:
        locale_module.setlocale(locale_module.LC_TIME, name)
    except locale_module.Error as exc:
        raise PluginError(
            f"widget {widget!r}: locale {name!r} is not available ({exc}); "
            f"generate it or use one from `locale -a`"
        ) from exc
    _LOCALE = (widget, name)
