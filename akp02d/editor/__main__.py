"""akp02d-edit [THEME]: open a theme in the editor.

THEME is a theme name (looked up the way the daemon looks it up) or a
directory; without one, the config's theme opens. A directory that does
not exist yet starts a new theme there.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .. import __version__, config as config_module, theme as theme_module
from ..table import SettingsError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="akp02d-edit", description="Edit akp02d themes with a live preview.")
    parser.add_argument("theme", nargs="?", help="theme name or directory")
    parser.add_argument("-c", "--config", type=Path, help="path to akp02d.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"akp02d-edit {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:
        # Say what failed: "not installed" and "installed for another
        # Python" or "a library it needs is missing" need different fixes.
        print(f"akp02d-edit could not load PySide6: {exc}\n"
              f"  python: {sys.executable} ({sys.version.split()[0]})\n"
              "Install it with pip install 'akp02d[editor]' or your "
              "distribution's PySide6 package (pyside6 on Arch) -- for "
              "this Python.", file=sys.stderr)
        return 1

    # A bug in one handler should cost that action, not the theme being
    # edited: log an exception escaping a Qt callback and carry on, as an
    # unsaved theme is worth more than a clean exit.
    def report(kind, value, tb) -> None:
        logging.getLogger("akp02d.editor").error(
            "unexpected error", exc_info=(kind, value, tb))

    sys.excepthook = report

    from . import style
    from .document import DocumentError, ThemeDocument
    from .window import EditorWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("akp02d-edit")
    app.setDesktopFileName("akp02d-edit")
    style.apply(app)

    problems: list[str] = []
    try:
        cfg = config_module.load(args.config)
    except SettingsError as exc:
        problems.append(f"{exc}\n\nUsing default settings.")
        cfg = config_module.Config()

    name = args.theme or cfg.theme
    document = None
    candidate = Path(name).expanduser()
    if ("/" in name or name.startswith(".")) and not (candidate / "theme.toml").exists() \
            and not candidate.is_file():
        document = ThemeDocument.new(path=candidate / "theme.toml")
    else:
        try:
            path = (candidate if candidate.is_file()
                    else theme_module.find(name, cfg.theme_paths))
            document = ThemeDocument.open(path)
        except (SettingsError, OSError, DocumentError) as exc:
            problems.append(f"Could not open theme {name!r}: {exc}\n\nStarting a new one.")
            document = ThemeDocument.new()

    window = EditorWindow(document, cfg)
    window.show()
    for message in problems:
        QMessageBox.warning(window, "akp02d-edit", message)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
