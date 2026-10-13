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

from ..schema import Schema, boolean, choice, image, integer, number
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
    """A picture: still, or animated at its own frame rate."""

    default_fps = 1.0
    summary = "A picture from the theme's folder; animated GIFs play"
    schema = Schema(
        image("path", group="Image",
              doc="An image file in the theme's folder (PNG, JPEG, WebP, "
                  "GIF, ...); animated ones play"),
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
                doc="Play an animated file; off shows its first frame"),
    )

    __slots__ = ("_ends", "_frames", "_last", "_start", "fps_auto")

    def __init__(self, spec: WidgetSpec, theme: Theme) -> None:
        super().__init__(spec, theme)
        options = spec.options
        o = self.schema.read(options, spec)
        options.reject_unknown()

        file = (theme.path / Path(o.path).expanduser())
        if not file.is_file():
            raise PluginError(f"widget {self.id!r}: path: no file {o.path!r} in {theme.path}")
        try:
            frames, delays = load_frames(file)
        except (OSError, UnidentifiedImageError) as exc:
            raise PluginError(f"widget {self.id!r}: path: cannot read {o.path!r}: {exc}") from exc
        if not o.animate:
            frames, delays = frames[:1], delays[:1]

        box = spec.rect.size
        keep = max(1, MAX_BYTES // (box[0] * box[1] * 3))
        if len(frames) > keep:
            log.warning("%s: %s has %d frames; keeping %d of them to stay within "
                        "%d MiB -- a smaller box keeps more", self.id, o.path,
                        len(frames), keep, MAX_BYTES // 2**20)
            frames, delays = _thin(frames, delays, keep)

        place = placement(frames[0].size, box, o.fit, ALIGNS[o.align])
        mask = _corners(box, place, o.radius)
        resample = Image.Resampling.NEAREST if o.scaling == "pixel" \
            else Image.Resampling.LANCZOS
        self._frames = tuple(self._compose(f, place, mask, o.opacity, resample)
                             for f in frames)
        # When each frame ends, from the start of a loop, in seconds.
        total, ends = 0, []
        for delay in delays:
            total += delay
            ends.append(total / 1000)
        self._ends = tuple(ends)
        self._start: float | None = None
        self._last = -1
        self.fps_auto = False
        if len(self._frames) > 1 and spec.fps is None:
            # Visit as often as the quickest frame needs, and no more.
            self.fps = min(MAX_AUTO_FPS, 1000 / min(delays))
            self.period = 1.0 / self.fps
            self.fps_auto = True

    def _compose(self, frame: Image.Image, place, mask, opacity: float,
                 resample) -> Image.Image:
        """One frame fitted into the box and laid over the background."""
        x, y, width, height = place
        if frame.size != (width, height):
            frame = frame.resize((width, height), resample)
        layer = Image.new("RGBA", self.backdrop.size, (0, 0, 0, 0))
        layer.paste(frame, (x, y))
        alpha = layer.getchannel("A")
        if mask is not None:
            alpha = ImageChops.multiply(alpha, mask)
        if opacity < 1.0:
            alpha = alpha.point(lambda a: round(a * opacity))
        out = self.backdrop.copy()
        out.paste(layer.convert("RGB"), (0, 0), alpha)
        return out

    def render(self, now: float) -> bool:
        if self._start is None:
            self._start = now
        index = 0
        if len(self._frames) > 1:
            at = (now - self._start) % self._ends[-1]
            index = min(bisect.bisect_right(self._ends, at), len(self._frames) - 1)
        if index == self._last:
            return False
        self._last = index
        self.surface.paste(self._frames[index], (0, 0))
        return True


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
