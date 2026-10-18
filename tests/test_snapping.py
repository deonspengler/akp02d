"""Placing boxes in the editor: the same snapping on both axes, for
moving and resizing; and a theme may put a widget anywhere."""

from __future__ import annotations

import pytest

from akp02 import Orientation
from akp02d import layout
from akp02d.editor.engine import GRID, follow_ratio, keep_ratio, place
from akp02d.layout import Rect

SCREEN = (462, 1920)
GRAPH = Rect(72, 400, 318, 160)


def test_any_position_is_a_valid_theme() -> None:
    for orientation in (Orientation.PORTRAIT, Orientation.LANDSCAPE):
        screen = layout.screen_size(orientation)
        for inverted in (False, True):
            layout.validate([("a", Rect(3, 5, 77, 41)), ("b", Rect(101, 61, 13, 9))],
                            screen, orientation, inverted)


def test_moving_snaps_to_the_grid_on_both_axes() -> None:
    assert place(Rect(13, 13, 100, 50), "move", [], SCREEN).rect == Rect(16, 16, 100, 50)
    assert place(Rect(203, 707, 100, 50), "move", [], SCREEN).rect == Rect(200, 704, 100, 50)


def test_resizing_snaps_the_dragged_edges_on_both_axes() -> None:
    start = Rect(16, 16, 100, 50)
    rect = place(Rect(16, 16, 107, 53), "rb", [], SCREEN).rect
    assert rect == Rect(16, 16, 104, 56)            # right to 120, bottom to 72
    rect = place(Rect(21, 11, 95, 55), "lt", [], SCREEN).rect
    assert (rect.x, rect.y) == (24, 8) and rect.x + rect.width == 116
    assert rect.y + rect.height == start.y + start.height


def test_edges_and_centres_line_up_with_other_widgets() -> None:
    placed = place(Rect(75, 580, 100, 50), "move", [GRAPH], SCREEN, reach=6)
    assert placed.rect.x == 72                      # left edges line up
    assert any(g.axis == "x" and g.at == 72 for g in placed.guides)
    # Centres: a 100-wide box centred under the graph (centre 231).
    placed = place(Rect(183, 600, 100, 50), "move", [GRAPH], SCREEN, reach=6)
    assert placed.rect.x + 50 == 231
    # A resized edge meets another widget's edge.
    placed = place(Rect(16, 600, 370, 50), "r", [GRAPH], SCREEN, reach=6)
    assert placed.rect.x + placed.rect.width == 390


def test_a_guide_beats_the_grid_and_the_grid_waits_beyond_reach() -> None:
    other = Rect(0, 0, 37, 30)                      # right edge off the grid
    assert place(Rect(36, 100, 20, 20), "move", [other], SCREEN).rect.x == 37
    assert place(Rect(59, 100, 20, 20), "move", [other], SCREEN).rect.x == 56


def test_panel_centre_lines() -> None:
    placed = place(Rect(229, 700, 4, 50), "move", [], SCREEN)
    assert placed.rect.x == 229
    assert any(g.axis == "x" and g.at == 231 and (g.start, g.end) == (0, 1920)
               for g in placed.guides)


def test_even_gaps_between_two_neighbours() -> None:
    left, right = Rect(0, 0, 100, 100), Rect(300, 0, 100, 100)
    placed = place(Rect(172, 10, 50, 50), "move", [left, right], SCREEN)
    assert placed.rect.x == 175
    across = [g for g in placed.gaps if g.axis == "x"]
    assert [(g.start, g.end) for g in across] == [(100, 175), (225, 300)]
    assert all(g.equal for g in across)


def test_gaps_measure_to_the_nearest_neighbours() -> None:
    placed = place(Rect(72, 600, 318, 80), "move", [GRAPH], SCREEN)
    down = [g for g in placed.gaps if g.axis == "y"]
    assert [(g.start, g.end) for g in down] == [(560, 600)] and not down[0].equal


def test_snapping_off_places_freely_but_on_the_panel() -> None:
    assert place(Rect(13, 13, 100, 50), "move", [], SCREEN, snapping=False).rect == \
        Rect(13, 13, 100, 50)
    assert place(Rect(-5, 1900, 100, 50), "move", [], SCREEN, snapping=False).rect == \
        Rect(0, 1870, 100, 50)
    # A resize stops its edge at the panel rather than sliding the box.
    assert place(Rect(400, 10, 100, 50), "r", [], SCREEN, snapping=False).rect == \
        Rect(400, 10, 62, 50)


@pytest.mark.parametrize("mode", ["r", "b", "rb", "lt", "l"])
def test_keeping_proportions(mode: str) -> None:
    start = Rect(40, 40, 200, 100)
    raw = Rect(40, 40, 260, 100) if mode in ("r", "rb") else \
        Rect(40, 40, 200, 130) if mode == "b" else Rect(10, 25, 230, 115)
    shaped, lead = keep_ratio(start, raw, mode)
    assert shaped.width == 2 * shaped.height
    snapped = place(shaped, lead, [], SCREEN).rect
    final = follow_ratio(start, snapped, mode, lead)
    assert abs(final.width - 2 * final.height) <= 1
    if "l" not in mode and "t" not in mode and mode != "b" and mode != "r":
        assert (final.x, final.y) == (start.x, start.y)    # the far corner stays


def test_grid_is_a_block() -> None:
    assert GRID == 8
