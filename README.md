# akp02d

Turns the Ajazz AKP02 panel into a live system display: clock, CPU,
memory, disk, network, temperatures, an audio visualiser, what's
playing and more, in themes you design with a drag-and-drop editor.

<img src="https://raw.githubusercontent.com/deonspengler/akp02d/main/docs/panel.png" width="260" alt="The panel showing a clock, memory and disk gauges, network and CPU graphs, and an audio visualiser over a wallpaper">

## Install

From the AUR ([akp02d](https://aur.archlinux.org/packages/akp02d)):

    yay -S akp02d

Optional, for the features that use them:

| package | for |
|---|---|
| `pyside6` | the theme editor, `akp02d-edit` |
| `cava` | the audio visualiser |
| `playerctl` | what's playing, and cover art |
| `libpulse` | the volume level (works with PipeWire too) |

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

If akp02d cannot open the panel, your user needs access to it: see
[akp02](https://github.com/deonspengler/akp02) for the udev rule.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
