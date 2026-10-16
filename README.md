# akp02d

[![AUR](https://img.shields.io/aur/version/akp02d)](https://aur.archlinux.org/packages/akp02d)
[![PyPI](https://img.shields.io/pypi/v/akp02d)](https://pypi.org/project/akp02d/)
[![Python](https://img.shields.io/pypi/pyversions/akp02d)](https://pypi.org/project/akp02d/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)

Turns the Ajazz AKP02 panel into a live system display: clock, CPU,
memory, disk, network, temperatures, an audio visualiser, what's
playing and more, in themes you design with a drag-and-drop editor.

<img src="https://raw.githubusercontent.com/deonspengler/akp02d/main/docs/editor.png" width="800" alt="The akp02d theme editor: layers, a live preview of the panel, and the selected widget's settings">

## Install

From the AUR ([akp02d](https://aur.archlinux.org/packages/akp02d)):

    yay -S akp02d

### Other distributions

With [pipx](https://pipx.pypa.io) and Python 3.13 or newer (add
`--python 3.13` if your system Python is older):

    pipx install 'akp02d[editor]'

Then install `cava` from your distribution (and `playerctl` and
`pactl` if you want them), and let your user open the panel:

    sudo cp "$(akp02d --udev-rule)" /etc/udev/rules.d/
    sudo udevadm control --reload-rules

Unplug the panel and plug it back in. To start akp02d with your
session:

    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/akp02d.service <<'EOF'
    [Unit]
    Description=akp02d

    [Service]
    ExecStart=%h/.local/bin/akp02d
    Restart=on-failure
    RestartSec=5

    [Install]
    WantedBy=default.target
    EOF

## Start it

    systemctl --user enable --now akp02d

The panel lights up with the default theme and starts again with your
session. To run it by hand instead, just run `akp02d`.

## Make it yours

    akp02d-edit

The editor shows a live preview of the panel, no panel needed. Drag
widgets in from **Add widget**, move and resize them, and pick colours,
fonts and data sources on the right. **Apply to panel** puts your
theme on the panel straight away.

Themes live in `~/.config/akp02d/themes/`, one folder each, so they are
easy to back up or share. The bundled ones are copied there on first
run and never overwritten; `akp02d --restore-theme default` brings back
the original, keeping yours as `default.bak`.

## Control it

    akp02d ctl status              # what's showing
    akp02d ctl themes              # your themes
    akp02d ctl theme neon --save   # switch, and keep it after a restart
    akp02d ctl brightness 60       # 0-100

Settings for this machine, such as brightness and an upside-down panel
(`inverted`), are in `~/.config/akp02d/akp02d.toml`.

## If something is wrong

    akp02d --check                 # checks the settings and theme, and says what is wrong
    journalctl --user -u akp02d    # the service's log

If akp02d cannot open the panel, unplug it and plug it back in: the
access rule installed with akp02d applies to newly connected devices.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
