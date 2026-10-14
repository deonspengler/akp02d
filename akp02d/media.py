"""What is playing, from playerctl: one shared process per player.

A now-playing block is several widgets -- a title label, an artist
label, a progress bar -- and each owns its own source. If every one ran
`playerctl` on every read, three widgets at 1 fps would start three
processes a second. Instead every source following the same player
shares one `Stream`: a single `playerctl --follow` child that prints a
line whenever the track or the play state changes, read through a
non-blocking pipe exactly the way the spectrum reads cava. Streams are
counted; the last source to let go stops the child.

Position is the one thing playerctl does not announce as it changes --
it would be an event a second for nothing. So the stream notes the
position and the time at each event and, while playing, counts forward
on its own. Every few seconds it also asks once for the whole state (a
short-lived `playerctl metadata`, collected on a later read, never
waited for), which corrects for seeks and drift -- and for a pause or
resume, should a playerctl version not report one as it happens.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Fields in each line playerctl prints, separated by the ASCII unit
# separator, which no title contains -- a tab or a dash might.
SEP = "\x1f"
FORMAT = SEP.join(("{{status}}", "{{playerName}}", "{{position}}", "{{mpris:length}}",
                   "{{artist}}", "{{title}}", "{{album}}", "{{mpris:artUrl}}",
                   "{{volume}}"))
RESYNC_SEC = 5.0


def installed() -> bool:
    return shutil.which("playerctl") is not None


def players() -> list[str]:
    """Players running now, for an editor's dropdown."""
    if not installed():
        return []
    try:
        done = subprocess.run(["playerctl", "-l"], capture_output=True, text=True,
                              timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in done.stdout.splitlines():
        # "spotify" or "firefox.instance1234": the part playerctl -p takes.
        name = line.strip()
        if name and name not in names:
            names.append(name)
    return names


@dataclass(slots=True)
class Track:
    status: str = "Stopped"      # Playing | Paused | Stopped
    player: str = ""
    artist: str = ""
    title: str = ""
    album: str = ""
    # The cover: file://, http(s)://, or a data: URL with the picture
    # inline (some players send it base64-encoded); "" for none.
    art: str = ""
    volume: float | None = None  # the player's own, 0..1; None if it has none
    length: float = 0.0          # seconds
    position: float = 0.0        # seconds, at `at`
    at: float = 0.0              # monotonic time position was true

    def position_now(self, now: float) -> float:
        if self.status != "Playing":
            return self.position
        moved = self.position + max(0.0, now - self.at)
        return min(moved, self.length) if self.length else moved


def parse(line: str, now: float) -> Track:
    """One line of FORMAT output. An empty line means no player."""
    parts = line.rstrip("\n").split(SEP)
    if len(parts) < 7 or not parts[0]:
        return Track(at=now)
    status, player, position, length, artist, title, album = parts[:7]
    art = parts[7].strip() if len(parts) > 7 else ""
    try:
        volume: float | None = float(parts[8]) if len(parts) > 8 and parts[8].strip() else None
    except ValueError:
        volume = None
    return Track(
        status=status, player=player, artist=artist, title=title, album=album, art=art,
        volume=volume,
        length=_seconds(length), position=_seconds(position), at=now,
    )


def _seconds(microseconds: str) -> float:
    try:
        return int(microseconds) / 1e6
    except ValueError:
        return 0.0


class Stream:
    """A running `playerctl --follow` for one player (or the active one)."""

    def __init__(self, player: str | None) -> None:
        self.player = player
        self.track = Track(at=time.monotonic())
        self.users = 0
        self._buffer = b""
        self._process: subprocess.Popen[bytes] | None = None
        self._probe: subprocess.Popen[bytes] | None = None
        self._next_probe = 0.0
        self._alive = False

    def _args(self, *rest: str) -> list[str]:
        return ["playerctl", *(("-p", self.player) if self.player else ()), *rest]

    def start(self) -> None:
        try:
            self._process = subprocess.Popen(
                self._args("--follow", "metadata", "--format", FORMAT),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as exc:
            log.warning("cannot start playerctl: %s", exc)
            return
        assert self._process.stdout is not None
        os.set_blocking(self._process.stdout.fileno(), False)
        self._alive = True
        # The follower's first line is the whole state: no probe until
        # it has had time to go stale.
        self._next_probe = time.monotonic() + RESYNC_SEC

    def read(self, now: float) -> Track:
        """The current track, after taking in whatever has arrived."""
        if self._alive and self._process is not None:
            stdout = self._process.stdout
            assert stdout is not None
            while True:
                try:
                    chunk = os.read(stdout.fileno(), 65536)
                except BlockingIOError:
                    break
                except OSError:
                    chunk = b""
                if not chunk:
                    log.warning("playerctl ended; now playing is frozen")
                    self._alive = False
                    break
                self._buffer += chunk
            *lines, self._buffer = self._buffer.split(b"\n")
            if lines:
                # Latest wins: older events are already out of date.
                self.track = parse(lines[-1].decode("utf-8", "replace"), now)
                self._next_probe = now + RESYNC_SEC
        self._resync(now)
        return self.track

    def _resync(self, now: float) -> None:
        """Ask the player for its whole state now and then, without waiting."""
        probe = self._probe
        if probe is not None:
            if probe.poll() is None:
                return
            out = probe.stdout.read() if probe.stdout else b""
            self._probe = None
            if probe.returncode == 0:
                # An empty answer means no player: that is news too.
                self.track = parse(out.decode("utf-8", "replace"), now)
            return
        if now < self._next_probe or not self._alive:
            return
        self._next_probe = now + RESYNC_SEC
        try:
            self._probe = subprocess.Popen(self._args("metadata", "--format", FORMAT),
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)
        except OSError:
            self._probe = None

    def close(self) -> None:
        self._alive = False
        for process in (self._process, self._probe):
            if process is None:
                continue
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdout is not None:
                process.stdout.close()
        self._process = self._probe = None


_STREAMS: dict[str | None, Stream] = {}


def acquire(player: str | None) -> Stream:
    """The shared stream for `player`, started on first use."""
    stream = _STREAMS.get(player)
    if stream is None:
        stream = _STREAMS[player] = Stream(player)
        stream.start()
    stream.users += 1
    return stream


def release(stream: Stream) -> None:
    stream.users -= 1
    if stream.users <= 0:
        stream.close()
        if _STREAMS.get(stream.player) is stream:
            del _STREAMS[stream.player]


def running() -> int:
    """How many playerctl followers are running, for tests and logs."""
    return len(_STREAMS)
