"""The themes folder, and the control socket: a running daemon driven
from outside, the way the editor and `akp02d ctl` drive it."""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest

from akp02d import config, control, ctl, paths, theme as theme_module
from akp02d.__main__ import main
from akp02d.app import App

from test_reload import FakePanel

PORTRAIT = """\
[theme]
orientation = "portrait"
background = "#102030"

[[widget.label]]
x = 0
y = 0
width = 200
height = 40
format = "{name}"
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A config home and runtime dir of our own, never the real ones."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return tmp_path


def add_theme(name: str, text: str = PORTRAIT) -> Path:
    folder = paths.themes_dir() / name
    folder.mkdir(parents=True)
    (folder / "theme.toml").write_text(text.replace("{name}", name))
    return folder


# -- the themes folder --


def test_first_run_sets_up_the_home(home) -> None:
    done = paths.setup()
    assert (paths.themes_dir() / "default" / "theme.toml").is_file()
    assert paths.config_file().is_file()
    assert len(done) == 3
    cfg = config.load()
    assert cfg.theme == "default" and cfg.theme_paths == (paths.themes_dir(),)
    # A second run touches nothing.
    assert paths.setup() == []


def test_setup_never_overwrites_an_edited_theme(home) -> None:
    paths.setup()
    mine = paths.themes_dir() / "default" / "theme.toml"
    mine.write_text("# mine\n" + mine.read_text())
    paths.setup()
    assert mine.read_text().startswith("# mine")


def test_restore_keeps_the_edited_one(home) -> None:
    paths.setup()
    mine = paths.themes_dir() / "default" / "theme.toml"
    mine.write_text("# mine\n")
    restored, kept = paths.restore_theme("default")
    assert kept == paths.themes_dir() / "default.bak"
    assert (kept / "theme.toml").read_text() == "# mine\n"
    assert not (restored / "theme.toml").read_text().startswith("# mine")
    _, again = paths.restore_theme("default")
    assert again == paths.themes_dir() / "default.bak2"
    with pytest.raises(KeyError):
        paths.restore_theme("nope")


def test_names_resolve_only_in_the_themes_folder(home, tmp_path, monkeypatch) -> None:
    paths.setup()
    elsewhere = tmp_path / "work" / "mine"
    elsewhere.mkdir(parents=True)
    (elsewhere / "theme.toml").write_text(PORTRAIT)
    monkeypatch.chdir(tmp_path / "work")
    search = config.load().theme_paths
    # A name means the themes folder: not the current directory ...
    with pytest.raises(theme_module.ThemeError, match="there are: default"):
        theme_module.find("mine", search)
    # ... while an explicit path runs a theme from anywhere.
    assert theme_module.find("./mine", search) == Path("mine")
    assert theme_module.find(str(elsewhere), search) == elsewhere


def test_save_theme_keeps_the_rest_of_the_file(home) -> None:
    path = paths.config_file()
    path.parent.mkdir(parents=True)
    path.write_text('# my settings\nfps = 20.0\ntheme = "old"  # was\n\n'
                    '[device]\n# theme = "not this one"\nbrightness = 50\n')
    config.save_theme("neon")
    text = path.read_text()
    assert 'theme = "neon"' in text and '"old"' not in text
    assert '# theme = "not this one"' in text and "# my settings" in text
    assert config.load().theme == "neon"
    # No theme line yet: one goes in after the opening comments.
    path.write_text("# hello\n\n[device]\nbrightness = 50\n")
    config.save_theme("neon")
    assert config.load().theme == "neon"
    assert path.read_text().startswith("# hello\n\ntheme")


# -- the socket --


@pytest.fixture
def daemon(home):
    """App on a fake panel, its loop in a thread, serving the socket."""
    paths.setup()
    # Themes of our own: the bundled default has a spectrum, and the
    # machine running the tests may have no cava.
    add_theme("start")
    add_theme("neon")
    add_theme("broken", PORTRAIT + "size = \"huge\"\n")
    cfg = config.load()
    panel = FakePanel()
    app = App(cfg, theme_module.load(paths.themes_dir() / "start"), panel, watch=False)
    app.prepare()
    app.control = control.Server(app.command)
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    yield app
    app.stop()
    thread.join(timeout=3)
    app.control.close()
    app.close()


def test_socket_is_private(daemon) -> None:
    mode = stat.S_IMODE(os.stat(paths.socket_path()).st_mode)
    assert mode == 0o600


def test_status_and_themes(daemon) -> None:
    status = control.request({"cmd": "status"})
    assert status["ok"] and status["theme"] == "start" and status["pid"] == os.getpid()
    themes = control.request({"cmd": "themes"})
    assert themes["themes"] == ["broken", "default", "neon", "start"]
    assert themes["current"] == "start"


def test_switch_theme_and_back(daemon) -> None:
    painted = len(daemon.panel.full)
    reply = control.request({"cmd": "theme", "name": "neon"})
    assert reply == {"ok": True, "theme": "neon", "saved": None}
    assert control.request({"cmd": "status"})["theme"] == "neon"
    assert len(daemon.panel.full) == painted + 1          # the new theme, painted whole
    # The loop picked up the new widgets.
    time.sleep(0.3)
    assert [w.id for w in daemon.widgets] == ["label#1"]
    assert not config.load().theme == "neon"              # not saved unless asked


def test_a_broken_theme_changes_nothing(daemon) -> None:
    reply = control.request({"cmd": "theme", "name": "broken"})
    assert not reply["ok"] and "size" in reply["error"]
    assert "still showing 'start'" in reply["error"]
    assert control.request({"cmd": "status"})["theme"] == "start"
    missing = control.request({"cmd": "theme", "name": "nope"})
    assert not missing["ok"] and "there are:" in missing["error"]


def test_switch_and_save(daemon) -> None:
    reply = control.request({"cmd": "theme", "name": "neon", "save": True})
    assert reply["ok"] and reply["saved"] == str(paths.config_file())
    assert config.load().theme == "neon"


def test_brightness_reload_and_nonsense(daemon) -> None:
    assert control.request({"cmd": "brightness", "value": 40}) == {"ok": True, "brightness": 40}
    assert control.request({"cmd": "status"})["brightness"] == 40
    assert not control.request({"cmd": "brightness", "value": 400})["ok"]
    assert control.request({"cmd": "reload"})["ok"]
    assert "unknown command" in control.request({"cmd": "dance"})["error"]
    # Not JSON at all: answered, and the daemon carries on.
    import socket
    raw = socket.socket(socket.AF_UNIX)
    raw.connect(str(paths.socket_path()))
    raw.sendall(b"hello\n")
    assert b"not a request" in raw.recv(1000)
    raw.close()
    assert control.request({"cmd": "status"})["ok"]


def test_only_one_daemon(daemon) -> None:
    with pytest.raises(control.AlreadyRunning):
        control.Server(lambda r: r)


def test_a_dead_daemons_socket_is_cleared(home) -> None:
    import socket
    path = paths.socket_path()
    leftover = socket.socket(socket.AF_UNIX)
    leftover.bind(str(path))
    leftover.close()                    # bound, never listening: a crash's leftover
    server = control.Server(lambda r: {"ok": True})
    server.close()
    assert not path.exists()


def test_ctl_command(daemon, capsys) -> None:
    assert main(["ctl", "status"]) == 0
    assert capsys.readouterr().out.startswith("start (portrait, ")
    assert main(["ctl", "themes"]) == 0
    assert capsys.readouterr().out.splitlines() == ["  broken", "  default", "  neon", "* start"]
    assert main(["ctl", "theme", "neon"]) == 0
    assert capsys.readouterr().out.strip() == "showing neon"
    assert main(["ctl", "theme", "broken"]) == 1
    assert "still showing 'neon'" in capsys.readouterr().err
    assert main(["ctl", "--json", "brightness", "55"]) == 0
    assert json.loads(capsys.readouterr().out)["brightness"] == 55


def test_ctl_without_a_daemon(home, capsys) -> None:
    assert main(["ctl", "status"]) == 3
    assert "not running" in capsys.readouterr().err


def test_restore_from_the_command_line(home) -> None:
    assert main(["--restore-theme", "default"]) == 0
    assert (paths.themes_dir() / "default" / "theme.toml").is_file()
    assert main(["--restore-theme", "nope"]) == 2


def test_trash_follows_the_freedesktop_layout(home, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    paths.setup()
    for _ in range(2):
        target = add_theme("gone")
        moved = paths.trash(target)
        assert not target.exists() and (moved / "theme.toml").is_file()
    names = sorted(p.name for p in (paths.trash_dir() / "files").iterdir())
    assert names == ["gone", "gone.2"]
    note = (paths.trash_dir() / "info" / "gone.trashinfo").read_text()
    assert note.startswith("[Trash Info]\nPath=/") and "/themes/gone" in note
    assert "DeletionDate=" in note


def test_requests_do_not_cost_a_frame(daemon, monkeypatch) -> None:
    """A request wakes the loop early; the frame must still wait out its
    time. Counted as a frame of its own, each early wake ran the loop's
    schedule ahead of the clock, and it then slept the difference off --
    a stall of several frames in anything moving, every time the editor
    asked the daemon how it was."""
    widget = daemon.widgets[0]
    period = 1.0 / daemon.config.fps
    widget.period = period
    times: list[float] = []
    real = type(widget).render

    def render(self, now: float) -> bool:
        times.append(now)
        return real(self, now)

    monkeypatch.setattr(type(widget), "render", render)
    time.sleep(1.2)                                  # past its old one-second due
    times.clear()
    # The editor's way: a status request now and then, each one a
    # connect, a line and a close -- three early wakes of the loop.
    for _ in range(6):
        control.request({"cmd": "status"})
        time.sleep(0.15)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) >= 20 and max(gaps) < 1.5 * period, \
        f"{len(times)} renders, longest gap {max(gaps) * 1000:.0f} ms"
