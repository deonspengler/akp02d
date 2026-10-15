"""The volume source: one shared pactl, no polling, levels and mute."""

from __future__ import annotations

import stat
import time
from pathlib import Path

import os
import pytest

from akp02d import media, sources, volume
from akp02d.plugins.base import set_dry_run
from akp02d.table import Table

FAKE = r"""#!/bin/sh
# A stand-in for pactl: logs each call (and its locale), answers like
# the real one from files the test writes.
echo "$LC_ALL $*" >> "$PACTL_LOG"
case "$*" in
  subscribe)
    if [ -n "$PACTL_ONESHOT" ]; then echo "Event 'change' on sink #56"; exit 0; fi
    while true; do echo "Event 'change' on sink #56"; sleep 0.1; done ;;
  get-sink-volume*)
    v=$(cat "$PACTL_DIR/volume")
    echo "Volume: front-left: 42598 /  $v% / -11.23 dB,   front-right: 42598 /  $v% / -11.23 dB"
    echo "        balance 0.00" ;;
  get-source-volume*)
    echo "Volume: mono: 26214 /  40% / -23.88 dB" ;;
  get-sink-mute*) echo "Mute: $(cat "$PACTL_DIR/mute")" ;;
  get-source-mute*) echo "Mute: no" ;;
  get-default-sink) echo "alsa_output.pci.analog-stereo" ;;
  get-default-source) echo "alsa_input.pci.analog-stereo" ;;
  "-f json list sinks")
    echo '[{"name":"alsa_output.pci.analog-stereo","description":"Built-in Audio Analog Stereo"},{"name":"bluez_output.headset","description":"WH-1000XM4"}]' ;;
  "-f json list sources")
    echo '[{"name":"alsa_input.pci.analog-stereo","description":"Built-in Mic"},{"name":"alsa_output.pci.analog-stereo.monitor","description":"Monitor of Built-in Audio"}]' ;;
esac
"""


@pytest.fixture
def pactl(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "pactl"
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "pactl.log"
    log.write_text("")
    (tmp_path / "volume").write_text("65")
    (tmp_path / "mute").write_text("no")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PACTL_LOG", str(log))
    monkeypatch.setenv("PACTL_DIR", str(tmp_path))
    yield tmp_path
    for stream in list(volume._STREAMS.values()):
        stream.close()
    volume._STREAMS.clear()


def calls(state: Path, what: str) -> int:
    return sum(what in line for line in (state / "pactl.log").read_text().splitlines())


def until(source, check, seconds: float = 2.0):
    end = time.monotonic() + seconds
    while True:
        _, fields = source.read(time.monotonic())
        if check(fields) or time.monotonic() > end:
            return fields
        time.sleep(0.02)


def test_reads_the_default_output(pactl) -> None:
    source = sources.create("volume", Table({}), "w")
    fields = until(source, lambda f: f["device"])
    assert fields["value"] == pytest.approx(0.65) and fields["percent"] == pytest.approx(65)
    assert fields["text"] == "65%" and not fields["muted"]
    assert fields["device"] == "Built-in Audio Analog Stereo"
    # Untranslated output: every call in the C locale.
    assert all(line.startswith("C ") for line in (pactl / "pactl.log").read_text().splitlines())
    source.close()


def test_follows_changes_and_mute(pactl) -> None:
    source = sources.create("volume", Table({}), "w")
    until(source, lambda f: f["device"])
    (pactl / "volume").write_text("30")
    fields = until(source, lambda f: abs(f["percent"] - 30) < 0.01)
    assert fields["value"] == pytest.approx(0.30)
    (pactl / "mute").write_text("yes")
    fields = until(source, lambda f: f["muted"])
    # What you hear is nothing; the level set is still there.
    assert fields["value"] == 0.0 and fields["percent"] == pytest.approx(30)
    assert fields["text"] == "muted"
    source.close()


def test_boost_above_100(pactl) -> None:
    (pactl / "volume").write_text("130")
    source = sources.create("volume", Table({}), "w")
    fields = until(source, lambda f: f["device"])
    assert fields["value"] == pytest.approx(1.3) and fields["text"] == "130%"
    source.close()


def test_microphone(pactl) -> None:
    source = sources.create("volume", Table({"input": True}), "w")
    fields = until(source, lambda f: f["device"])
    assert fields["percent"] == pytest.approx(40) and fields["device"] == "Built-in Mic"
    source.close()


def test_one_process_shared_and_released(pactl) -> None:
    widgets = [sources.create("volume", Table({}), name) for name in "abc"]
    for source in widgets:
        until(source, lambda f: f["device"])
    assert calls(pactl, "subscribe") == 1 and volume.running() == 1
    process = volume._STREAMS[(None, False)]._process
    for source in widgets[:2]:
        source.close()
    assert process.poll() is None
    widgets[2].close()
    process.wait(timeout=2)
    assert volume.running() == 0


def test_restarts_when_the_sound_server_does(pactl, monkeypatch) -> None:
    monkeypatch.setenv("PACTL_ONESHOT", "1")
    monkeypatch.setattr(volume, "RESTART_SEC", 0.1)
    source = sources.create("volume", Table({}), "w")
    end = time.monotonic() + 3
    while calls(pactl, "subscribe") < 2 and time.monotonic() < end:
        source.read(time.monotonic())
        time.sleep(0.02)
    assert calls(pactl, "subscribe") >= 2
    source.close()


def test_devices_for_the_dropdown(pactl) -> None:
    assert volume.devices() == [("alsa_output.pci.analog-stereo", "Built-in Audio Analog Stereo"),
                                ("bluez_output.headset", "WH-1000XM4")]
    # A monitor is not a microphone.
    assert volume.devices(True) == [("alsa_input.pci.analog-stereo", "Built-in Mic")]


def test_check_does_not_start_pactl(pactl) -> None:
    set_dry_run(True)
    try:
        source = sources.create("volume", Table({}), "w")
        source.read(0.0)
        assert calls(pactl, "subscribe") == 0
    finally:
        set_dry_run(False)


def test_missing_pactl(monkeypatch) -> None:
    monkeypatch.setattr(volume, "installed", lambda: False)
    with pytest.raises(sources.SourceError, match="pactl is not installed"):
        sources.create("volume", Table({}), "w")


def test_parse_is_strict_about_garbage() -> None:
    assert not volume.parse("", False).known
    assert not volume.parse("nonsense@@akp02d@@x@@akp02d@@y@@akp02d@@[]", False).known


def test_player_volume_on_now_playing() -> None:
    line = media.SEP.join(("Playing", "spotify", "1", "2", "A", "T", "Al", "", "0.42"))
    assert media.parse(line, 0.0).volume == pytest.approx(0.42)
    fields = sources.now_playing_fields(media.parse(line, 0.0), 0.0, "idle")
    assert fields["volume"] == pytest.approx(42)
    seven = media.SEP.join(("Playing", "x", "0", "0", "A", "T", "Al"))
    assert sources.now_playing_fields(media.parse(seven, 0.0), 0.0, "idle")["volume"] == 0.0
