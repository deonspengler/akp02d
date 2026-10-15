"""Where akp02d keeps things: one home, under the user's config directory.

    ~/.config/akp02d/akp02d.toml     the machine's settings
    ~/.config/akp02d/themes/<name>/  every theme, by name

(~/.config is $XDG_CONFIG_HOME when that is set.) A theme name means a
folder in themes/ and nowhere else, so the daemon, `akp02d ctl themes`
and the editor all see the same list. An explicit path -- anything with
a slash in it -- still runs a theme from wherever it is, for developing
one in place.

The themes that ship with akp02d live inside the package only as
originals to copy from. Every start copies any of them the user does not
have yet -- so a new one appears after an upgrade -- and never touches
one that exists: the user's copy is theirs to edit.
`akp02d --restore-theme NAME` puts an original back, keeping the edited
one beside it.

The control socket lives in $XDG_RUNTIME_DIR (/run/user/<uid>), which
only the user can enter and which is emptied at logout.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

STARTER_CONFIG = """\
# akp02d settings: what belongs to the machine, not the look.
# Themes live in the themes/ folder beside this file; `theme` names one.

theme = "default"
# fps = 30.0

[device]
# inverted = false          # the panel is mounted upside down
# brightness = 80           # 0-100
# clear_on_exit = true
# jpeg_quality = 80         # 1-95
# jpeg_subsampling = 0      # -1 auto, 0 is 4:4:4 (sharpest), 1 is 4:2:2, 2 is 4:2:0
"""


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "akp02d"


def config_file() -> Path:
    return config_home() / "akp02d.toml"


def themes_dir() -> Path:
    return config_home() / "themes"


def bundled_themes_dir() -> Path:
    """The originals that ship in the package."""
    return Path(__file__).resolve().parent / "themes"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base)
    # No session runtime directory (a bare tty, some containers): a
    # private folder of our own, as XDG suggests.
    fallback = Path(f"/tmp/akp02d-{os.getuid()}")
    fallback.mkdir(mode=0o700, exist_ok=True)
    return fallback


def socket_path() -> Path:
    return runtime_dir() / "akp02d.sock"


def theme_names(folder: Path | None = None) -> list[str]:
    """The themes in `folder` (the themes folder by default), by name."""
    folder = folder or themes_dir()
    try:
        return sorted(p.name for p in folder.iterdir() if (p / "theme.toml").is_file())
    except OSError:
        return []


def setup() -> list[str]:
    """Make the config home on first run and keep it stocked.

    Copies each bundled theme the user does not have, and writes a starter
    akp02d.toml if there is none. Returns what it did, for the log.
    """
    done: list[str] = []
    themes = themes_dir()
    if not themes.is_dir():
        themes.mkdir(parents=True)
        done.append(f"created {themes}")
    for name in theme_names(bundled_themes_dir()):
        target = themes / name
        if not target.exists():
            shutil.copytree(bundled_themes_dir() / name, target)
            done.append(f"copied theme {name!r} to {target}")
    config = config_file()
    if not config.exists():
        config.write_text(STARTER_CONFIG, encoding="utf-8")
        done.append(f"wrote {config}")
    return done


def restore_theme(name: str) -> tuple[Path, Path | None]:
    """Put a bundled theme back as it shipped.

    The current one, if any, is kept as <name>.bak (or .bak2, ...).
    Returns (restored, kept). Raises KeyError for a theme akp02d does not
    ship.
    """
    original = bundled_themes_dir() / name
    if not (original / "theme.toml").is_file():
        raise KeyError(name)
    target = themes_dir() / name
    kept = None
    if target.exists():
        kept = target.with_name(f"{name}.bak")
        number = 2
        while kept.exists():
            kept = target.with_name(f"{name}.bak{number}")
            number += 1
        target.rename(kept)
    themes_dir().mkdir(parents=True, exist_ok=True)
    shutil.copytree(original, target)
    return target, kept
