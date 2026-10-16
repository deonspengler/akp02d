"""The system's volume, from pactl (PulseAudio, or PipeWire through
pipewire-pulse): one shared process per device.

Every source watching the same device shares one `pactl subscribe`
child. A change to that device, or to the default device, starts one
short-lived probe, collected on a later read; events arriving meanwhile
just mark one more to follow, so a held volume key costs two probes,
not twenty. Calls run in the C locale (pactl's output is translated),
and a sound server restart restarts the subscription.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

RESTART_SEC = 5.0
# Splits the probe's answers apart; no device name contains it.
MARK = "@@akp02d@@"
_PERCENT = re.compile(r"(\d+)%")


def _env() -> dict[str, str]:
    """This process's environment, in the C locale (read at each call)."""
    return {**os.environ, "LC_ALL": "C"}


def installed() -> bool:
    return shutil.which("pactl") is not None


def _kind(input_: bool) -> str:
    return "source" if input_ else "sink"


def devices(input_: bool = False) -> list[tuple[str, str]]:
    """(name, description) of each output (or input), for a dropdown.

    Monitors -- the "what is playing" side of each output -- are not
    inputs anyone means, so they are left out of the inputs.
    """
    if not installed():
        return []
    kind = _kind(input_)
    try:
        done = subprocess.run(["pactl", "-f", "json", "list", f"{kind}s"],
                              capture_output=True, text=True, timeout=2.0, env=_env())
        entries = json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        entries = None
    if isinstance(entries, list):
        found = [(e.get("name", ""), e.get("description", "") or e.get("name", ""))
                 for e in entries if isinstance(e, dict)]
    else:   # an older pactl, without -f json: names only
        try:
            done = subprocess.run(["pactl", "list", "short", f"{kind}s"],
                                  capture_output=True, text=True, timeout=2.0, env=_env())
        except (OSError, subprocess.TimeoutExpired):
            return []
        found = [(line.split("\t")[1],) * 2 for line in done.stdout.splitlines()
                 if line.count("\t") >= 1]
    return [(name, text) for name, text in found
            if name and not (input_ and name.endswith(".monitor"))]


@dataclass(slots=True)
class Level:
    volume: float = 0.0        # 1.0 is 100%; PipeWire allows more
    muted: bool = False
    device: str = ""           # description, or name if there is none
    known: bool = False        # False until the first answer arrives


def parse(text: str, input_: bool) -> Level:
    """The probe's answer: volume, mute, default device, device list."""
    parts = text.split(MARK)
    if len(parts) < 4:
        return Level()
    volume_text, mute_text, default_text, listing = parts[:4]
    percents = [int(p) for p in _PERCENT.findall(volume_text)]
    if not percents:
        return Level()
    name = default_text.strip()
    device = name
    try:
        for entry in json.loads(listing):
            if isinstance(entry, dict) and entry.get("name") == name:
                device = entry.get("description") or name
                break
    except ValueError:
        pass
    return Level(volume=sum(percents) / len(percents) / 100,
                 muted="yes" in mute_text.lower(), device=device, known=True)


class Stream:
    """A running `pactl subscribe`, and the latest level of one device."""

    def __init__(self, device: str | None, input_: bool) -> None:
        self.device = device
        self.input = input_
        self.level = Level()
        self.users = 0
        self._buffer = b""
        self._process: subprocess.Popen[bytes] | None = None
        self._probe: subprocess.Popen[bytes] | None = None
        self._again = False
        self._retry_at = 0.0

    @property
    def _target(self) -> str:
        if self.device:
            return self.device
        return "@DEFAULT_SOURCE@" if self.input else "@DEFAULT_SINK@"

    def start(self) -> None:
        try:
            self._process = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, bufsize=0, env=_env())
        except OSError as exc:
            log.warning("cannot start pactl: %s", exc)
            self._process = None
            return
        assert self._process.stdout is not None
        os.set_blocking(self._process.stdout.fileno(), False)
        self._again = True        # the state now, before any event

    def read(self, now: float) -> Level:
        process = self._process
        if process is not None:
            assert process.stdout is not None
            while True:
                try:
                    chunk = os.read(process.stdout.fileno(), 65536)
                except BlockingIOError:
                    break
                except OSError:
                    chunk = b""
                if not chunk:
                    log.warning("pactl subscribe ended (sound server restarted?); "
                                "trying again in %gs", RESTART_SEC)
                    self._stop(process)
                    self._process = None
                    self._retry_at = now + RESTART_SEC
                    break
                self._buffer += chunk
            *lines, self._buffer = self._buffer.split(b"\n")
            if any(self._concerns(line) for line in lines):
                self._again = True
        elif now >= self._retry_at:
            self.start()
        self._collect()
        return self.level

    def _concerns(self, line: bytes) -> bool:
        text = line.decode("utf-8", "replace")
        # "Event 'change' on sink #56"; "server" when the default moves.
        return f" on {_kind(self.input)} #" in text or " on server" in text

    def _collect(self) -> None:
        probe = self._probe
        if probe is not None:
            if probe.poll() is None:
                return
            out = probe.stdout.read() if probe.stdout else b""
            if probe.stdout is not None:
                probe.stdout.close()
            self._probe = None
            level = parse(out.decode("utf-8", "replace"), self.input)
            if level.known:
                self.level = level
        if self._again:
            self._again = False
            self._ask()

    def _ask(self) -> None:
        kind = _kind(self.input)
        # One short-lived shell for the four answers, the device passed as
        # an argument -- never spliced into the script.
        script = (f'pactl get-{kind}-volume "$1"; echo "{MARK}"; '
                  f'pactl get-{kind}-mute "$1"; echo "{MARK}"; '
                  f'if [ "$1" = "@DEFAULT_{kind.upper()}@" ]; then pactl get-default-{kind}; '
                  f'else echo "$1"; fi; echo "{MARK}"; '
                  f'pactl -f json list {kind}s 2>/dev/null || echo "[]"')
        try:
            self._probe = subprocess.Popen(["sh", "-c", script, "sh", self._target],
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           env=_env())
        except OSError:
            self._probe = None

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
        if process.stdout is not None:
            process.stdout.close()

    def close(self) -> None:
        for process in (self._process, self._probe):
            if process is not None:
                self._stop(process)
        self._process = self._probe = None


_STREAMS: dict[tuple[str | None, bool], Stream] = {}


def acquire(device: str | None, input_: bool) -> Stream:
    """The shared stream for `device` (None: the default), started on
    first use."""
    key = (device, input_)
    stream = _STREAMS.get(key)
    if stream is None:
        stream = _STREAMS[key] = Stream(device, input_)
        stream.start()
    stream.users += 1
    return stream


def release(stream: Stream) -> None:
    stream.users -= 1
    if stream.users <= 0:
        stream.close()
        key = (stream.device, stream.input)
        if _STREAMS.get(key) is stream:
            del _STREAMS[key]


def running() -> int:
    """How many pactl subscribers are running, for tests and logs."""
    return len(_STREAMS)
