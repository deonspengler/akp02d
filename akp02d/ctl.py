"""akp02d ctl: talk to the running daemon from a terminal or a script.

    akp02d ctl status
    akp02d ctl themes
    akp02d ctl theme neon [--save]
    akp02d ctl brightness 60
    akp02d ctl reload

`--json` prints the daemon's reply as it came, for scripts. Exit status:
0 done, 1 the daemon said no (the reason on stderr), 3 no daemon running.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import control


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="akp02d ctl",
                                     description="Control the running akp02d.")
    parser.add_argument("--json", action="store_true", help="print the raw reply")
    commands = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    commands.add_parser("status", help="what the daemon is showing")
    commands.add_parser("themes", help="the themes it can show")
    theme = commands.add_parser("theme", help="switch to a theme")
    theme.add_argument("name", help="a theme's name, or a path to its folder")
    theme.add_argument("--save", action="store_true",
                       help="also make it the theme akp02d starts with")
    brightness = commands.add_parser("brightness", help="set the backlight, 0-100")
    brightness.add_argument("value", type=int)
    commands.add_parser("reload", help="load the current theme again")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    request: dict = {"cmd": args.cmd}
    if args.cmd == "theme":
        request.update(name=args.name, save=args.save)
    elif args.cmd == "brightness":
        request["value"] = args.value
    try:
        reply = control.request(request)
    except control.ControlError as exc:
        print(f"akp02d ctl: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(reply, indent=2))
        return 0 if reply.get("ok") else 1
    if not reply.get("ok"):
        print(f"akp02d ctl: {reply.get('error', 'refused')}", file=sys.stderr)
        return 1
    print(describe(args.cmd, reply))
    return 0


def describe(cmd: str, reply: dict) -> str:
    """A reply as a line or two for a person."""
    if cmd == "status":
        return (f"{reply['theme']} ({reply['orientation']}, {reply['widgets']} widgets)"
                f" · {reply['fps']:g} fps · brightness {reply['brightness']}"
                f" · up {_duration(reply['uptime'])} · pid {reply['pid']}")
    if cmd == "themes":
        return "\n".join(f"{'*' if name == reply['current'] else ' '} {name}"
                         for name in reply["themes"]) or f"no themes in {reply['folder']}"
    if cmd == "theme":
        saved = f" (saved to {reply['saved']})" if reply.get("saved") else ""
        return f"showing {reply['theme']}{saved}"
    if cmd == "brightness":
        return f"brightness {reply['brightness']}"
    if cmd == "reload":
        return f"reloaded {reply['theme']}"
    return json.dumps(reply)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"
