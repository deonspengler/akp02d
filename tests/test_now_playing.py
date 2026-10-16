"""The now_playing source: one shared playerctl, fields, and label fit."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest
from PIL import Image

from akp02 import Orientation
from akp02d import media, sources
from akp02d.layout import Rect
from akp02d.plugins import lookup
from akp02d.plugins.base import WidgetSpec, set_dry_run
from akp02d.table import Table
from akp02d.theme import Theme

FAKE = r"""#!/bin/sh
# A stand-in for playerctl: logs each start, answers like the real one.
echo "$*" >> "$PLAYERCTL_LOG"
# --follow first: its format string itself contains "position".
case "$*" in
  *--follow*)
    printf 'Playing\037spotify\03712000000\037213000000\037Lumen\037Afterglow\037Static Hearts\n'
    exec sleep 60 ;;
  *"-l"*) printf 'spotify\nmpv\n' ;;
  *metadata*)
    printf 'Paused\037spotify\03730500000\037213000000\037Lumen\037Afterglow\037Static Hearts\n' ;;
esac
"""


@pytest.fixture
def playerctl(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "playerctl"
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "playerctl.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PLAYERCTL_LOG", str(log))
    yield log
    for stream in list(media._STREAMS.values()):
        stream.close()
    media._STREAMS.clear()


def follows(log: Path) -> int:
    return sum("--follow" in line for line in log.read_text().splitlines())


def settle(source, seconds: float = 1.0):
    """Read until the follower's first line has arrived."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        _, fields = source.read(time.monotonic())
        if fields["status"] == "Playing":
            return fields
        time.sleep(0.02)
    return fields


def test_fields_from_playerctl(playerctl) -> None:
    source = sources.create("now_playing", Table({}), "w")
    fields = settle(source)
    assert fields["title"] == "Afterglow" and fields["artist"] == "Lumen"
    assert fields["album"] == "Static Hearts" and fields["player"] == "spotify"
    assert fields["length"] == "3:33"
    assert fields["position"].startswith("0:1")          # 12 s and counting
    assert 0.05 < fields["value"] < 0.07
    source.close()


def test_one_process_shared_and_released(playerctl) -> None:
    title = sources.create("now_playing", Table({}), "a")
    artist = sources.create("now_playing", Table({}), "b")
    progress = sources.create("now_playing", Table({}), "c")
    for source in (title, artist, progress):
        settle(source)
    assert follows(playerctl) == 1 and media.running() == 1
    other = sources.create("now_playing", Table({"player": "mpv"}), "d")
    settle(other)
    assert follows(playerctl) == 2 and media.running() == 2
    process = media._STREAMS[None]._process
    for source in (title, artist):
        source.close()
    assert process.poll() is None                        # still in use
    progress.close()
    process.wait(timeout=2)                              # last one stopped it
    assert media.running() == 1
    other.close()
    assert media.running() == 0


def test_position_counts_forward_and_resyncs(playerctl) -> None:
    stream = media.acquire(None)
    try:
        end = time.monotonic() + 1
        while stream.read(time.monotonic()).status != "Playing" and time.monotonic() < end:
            time.sleep(0.02)
        track = stream.track
        assert track.position_now(track.at + 2.0) == pytest.approx(14.0)
        stream._next_probe = 0.0                         # due now
        stream.read(time.monotonic())
        end = time.monotonic() + 2
        while stream._probe is not None or stream.track.position != 30.5:
            stream.read(time.monotonic())
            if time.monotonic() > end:
                break
            time.sleep(0.02)
        # Corrected by the periodic check: position, and a pause the
        # follower never reported.
        assert stream.track.position == 30.5 and stream.track.status == "Paused"
        assert stream.track.position_now(stream.track.at + 5) == 30.5  # paused: still
    finally:
        media.release(stream)


def test_nothing_playing_shows_idle() -> None:
    fields = sources.now_playing_fields(media.Track(), 0.0, "— silence —")
    assert fields["title"] == "— silence —"
    assert fields["artist"] == "" and fields["position"] == "" and fields["value"] == 0.0


def test_check_does_not_start_playerctl(playerctl) -> None:
    set_dry_run(True)
    try:
        source = sources.create("now_playing", Table({}), "w")
        source.read(0.0)
        assert follows(playerctl) == 0
    finally:
        set_dry_run(False)


def test_missing_playerctl(monkeypatch) -> None:
    monkeypatch.setattr(media, "installed", lambda: False)
    with pytest.raises(sources.SourceError, match="playerctl is not installed"):
        sources.create("now_playing", Table({}), "w")


def test_players_for_the_dropdown(playerctl) -> None:
    assert media.players() == ["spotify", "mpv"]


def test_widget_close_releases_its_source(playerctl) -> None:
    theme = Theme(name="t", path=Path("."), orientation=Orientation.PORTRAIT,
                  background=Image.new("RGB", (462, 1920)), font_default=None, widgets=())
    spec = WidgetSpec("label#1", "label", Rect(0, 0, 300, 40), None,
                      Table({"source": "now_playing"}))
    label = lookup("label")(spec, theme)
    assert media.running() == 1
    label.close()
    assert media.running() == 0


# -- label fit --


def label(fit: str, text: str, width: int = 200):
    theme = Theme(name="t", path=Path("."), orientation=Orientation.PORTRAIT,
                  background=Image.new("RGB", (462, 1920)), font_default=None, widgets=())
    spec = WidgetSpec("label#1", "label", Rect(0, 0, width, 40), None,
                      Table({"format": text, "size": 24, "fit": fit}))
    return lookup("label")(spec, theme)


def test_fit_ellipsis_and_shrink() -> None:
    long = "Afterglow (Extended Midnight Mix)"
    clipped = label("clip", long)
    assert clipped._fitted(long)[0] == long
    trimmed, font, size = label("ellipsis", long)._fitted(long)
    assert trimmed.endswith("…") and len(trimmed) < len(long)
    assert font.getlength(trimmed) <= 200
    shrunk, font, size = label("shrink", long)._fitted(long)
    assert shrunk == long and size < 24 and font.getlength(long) <= 200
    # Too long even at the smallest size: drawn at that size, clipped.
    assert label("shrink", long * 4)._fitted(long * 4)[2] == 8
    # Text that fits is left alone in every mode.
    for mode in ("clip", "ellipsis", "shrink"):
        assert label(mode, "Hi")._fitted("Hi")[0] == "Hi"


def test_only_labels_and_images_take_it(playerctl) -> None:
    from akp02d.plugins import lookup
    from akp02d.schema import SOURCE

    cls = sources.lookup("now_playing")
    for plugin, takes in (("label", True), ("image", True),
                          ("bar", False), ("gauge", False), ("graph", False)):
        option = next(o for o in lookup(plugin).schema if o.kind == SOURCE)
        assert sources.fits(cls, option.accepts) is takes, plugin
