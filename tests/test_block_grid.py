"""A widget's pixels must encode the same whether it is pushed alone or
together with a neighbour, or its edges flicker as the two alternate."""

from __future__ import annotations

import io
import random

import pytest
from akp02 import AKP02, Orientation
from PIL import Image, ImageChops

from akp02d.layout import Rect, is_aligned, on_block_grid, screen_size

CASES = [(o, inv) for o in (Orientation.PORTRAIT, Orientation.LANDSCAPE)
         for inv in (False, True)]


def noise(size: tuple[int, int], seed: int) -> Image.Image:
    rng = random.Random(seed)
    return Image.frombytes("RGB", size, bytes(rng.randrange(256) for _ in range(size[0] * size[1] * 3)))


_UNDO = {  # the inverse of the driver's turn into the portrait buffer
    (Orientation.LANDSCAPE, False): Image.Transpose.ROTATE_90,
    (Orientation.LANDSCAPE, True): Image.Transpose.ROTATE_270,
    (Orientation.PORTRAIT, False): None,
    (Orientation.PORTRAIT, True): Image.Transpose.ROTATE_180,
}


def encoder(orientation: Orientation, inverted: bool) -> AKP02:
    panel = AKP02.__new__(AKP02)                     # encoding needs no device
    panel._orientation, panel.inverted = orientation, inverted
    panel.jpeg_quality, panel.jpeg_subsampling = AKP02.JPEG_QUALITY, AKP02.JPEG_SUBSAMPLING
    return panel


def decoded(screen: Image.Image, box, orientation: Orientation, inverted: bool) -> Image.Image:
    """What the panel would show for `box`, back in caller space."""
    jpeg = encoder(orientation, inverted).encode_region(screen.crop(box))
    picture = Image.open(io.BytesIO(jpeg)).convert("RGB")
    undo = _UNDO[(orientation, inverted)]
    return picture if undo is None else picture.transpose(undo)


@pytest.mark.parametrize("orientation,inverted", CASES)
def test_alone_and_together_encode_the_same(orientation, inverted) -> None:
    width, height = screen_size(orientation)
    screen = noise((width, height), 1)
    if orientation is Orientation.PORTRAIT:
        widget, below = Rect(72, 403, 318, 315), Rect(40, 731, 380, 141)
    else:
        widget, below = Rect(603, 40, 301, 211), Rect(560, 260, 410, 150)
    alone = (widget.x, widget.y, widget.x + widget.width, widget.y + widget.height)
    together = (min(widget.x, below.x), widget.y,
                max(widget.x + widget.width, below.x + below.width), below.y + below.height)

    shown = []
    for box in (alone, together):
        grid = on_block_grid(box, orientation, inverted)
        assert grid[0] <= box[0] and grid[1] <= box[1] and grid[2] >= box[2] and grid[3] >= box[3]
        assert 0 <= grid[0] and 0 <= grid[1] and grid[2] <= width and grid[3] <= height
        rect = Rect(grid[0], grid[1], grid[2] - grid[0], grid[3] - grid[1])
        assert is_aligned(rect, orientation, inverted)
        picture = decoded(screen, grid, orientation, inverted)
        shown.append(picture.crop((alone[0] - grid[0], alone[1] - grid[1],
                                   alone[2] - grid[0], alone[3] - grid[1])))
    assert ImageChops.difference(*shown).getbbox() is None


def test_unaligned_regions_do_differ() -> None:
    """The flicker this guards against: without the grid, the edge rows
    of the same widget come out differently."""
    screen = noise((462, 1920), 2)
    alone, together = (72, 403, 390, 718), (72, 403, 390, 872)
    panel = encoder(Orientation.PORTRAIT, False)

    def show(box):
        return Image.open(io.BytesIO(panel.encode_region(screen.crop(box)))).convert("RGB")

    a = show(alone)
    b = show(together).crop((0, 0, *a.size))
    assert ImageChops.difference(a, b).getbbox() is not None
