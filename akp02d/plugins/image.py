"""image: a picture from the theme's folder, still or animated.

    [[widget.image]]
    x = 16
    y = 40
    width = 160
    height = 160
    path = "logo.png"
    fit = "contain"          # cover | stretch | none
    align = "center"         # top, bottom-left, ...
    radius = 12              # rounded corners
    opacity = 1.0

`path` is relative to the theme's folder, like the wallpaper, so a theme
carries its pictures with it. Transparency is honoured: a PNG with an
alpha channel sits over the wallpaper, not in a box.

Or the picture comes from a source -- `source = "media"` shows the cover
of what is playing, whether the player hands it over as a file, a
download, or inline base64 (see artwork.py). `path` is then the picture
shown while there is no cover; without one, the background shows. A
new cover that is still downloading leaves the old one up until it
arrives, rather than flashing the fallback between tracks.

How the picture meets the box:

* contain -- all of it, as large as fits; the spare room shows the
  background, and `align` says which side the picture keeps to.
* cover -- fills the box, cropping what does not fit; `align` says which
  part is kept.
* stretch -- fills the box exactly, ignoring the picture's shape.
* none -- drawn at its own size, placed by `align`, cropped by the box.

`scaling = "pixel"` scales by repeating pixels rather than smoothing
them, which keeps pixel art and small icons crisp.

An animated GIF, WebP or PNG plays at its own frame timing, looping. Its
frames are fitted and laid over the background once, at load, so playing
is a paste per frame -- and a still picture is drawn once and costs
nothing after. Frames take memory at the box's size; a long animation
in a big box keeps an even spread of them within MAX_BYTES, and says so.
"""

from __future__ import annotations

import bisect
import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageDraw, ImageSequence, UnidentifiedImageError

from .. import artwork, sources
from ..schema import Schema, boolean, choice, image, integer, number, source
from .base import PluginError, Widget, WidgetSpec, register

if TYPE_CHECKING:
    from ..theme import Theme

log = logging.getLogger(__name__)

ALIGNS = {
    "center": (0.5, 0.5), "top": (0.5, 0.0), "bottom": (0.5, 1.0),
    "left": (0.0, 0.5), "right": (1.0, 0.5),
    "top-left": (0.0, 0.0), "top-right": (1.0, 0.0),
    "bottom-left": (0.0, 1.0), "bottom-right": (1.0, 1.0),
}
# What a theme's frames may hold in memory, all frames together.
MAX_BYTES = 96 * 2**20
# Browsers treat a frame delay under 20 ms as 100 ms; so do we, or a GIF
# saved with "0" would try to play at the loop's full rate.
MIN_DELAY_MS = 20
DEFAULT_DELAY_MS = 100
# The fastest an animation asks to be visited when the theme gives no fps.
MAX_AUTO_FPS = 30.0


@lru_cache(maxsize=8)
def _decode(file: str, mtime_ns: int, size: int) -> tuple[tuple[Image.Image, ...], tuple[int, ...]]:
    """Every frame as RGBA, with its delay in ms. Cached by file state,
    so a reload that did not touch the picture does not decode it again.
    Callers must not draw on the frames."""
    with Image.open(file) as picture:
        frames, delays = [], []
        for frame in ImageSequence.Iterator(picture):
            frames.append(frame.convert("RGBA"))
            delay = frame.info.get("duration", DEFAULT_DELAY_MS) or DEFAULT_DELAY_MS
            delays.append(int(delay) if delay >= MIN_DELAY_MS else DEFAULT_DELAY_MS)
    return tuple(frames), tuple(delays)


def load_frames(file: Path) -> tuple[tuple[Image.Image, ...], tuple[int, ...]]:
    stat = file.stat()
    return _decode(str(file), stat.st_mtime_ns, stat.st_size)


def placement(source: tuple[int, int], box: tuple[int, int], fit: str,
              align: tuple[float, float]) -> tuple[int, int, int, int]:
    """Where the picture goes in the box: x, y, width, height. x and y
    may be negative, or the size larger than the box: that part is
    cropped."""
    sw, sh = source
    bw, bh = box
    if fit == "stretch":
        return 0, 0, bw, bh
    if fit == "none":
        scale = 1.0
    elif fit == "cover":
        scale = max(bw / sw, bh / sh)
    else:
        scale = min(bw / sw, bh / sh)
    width, height = max(1, round(sw * scale)), max(1, round(sh * scale))
    ax, ay = align
    return round((bw - width) * ax), round((bh - height) * ay), width, height


@register("image")
class ImageWidget(Widget):
    """A picture: a file, still or animated, or a source's (cover art)."""

    default_fps = 1.0
    summary = "A picture: a file in the theme's folder, or cover art from media"
    schema = Schema(
        source(accepts=(sources.PICTURE,), group="Image",
               doc="Where a changing picture comes from: media for the cover "
                   "of what is playing. Empty shows `path`"),
        image("path", None, group="Image",
              doc="An image file in the theme's folder (PNG, JPEG, WebP, "
                  "GIF, ...); animated ones play. With a source, shown "
                  "whenever it has no picture"),
        choice("fit", ("contain", "cover", "stretch", "none"), "contain", group="Image",
               doc="contain: all of it, as large as fits; cover: fill the box, "
                   "cropping; stretch: fill exactly, ignoring its shape; "
                   "none: its own size"),
        choice("align", tuple(ALIGNS), "center", group="Image",
               when=lambda v: v.get("fit") != "stretch",
               doc="Which side the picture keeps to, or which part cover "
                   "and none keep"),
        choice("scaling", ("smooth", "pixel"), "smooth", group="Image",
               doc="pixel keeps pixel art and small icons crisp"),
        number("opacity", 1.0, 0.0, 1.0, group="Image"),
        integer("radius", 0, 0, 1024, group="Image",
                doc="Corner rounding of the picture, in pixels"),
        boolean("animate", True, group="Image",
                when=lambda v: bool(v.get("path")),
                doc="Play an animated file; off shows its first frame"),
    )

    __slots__ = ("_art", "_ends", "_fit", "_frames", "_last", "_opacity", "_radius",
                 "_resample", "_source", "_start", "_url", "fps_auto")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        o = self.schema.read(options, spec)
        options.reject_unknown()
        self._source = o.source
        if self._source is None and not o.path:
            raise PluginError(f"widget {self.id!r}: needs a picture: set path to an "
                              "image file, or source to where pictures come from "
                              "(media for cover art)")
        self._fit = (o.fit, ALIGNS[o.align])
        self._radius = o.radius
        self._opacity = o.opacity
        self._resample = Image.Resampling.NEAREST if o.scaling == "pixel" \
            else Image.Resampling.LANCZOS
        # The source's picture now: its URL, and that picture composed, once
        # it has arrived.
        self._url = ""
        self._art: Image.Image | None = None

        frames: tuple[Image.Image, ...] = ()
        delays: tuple[int, ...] = ()
        if o.path:
            file = theme.path / Path(o.path).expanduser()
            if not file.is_file():
                raise PluginError(f"widget {self.id!r}: path: no file {o.path!r} "
                                  f"in {theme.path}")
            try:
                frames, delays = load_frames(file)
            except (OSError, UnidentifiedImageError) as exc:
                raise PluginError(f"widget {self.id!r}: path: cannot read "
                                  f"{o.path!r}: {exc}") from exc
            if not o.animate:
                frames, delays = frames[:1], delays[:1]
            box = spec.rect.size
            keep = max(1, MAX_BYTES // (box[0] * box[1] * 3))
            if len(frames) > keep:
                log.warning("%s: %s has %d frames; keeping %d of them to stay within "
                            "%d MiB -- a smaller box keeps more", self.id, o.path,
                            len(frames), keep, MAX_BYTES // 2**20)
                frames, delays = _thin(frames, delays, keep)
        # The file's frames, fitted and laid over the background; with no
        # path, the background alone.
        self._frames = tuple(self._compose(f) for f in frames) or (self.backdrop.copy(),)
        # When each frame ends, from the start of a loop, in seconds.
        total, ends = 0, []
        for delay in delays or (1000,):
            total += delay
            ends.append(total / 1000)
        self._ends = tuple(ends)
        self._start: float | None = None
        self._last: object = None
        self.fps_auto = False
        if len(self._frames) > 1 and spec.fps is None:
            # Visit as often as the quickest frame needs, and no more.
            self.fps = min(MAX_AUTO_FPS, 1000 / min(delays))
            self.period = 1.0 / self.fps
            self.fps_auto = True

    def _compose(self, picture: Image.Image) -> Image.Image:
        """A picture fitted into the box and laid over the background."""
        box = self.backdrop.size
        fit, align = self._fit
        x, y, width, height = place = placement(picture.size, box, fit, align)
        if picture.size != (width, height):
            picture = picture.resize((width, height), self._resample)
        layer = Image.new("RGBA", box, (0, 0, 0, 0))
        layer.paste(picture, (x, y))
        alpha = layer.getchannel("A")
        mask = _corners(box, place, self._radius)
        if mask is not None:
            alpha = ImageChops.multiply(alpha, mask)
        if self._opacity < 1.0:
            opacity = self._opacity
            alpha = alpha.point(lambda a: round(a * opacity))
        out = self.backdrop.copy()
        out.paste(layer.convert("RGB"), (0, 0), alpha)
        return out

    def render(self, now: float) -> bool:
        if self._start is None:
            self._start = now
        if self._source is not None and self._source_picture(now):
            if self._last == self._url:
                return False
            self._last = self._url
            assert self._art is not None
            self.surface.paste(self._art, (0, 0))
            return True
        index = 0
        if len(self._frames) > 1:
            at = (now - self._start) % self._ends[-1]
            index = min(bisect.bisect_right(self._ends, at), len(self._frames) - 1)
        if index == self._last:
            return False
        self._last = index
        self.surface.paste(self._frames[index], (0, 0))
        return True

    def _source_picture(self, now: float) -> bool:
        """Whether the source has a picture to show (now in self._art).

        While a new cover is still downloading, the old one stays up --
        a flash of the fallback between tracks would only be noise.
        """
        _, fields = self._source.read(now)
        url = fields.get("art") or ""
        if url != self._url:
            picture = artwork.picture(url)
            if picture is artwork.PENDING:
                return self._art is not None
            self._url = url
            self._art = self._compose(picture) if picture is not None else None
        elif self._art is None and url:
            # Arrived since: a download that was pending.
            picture = artwork.picture(url)
            if isinstance(picture, Image.Image):
                self._art = self._compose(picture)
        return self._art is not None


def _thin(frames, delays, keep: int):
    """`keep` frames spread evenly, each lasting until the next kept one,
    so the animation keeps its length and pace."""
    picked = [round(i * len(frames) / keep) for i in range(keep)]
    out_frames, out_delays = [], []
    for n, first in enumerate(picked):
        last = picked[n + 1] if n + 1 < len(picked) else len(frames)
        out_frames.append(frames[first])
        out_delays.append(sum(delays[first:last]))
    return tuple(out_frames), tuple(out_delays)


def _corners(box: tuple[int, int], place, radius: int) -> Image.Image | None:
    """A mask rounding the corners of the picture's visible part."""
    if radius <= 0:
        return None
    x, y, width, height = place
    left, top = max(0, x), max(0, y)
    right, bottom = min(box[0], x + width), min(box[1], y + height)
    if right <= left or bottom <= top:
        return None
    radius = min(radius, (right - left) // 2, (bottom - top) // 2)
    # Drawn at 4x and scaled down, so the curves are smooth.
    s = 4
    mask = Image.new("L", (box[0] * s, box[1] * s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (left * s, top * s, right * s - 1, bottom * s - 1), radius * s, fill=255)
    return mask.resize(box, Image.Resampling.LANCZOS)
