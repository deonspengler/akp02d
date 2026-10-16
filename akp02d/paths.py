"""Where akp02d keeps things.

    ~/.config/akp02d/akp02d.toml     settings ($XDG_CONFIG_HOME if set)
    ~/.config/akp02d/themes/<name>/  themes, by name
    $XDG_RUNTIME_DIR/akp02d.sock     the control socket

A theme name means a folder in themes/; a path (anything with a slash)
runs a theme from where it is. The bundled themes are originals: every
start copies any the user does not have yet and never touches one that
exists. `akp02d --restore-theme NAME` puts an original back, keeping
the edited copy beside it.
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


def name_problem(name: str) -> str | None:
    """What is wrong with `name` for a new theme, or None if it is fine.

    A theme's name is its folder's name and what akp02d.toml says, so it
    is one plain folder name, and not one already taken.
    """
    if not name:
        return "A theme needs a name."
    if "/" in name or "\\" in name or name.startswith(".") or name.startswith("~"):
        return "A name is a plain folder name: no slashes, not starting with . or ~."
    if len(name) > 64:
        return "That name is too long; 64 characters at most."
    if (themes_dir() / name).exists():
        return f"There is already a theme called {name!r}."
    return None


def trash_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / "Trash"


def trash(path: Path) -> Path:
    """Move `path` to the desktop's trash, so it can be got back.

    The freedesktop.org layout every Linux file manager reads: the item
    under Trash/files, and a Trash/info/<name>.trashinfo saying where it
    came from and when -- which is what "Restore" uses. A name already
    in the trash gets a number. Returns where it went.
    """
    from datetime import datetime
    from urllib.parse import quote

    path = path.resolve()
    files, info = trash_dir() / "files", trash_dir() / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)
    name, number = path.name, 2
    while (files / name).exists() or (info / f"{name}.trashinfo").exists():
        name = f"{path.name}.{number}"
        number += 1
    note = info / f"{name}.trashinfo"
    note.write_text("[Trash Info]\n"
                    f"Path={quote(str(path))}\n"
                    f"DeletionDate={datetime.now().strftime('%Y-%m-%dT%H:%M:%S')}\n",
                    encoding="utf-8")
    try:
        shutil.move(str(path), str(files / name))
    except OSError:
        note.unlink(missing_ok=True)
        raise
    return files / name
