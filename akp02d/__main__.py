"""Command line entry point: python -m akp02d (or the akp02d script)."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import signal
import sys
import warnings
from pathlib import Path

from akp02 import AKP02, DeviceNotFoundError

from . import __version__, config as config_module, layout, plugins, theme as theme_module
from .plugins import base as plugins_base
from .app import App
from .layout import LayoutError
from .table import SettingsError

log = logging.getLogger("akp02d")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="akp02d", description="Themed system stats on an Ajazz AKP02 panel."
    )
    parser.add_argument("-c", "--config", type=Path, help="path to akp02d.toml")
    parser.add_argument("-t", "--theme", help="theme name or directory, overrides config")
    parser.add_argument("-f", "--fps", type=float, help="frame rate, overrides config")
    parser.add_argument(
        "--stats", nargs="?", type=float, const=5.0, default=0.0, metavar="SECONDS",
        help="log timing every SECONDS (default 5)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="cross-check every region push and turn library warnings into errors",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="load and validate the config and theme, then exit; no device needed",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--version", action="version", version=f"akp02d {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)
    if args.strict:
        # The library nudges a misaligned region and warns rather than
        # refusing. akp02d validates the same rule at load, so a warning
        # here means the two disagree -- worth a crash in development.
        warnings.simplefilter("error")

    try:
        cfg = config_module.load(args.config)
        if args.fps is not None:
            if not 1.0 <= args.fps <= 120.0:
                raise config_module.ConfigError(f"--fps must be 1-120, got {args.fps}")
            cfg = dataclasses.replace(cfg, fps=args.fps)
        name = args.theme or cfg.theme
        path = theme_module.find(name, cfg.theme_paths)
        loaded = theme_module.load(path)
        layout.validate(
            [(spec.id, spec.rect) for spec in loaded.widgets],
            layout.screen_size(loaded.orientation),
            loaded.orientation,
            cfg.device.inverted,
        )
    except (SettingsError, LayoutError) as exc:
        log.error("%s", exc)
        return 2

    log.info(
        "theme %r from %s (%d widget(s)), %s, %.0f fps, plugins: %s",
        loaded.name, path, len(loaded.widgets),
        loaded.orientation.name.lower(), cfg.fps, ", ".join(plugins.available()),
    )

    if args.check:
        # Building the widgets is where fonts load and options are
        # checked, so this catches nearly everything a real run would.
        # Dry run keeps that from starting child processes: it validates
        # the theme, not that a child would have stayed alive.
        plugins_base.set_dry_run(True)
        try:
            for spec in loaded.widgets:
                plugins.create(spec, loaded).close()
        except SettingsError as exc:
            log.error("%s", exc)
            return 2
        log.info("ok")
        return 0

    try:
        panel = AKP02(
            jpeg_quality=cfg.device.jpeg_quality,
            jpeg_subsampling=cfg.device.jpeg_subsampling,
        )
    except (DeviceNotFoundError, ImportError) as exc:
        log.error("%s", exc)
        return 3

    app = App(cfg, loaded, panel, strict=args.strict, stats_interval=args.stats)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.stop())

    try:
        with panel:
            try:
                app.prepare()
            except (SettingsError, LayoutError) as exc:
                log.error("%s", exc)
                return 2
            log.info("running; ctrl-c to stop")
            try:
                app.run()
            finally:
                app.close()
    except OSError as exc:
        log.error("device error: %s", exc)
        return 3
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
