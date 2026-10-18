# akp02d

[![AUR](https://img.shields.io/aur/version/akp02d)](https://aur.archlinux.org/packages/akp02d)
[![PyPI](https://img.shields.io/pypi/v/akp02d)](https://pypi.org/project/akp02d/)
[![Python](https://img.shields.io/pypi/pyversions/akp02d)](https://pypi.org/project/akp02d/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)

A live system monitor for the Ajazz AKP02 Universal Secondary Screen: clock, CPU, memory,
disk, network, temperatures, audio visualiser, and what's currently playing —
all arranged in themes you design yourself.

<img src="https://raw.githubusercontent.com/deonspengler/akp02d/main/docs/editor.png" width="800" alt="The akp02d theme editor: layers, a live preview of the panel, and the selected widget's settings">

## Install

**Arch (AUR):**

    yay -S akp02d

**Any other distribution (Python ≥ 3.13):**

    pipx install 'akp02d[editor]'

Then install `cava` from your distro, plus `playerctl` and `pactl` if you
want them. Grant your user access to the panel:

    sudo cp "$(akp02d --udev-rule)" /etc/udev/rules.d/
    sudo udevadm control --reload-rules

Unplug the panel and plug it back in.

## Run

To start akp02d automatically with your session:

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

    systemctl --user enable --now akp02d

Or run it directly:

    akp02d

## Edit themes

    akp02d-edit

The editor shows a live preview of the panel — no hardware required. Drag
widgets in from **Add widget**, move and resize them, and configure colours,
fonts, and data sources on the right. **Apply to panel** pushes your theme
to the panel immediately.

Themes live in `~/.config/akp02d/themes/`, one folder per theme, so they're
easy to back up or share. Bundled themes are copied there on first run and
never overwritten; `akp02d --restore-theme default` brings back the original,
keeping yours as `default.bak`.

## CLI

    akp02d ctl status              # what's showing
    akp02d ctl themes              # list your themes
    akp02d ctl theme neon --save   # switch, and keep it after a restart
    akp02d ctl brightness 60       # 0-100

Machine-specific settings (brightness, inverted display, etc.) live in
`~/.config/akp02d/akp02d.toml`.

## Troubleshooting

    akp02d --check                 # validate settings and theme
    journalctl --user -u akp02d    # view the service log

If akp02d can't open the panel, unplug and replug it: the udev rule only
applies to newly connected devices.

## AI usage

The AKP02 protocol was reverse-engineered and the device handling written by
hand. AI assisted with some GUI and boilerplate code and the test suites;
everything was reviewed. akp02d is tested on real hardware. The default
theme's wallpaper is AI-generated.

## Licence

[GPL-3.0-or-later](LICENSE).
