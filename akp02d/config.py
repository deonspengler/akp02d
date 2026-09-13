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

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from akp02 import AKP02

from .table import SettingsError, Table


class ConfigError(SettingsError):
    """akp02d.toml is missing, malformed, or has an unusable value."""


DEFAULT_CONFIG_PATHS = (
    Path("akp02d.toml"),
    Path.home() / ".config" / "akp02d" / "akp02d.toml",
    Path("/etc/akp02d/akp02d.toml"),
)


def default_theme_paths() -> tuple[Path, ...]:
    """Where a theme name is looked up, first match wins."""
    return (
        Path("themes"),
        Path.home() / ".config" / "akp02d" / "themes",
        # Inside the package, so it ships in a wheel. The two above come
        # first, so a local theme of the same name still wins.
        Path(__file__).resolve().parent / "themes",
    )


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

    An explicit `path` that does not exist is an error; the implicit
    search finding nothing is not -- akp02d runs on defaults.
    """
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
    else:
        path = next((p for p in DEFAULT_CONFIG_PATHS if p.is_file()), None)
        if path is None:
            return Config()
    try:
        top = Table(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    fps = top.number("fps", 30.0, 1.0, 120.0)
    theme = top.string("theme", "default")
    paths = top.raw("theme_paths")
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
        theme_paths=(
            tuple(Path(p).expanduser() for p in paths)
            if paths is not None
            else default_theme_paths()
        ),
        device=device,
        source=path,
    )
