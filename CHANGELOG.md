# Changelog

## 1.0.0 (2026-10-16)

First release.

### Daemon

- Themes in `~/.config/akp02d/themes/`, by name; bundled themes are
  copied in on first run and never overwritten (`--restore-theme`).
- Widgets: `label`, `bar`, `gauge` (arc, dial, meter), `graph`,
  `spectrum` (bars, mirrored, segmented, radial) and `image` (still,
  animated, or now-playing cover art).
- Sources: `time`, `cpu`, `memory`, `disk`, `network`, `temperature`,
  `hostname`, `uptime`, `kernel`, `ip`, `now_playing` (MPRIS through
  one shared playerctl) and `volume` (one shared `pactl subscribe`).
- Gradients wherever a colour fills an area, with angle and shape;
  label outline, shadow and glow.
- The spectrum sweeps a peak across its bars after five seconds of
  silence (`when_silent`); `channels` is stereo, mono, left or right.
- Gauges: `start` (bottom or top) and `direction` (clockwise or
  counterclockwise) of the scale, zones, ticks, numbers, needle glide.
- `akp02d ctl` and a control socket: status, list and switch themes,
  brightness, reload. One daemon per user.
- The theme reloads when its files change; a theme that fails to load
  is refused and the panel keeps what it had.
- Colour-safe placement is checked at load, with the nearest valid
  position in the error.

### Theme editor (`akp02d-edit`)

- A live preview running the daemon's own widgets with simulated
  readings, music and covers; no panel needed.
- Drag widgets in from Add widget, move and resize with colour-safe
  snapping, no overlaps.
- Every option shown with its default and description; dropdowns of
  this machine's sensors, disks, interfaces, players and audio devices.
- A themes list (open, duplicate, rename, delete to trash), Apply to
  panel, and saves that keep the file's comments and order and write
  `preview.png`.
