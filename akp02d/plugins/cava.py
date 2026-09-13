"""The cava process: its config, its pipe, and the frames it produces.

Separated from the widget that draws them, so `spectrum.py` is about
shapes and this is about where the numbers come from. It also marks the
seam a second backend would use.

The pipe is non-blocking, so no thread is needed: read() drains whatever
has arrived and keeps the newest whole frame. Latest-wins is the correct
policy -- a stale audio frame is worthless -- and draining on demand
gives the freshest frame available at the moment it is wanted. An empty
pipe costs one EAGAIN.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

log = logging.getLogger(__name__)

MAX = 65535  # cava's 16-bit output range

# Written in this order, so a generated config reads the way cava's own
# example does.
SECTIONS = ("general", "input", "smoothing", "eq", "output")
CHANNELS = ("stereo", "mono")

# The frame's shape. bars and framerate size it; the output block puts it
# on stdout as raw little-endian 16-bit. raw_target is cava's default
# anyway, but it has been a bug before, so it is written not assumed.
FIXED = {
    "output": {
        "method": "raw",
        "raw_target": "/dev/stdout",
        "data_format": "binary",
        "bit_format": "16bit",
    }
}
OWNED_GENERAL = ("bars", "framerate")


class CavaError(Exception):
    """cava is missing, misconfigured, or would not start."""


class Stream:
    """A running cava, and the frames it has produced.

    Owns the config file too: cava reads it for as long as it runs, so it
    cannot be deleted at spawn time and would otherwise be left in /tmp
    on every start.
    """

    __slots__ = ("_alive", "_buffer", "_fd", "_path", "_process", "_swap", "_width")

    def __init__(self, bars: int, config: str, widget: str) -> None:
        self._width = bars * 2  # bytes per frame, 16-bit mono
        self._buffer = bytearray()
        self._swap = sys.byteorder == "big"
        self._process: subprocess.Popen[bytes] | None = None
        self._path: Path | None = None
        self._fd = -1
        self._alive = False

        handle = tempfile.NamedTemporaryFile(
            "w", suffix=f"-{widget}.conf", prefix="akp02d-cava-", delete=False
        )
        with handle as file:
            file.write(config)
        self._path = Path(handle.name)
        try:
            self._process = subprocess.Popen(
                ["cava", "-p", handle.name],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            self._path.unlink(missing_ok=True)
            self._path = None
            raise CavaError(f"widget {widget!r}: cannot start cava: {exc}") from exc
        stdout = self._process.stdout
        assert stdout is not None
        self._fd = stdout.fileno()
        # cava's pipe buffer holds hundreds of frames, so a slow tick
        # backs up harmlessly rather than stalling the process.
        os.set_blocking(self._fd, False)
        self._alive = True

    def read(self) -> tuple[int, ...] | None:
        """Everything waiting, reduced to the newest whole frame, or None."""
        if not self._alive:
            return None
        buffer = self._buffer
        while True:
            try:
                chunk = os.read(self._fd, 65536)
            except BlockingIOError:
                break  # nothing more waiting: the common case
            except OSError as exc:
                log.warning("cava pipe failed (%s); visualiser is now static", exc)
                self._alive = False
                return None
            if not chunk:
                log.warning("cava ended; visualiser is now static")
                self._alive = False
                return None
            buffer += chunk
        whole = len(buffer) - len(buffer) % self._width
        if not whole:
            return None
        values = array("H")
        values.frombytes(bytes(buffer[whole - self._width : whole]))
        del buffer[:whole]  # older frames are stale; drop them
        if self._swap:
            values.byteswap()
        return tuple(values)

    def close(self) -> None:
        self._alive = False
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            if self._process.stdout is not None:
                self._process.stdout.close()
            self._process = None
        if self._path is not None:
            # cava needed the file for as long as it ran; it does not now.
            self._path.unlink(missing_ok=True)
            self._path = None


def installed() -> bool:
    return shutil.which("cava") is not None


def merge(named: dict[str, dict], extra: dict[str, dict], widget: str) -> dict:
    """Fold the escape hatch into the named settings, refusing overlap."""
    merged = {name: dict(body) for name, body in named.items()}
    for section, body in extra.items():
        target = merged.setdefault(section, {})
        for key, value in body.items():
            if (
                key in target
                or key in FIXED.get(section, ())
                or (section == "general" and key in OWNED_GENERAL)
            ):
                raise CavaError(
                    f"widget {widget!r}: cava.{section}.{key} is already set by "
                    f"akp02d; use the widget's own option instead"
                )
            target[key] = value
    return merged


def config(bars: int, framerate: int, sections: dict[str, dict]) -> str:
    """The config file: the frame's shape, plus what the theme asked for."""
    merged = {name: dict(body) for name, body in sections.items()}
    merged["general"] = {
        "framerate": framerate,
        "bars": bars,
        **merged.get("general", {}),
    }
    for name, body in FIXED.items():
        merged[name] = {**body, **merged.get(name, {})}
    lines: list[str] = []
    for name in SECTIONS:
        body = merged.get(name)
        if not body:
            continue
        lines.append(f"[{name}]")
        lines += [f"{key} = {_ini(value)}" for key, value in body.items()]
        lines.append("")
    return "\n".join(lines)


def _ini(value: object) -> str:
    """cava's config takes 1/0 rather than true/false."""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
