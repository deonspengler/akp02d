"""akp02d.toml: the settings that belong to the machine, not the look.

Orientation lives in the theme, since a theme's coordinates are drawn
for one shape and are meaningless in the other. `inverted` stays here:
it describes how the panel is physically mounted, so the same theme
runs on a desk with the panel upside down.

Only things a person has a reason to choose belong here. The keepalive
interval, for one, does not: it is set by the firmware's sleep timeout,
the library already knows it, and a wrong value shows up as the panel
going dark mid-session.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from akp02 import AKP02

from . import paths
from .table import SettingsError, Table


class ConfigError(SettingsError):
    """akp02d.toml is missing, malformed, or has an unusable value."""


def default_theme_paths() -> tuple[Path, ...]:
    """Where a theme name is looked up: the themes folder, and only it
    (see paths.py). Read at call time, so XDG_CONFIG_HOME is honoured."""
    return (paths.themes_dir(),)


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    inverted: bool = False
    brightness: int = 80
    jpeg_quality: int = AKP02.JPEG_QUALITY
    jpeg_subsampling: int = AKP02.JPEG_SUBSAMPLING
    clear_on_exit: bool = True


@dataclass(frozen=True, slots=True)
class Config:
    fps: float = 30.0
    theme: str = "default"
    theme_paths: tuple[Path, ...] = field(default_factory=default_theme_paths)
    device: DeviceConfig = field(default_factory=DeviceConfig)
    source: Path | None = None


def load(path: Path | None = None) -> Config:
    """Read a config file, or return defaults if none exists.

    The file is ~/.config/akp02d/akp02d.toml unless `path` names
    another. An explicit `path` that does not exist is an error; no file
    in the usual place is not -- akp02d runs on defaults.
    """
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
    else:
        path = paths.config_file()
        if not path.is_file():
            return Config()
    try:
        top = Table(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    fps = top.number("fps", 30.0, 1.0, 120.0)
    theme = top.string("theme", "default")
    dev = top.table("device")
    device = DeviceConfig(
        inverted=dev.boolean("inverted", False),
        brightness=dev.integer(
            "brightness", 80, AKP02.BRIGHTNESS_MIN, AKP02.BRIGHTNESS_MAX
        ),
        jpeg_quality=dev.integer(
            "jpeg_quality",
            AKP02.JPEG_QUALITY,
            AKP02.JPEG_QUALITY_MIN,
            AKP02.JPEG_QUALITY_MAX,
        ),
        jpeg_subsampling=dev.one_of(
            "jpeg_subsampling", AKP02.JPEG_SUBSAMPLING, AKP02.JPEG_SUBSAMPLING_VALUES
        ),
        clear_on_exit=dev.boolean("clear_on_exit", True),
    )
    dev.reject_unknown()
    top.reject_unknown()
    return Config(
        fps=fps,
        theme=theme,
        device=device,
        source=path,
    )


_THEME_LINE = re.compile(r"^theme\s*=.*$", re.MULTILINE)


def save_theme(name: str, path: Path | None = None) -> Path:
    """Make `name` the theme akp02d starts with, in its config file.

    Only the `theme = ...` line changes: comments, order and every other
    setting stay as written. A file with no such line gets one at the top
    of its settings; no file at all gets the starter config. Written to a
    temporary file and renamed, so a crash never leaves half a config.
    """
    path = path or paths.config_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = paths.STARTER_CONFIG
    line = f"theme = {json.dumps(name)}"
    # Only a top-level key counts: one inside [device] is something else.
    head, table, tail = text.partition("\n[")
    if _THEME_LINE.search(head):
        head = _THEME_LINE.sub(lambda _m: line, head, count=1)
    else:
        lines = head.split("\n")
        # After the opening comments and blank lines.
        at = 0
        while at < len(lines) and (not lines[at].strip() or lines[at].lstrip().startswith("#")):
            at += 1
        lines.insert(at, line)
        head = "\n".join(lines)
    text = head + table + tail
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path
