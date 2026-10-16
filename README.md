# akp02d

Themed system stats for the Ajazz AKP02 panel: clocks, gauges, bars,
graphs, an audio spectrum, cover art and more, drawn from a theme file
you can edit by hand or in the live theme editor. Built on the
[`akp02`](https://github.com/deonspengler/akp02) library.

<img src="https://raw.githubusercontent.com/deonspengler/akp02d/main/docs/panel.png" width="260" alt="The panel showing a clock, memory and disk gauges, network and CPU graphs, and an audio spectrum over a wallpaper">

## Install

Linux and Python 3.11 or newer.

    pip install akp02d               # the daemon
    pip install 'akp02d[editor]'     # ... and the theme editor (PySide6)

or from this repository: `pip install 'akp02d[editor] @ git+https://github.com/deonspengler/akp02d'`.
On Python 3.11 the editor needs PySide6 older than 6.12 (6.12 crashes
there); the `editor` extra picks one.

Your user needs access to the panel's hidraw device; see
[akp02](https://github.com/deonspengler/akp02) for the udev rule. A few
features use programs you may already have:

| for | needs |
|---|---|
| the spectrum widget | `cava` |
| the `now_playing` source | `playerctl` |
| the `volume` source | `pactl` (libpulse; works with PipeWire too) |

## Run

    akp02d --check        # validate the config and theme; no panel needed
    akp02d                # run
    akp02d --stats -v     # run, logging timing every 5 s

The first run creates everything akp02d reads:

    ~/.config/akp02d/akp02d.toml      settings for this machine
    ~/.config/akp02d/themes/<name>/   themes, by name

(`~/.config` is `$XDG_CONFIG_HOME` if set.) The bundled themes are
copied in when missing and never overwritten; `akp02d --restore-theme
default` puts the original back and keeps yours as `default.bak`. A
theme name (`theme = "neon"` in akp02d.toml, or `-t neon`) is a folder
in `themes/`; a path (`-t ~/work/neon`) runs a theme from anywhere.

`akp02d.toml` holds what belongs to the machine, not the theme: `theme`
and `fps`, and under `[device]`: `brightness`, `inverted` (the panel is
mounted upside down), `clear_on_exit`, `jpeg_quality` and
`jpeg_subsampling`.

To start it with your session, as a systemd user service:

    cp contrib/akp02d.service ~/.config/systemd/user/
    systemctl --user enable --now akp02d

### While it runs

    akp02d ctl status              # theme, fps, brightness, uptime
    akp02d ctl themes
    akp02d ctl theme neon          # switch now
    akp02d ctl theme neon --save   # ... and start with it from now on
    akp02d ctl brightness 40
    akp02d ctl reload

A new theme is fully loaded before the panel changes, so one with a
mistake is refused, with the reason, and the panel keeps what it had.
The daemon also watches its theme: save `theme.toml` or replace an
image and the panel updates within a second (`--no-watch` turns this
off).

`akp02d ctl` talks to the daemon over an owner-only Unix socket,
`$XDG_RUNTIME_DIR/akp02d.sock`, one JSON object per line, so scripts
can too:

    echo '{"cmd": "theme", "name": "neon"}' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/akp02d.sock

## Theme editor

    akp02d-edit                # the theme in akp02d.toml
    akp02d-edit neon           # a theme by name, or a folder

A live preview that needs no panel: it runs akp02d's own widgets, so
what you see is what the panel will show. Readings are simulated
(moving CPU, network, music, a playlist with covers), so a layout can
be built on any machine.

* Drag widgets in from **Add widget**, or click to place one in a free
  spot. Drag to move, drag the handles to resize. Boxes snap to
  colour-safe positions and cannot overlap (hold Alt to drop one over
  another for a moment).
* Every option of the selected widget is listed with its default and a
  description. Options that do nothing in the current setup are hidden;
  ones that name something on this machine (a sensor, disk, network
  interface, player or audio device) are dropdowns of what it has.
* **Themes** lists the themes folder. One click opens a theme; a cyan
  dot marks the one on the panel. Duplicate, rename and delete (to the
  trash) are beside the heading and on the right-click menu.
* **Save** edits `theme.toml` in place, keeping your comments and
  order, and writes `preview.png` beside it. **Apply to panel** saves,
  switches the running daemon to this theme and makes it the start
  theme.

## Themes

A theme is a folder with a `theme.toml` and any files it names. Each
widget is a `[[widget.<plugin>]]` entry: `x`, `y`, `width`, `height`,
`fps` and `id` are reserved, the rest are the plugin's.

```toml
[theme]
orientation = "portrait"          # or "landscape"
background = "#0d0a15"            # a colour, or a list for a gradient
wallpaper = "panel.jpg"           # optional, over the background
font = "DejaVuSans-Bold.ttf"      # a file in the theme, or a system font

[[widget.label]]
x = 80
y = 90
width = 302
height = 46
fps = 1
source = "time"
format = "{value:%H:%M:%S}"
size = 56
color = "#11e3f5"

[[widget.gauge]]
x = 56
y = 190
width = 160
height = 160
source = "memory"
label = "MEM"
format = "{used_gb:.0f}/{total_gb:.0f}G"
gradient = ["#22dd66", "#ffd022", "#ff3b30"]
```

* `fps` is how often a widget reads its source and redraws (`1` is
  once a second). A widget is only sent to the panel when it changed.
* `format` is a Python format string filled from the source's fields,
  so `"{value:%H:%M}"` and `"CPU {percent:.0f}%"` work the same way. A
  label without a source shows its format as written.
* Colours are `#rrggbb`, `#rgb` or CSS names. Wherever a gradient is
  allowed, a list of colours is one. Gradients painted across an area
  (backgrounds, a label's `color`, a graph's `fill`) also take
  `<option>_shape` (`linear` or `radial`) and `<option>_angle` (degrees
  as in CSS: 0 bottom to top, 90 left to right, 180 top to bottom).
* Any widget can lay a `background` over the wallpaper at `alpha`
  opacity.

Mistakes fail at load with the full path, never silently:

    unknown key(s) in widget.label[0]: colour
    widget 'graph#1': source 'time' produces a moment, but this widget needs scalar or a pair

### Widgets

| widget | shows |
|---|---|
| `label` | one line of text: `fit` (`clip`, `ellipsis`, `shrink`), `locale`, `outline`, `shadow` (a glow with no offset), gradient `color` |
| `bar` | a filling bar, horizontal or vertical; `segments` for an LED look, `radius`, label and value `inside` or `above` |
| `gauge` | `style = "arc"`, `"dial"` (car style) or `"meter"` (analog); `sweep`, `start`, `direction`, `zones`, `ticks`, `needle`, `glide` |
| `graph` | recent history as a scrolling trace; a pair (network) mirrors about a centre line, on a log scale |
| `spectrum` | audio from cava: `type = "bars"`, `"mirrored"`, `"segmented"` or `"radial"`; `channels`, `peaks`, `when_silent` |
| `image` | a picture from the theme folder, animated GIF/WebP/PNG too; or `source = "now_playing"` for cover art, with `path` as the fallback |

The editor lists every option with its default and description.

```toml
[[widget.spectrum]]
x = 72
y = 772
width = 318
height = 318
fps = 30
type = "radial"
bars = 48
channels = "stereo"      # stereo (mirrored, lows in the middle), mono, left or right
gradient = ["#fd44dd", "#11e3f5"]
smoothing = 30           # 0 twitchy, 100 sluggish
when_silent = "sweep"    # after 5 s without sound, a peak sweeps until it returns

[[widget.image]]
x = 16
y = 16
width = 120
height = 120
source = "now_playing"   # the current track's cover
path = "no-cover.png"    # shown while nothing is playing
fit = "cover"            # contain, cover, stretch, none
radius = 12
```

### Sources

| source | reading | fields for `format` |
|---|---|---|
| `time` | a moment | `value` (`{value:%H:%M}`) |
| `cpu` | 0..1 | `value`, `percent` |
| `memory` | 0..1 | `percent`, `used_gb`, `total_gb`, `used`, `available` (bytes) |
| `disk` | 0..1 | `percent`, `used_gb`, `free_gb`, `total_gb`, ... (option `path`) |
| `network` | up/down pair | `up`, `down`, `up_kb`, `down_mb`, ... (option `interface`) |
| `temperature` | degrees | `celsius`, `fahrenheit` (options `sensor`, `sensor_label`) |
| `hostname` | text | `value`, `short` |
| `uptime` | text | `value` (`3d 4h 12m`), `days`, `hours`, `minutes` |
| `kernel` | text | `value`, `version`, `machine` |
| `ip` | text | `value` (option `interface`) |
| `now_playing` | track progress | `title`, `artist`, `album`, `position`, `length`, `remaining`, `status`, `volume`, ... (options `player`, `idle`) |
| `volume` | 0..1, 0 when muted | `text` (`65%` or `muted`), `percent`, `muted`, `device` (options `device`, `input`) |

Gauges, bars and graphs draw the numeric ones; labels show anything,
and a label with a source but no `format` uses the source's usual one.
`now_playing` follows any MPRIS player through one shared `playerctl`
process; `volume` follows the sound server through one shared
`pactl subscribe`, so neither polls.

### Colour-safe placement

The panel shows a region in the right colours only when its offset
along the 462-pixel side is a multiple of 8 (counted from the far edge
when that side is reflected; in landscape, right way up, that means
`(y + height) % 8 == 6`). akp02d checks this at load and names the
nearest valid position; the editor snaps to it.

    widget 'label#2': y=20 with height=60 is not colour-safe;
    nearest colour-safe y for this size: 18 or 26

## Writing a plugin

A plugin is a module in `akp02d/plugins/` with a `Widget` subclass
decorated with `@register`, imported in `plugins/__init__.py`. It
declares its options as a `Schema`, which gives it type and range
checks and gives the editor its form:

```python
from ..schema import Schema, color, integer, source
from .base import Widget, register

@register("dots")
class DotsWidget(Widget):
    default_fps = 1.0
    summary = "A reading as a row of dots"
    schema = Schema(
        source(accepts=("scalar",), required=True),
        integer("count", 10, 1, 64),
        color("color", "#ffffff"),
    )

    def __init__(self, spec, theme):
        super().__init__(spec, theme)
        o = self.schema.read(spec.options, spec)
        spec.options.reject_unknown()
        ...

    def render(self, now: float) -> bool:
        ...  # draw into self.surface; return True only if it changed
```

`render()` returning `False` costs nothing: no encode, no USB transfer.
`schema.py` documents `when=` (hide an option the others make
irrelevant) and `Auto(...)` (a default computed from other options).

## Licence

GNU General Public License v3.0. See [LICENSE](LICENSE).
