"""Sources: where a widget's number or text comes from.

A source is owned by the widget that names it and read on that widget's
own schedule, so `fps` keeps meaning what it says -- how often this
widget updates. Two widgets on the same source read it twice, which
costs about 18us for cpu and 35us for memory: not worth infrastructure
to avoid, and it keeps a widget's rate a property of the widget.

That reasoning is specific to cheap local reads. A source that costs an
API call or holds a connection will need sharing, and can grow it then.

Each read produces a `value` in 0..1 for shapes to draw, and `fields`
for text to format. Sources that are not numbers (time) leave value at
None and put the useful thing in fields.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .schema import Schema, string
from .table import SettingsError, Table

# What a source produces, and therefore which shapes can draw it.
SCALAR, TEXT, MOMENT, PAIR = "scalar", "text", "moment", "pair"


class SourceError(SettingsError):
    """A source is unknown, misconfigured, or unavailable."""


class Source:
    """Base class. Subclasses implement read().

    Constructed with the widget's own options table, so a source that
    needs configuring can read its own keys from it -- they sit beside
    the widget's, and reject_unknown still catches a typo in either.
    """

    kind = SCALAR
    # Options of the source's own, read from the widget's table. Declared
    # so an editor can show them beside the widget's.
    schema = Schema()
    # The names a format string can use, for editors and error messages.
    fields: tuple[str, ...] = ()
    # One line on what it reads, for editors.
    summary = ""
    # The range a widget draws `value` against unless the theme sets its
    # own min and max. Fractions by default; a temperature is degrees.
    range: tuple[float, float] = (0.0, 1.0)
    # Ready-made format strings, the first used when a label names the
    # source but no format. Offered by editors as suggestions.
    formats: tuple[str, ...] = ("{value}",)

    def __init__(self, options: Table, widget: str) -> None:
        self.schema.read(options)

    def close(self) -> None:
        """Let go of anything held open. Most sources hold nothing."""

    def read(self, now: float) -> tuple[float | None, dict[str, Any]]:
        """Return (value in 0..1 or None, fields for formatting).

        A pair source leaves value None and puts `up` and `down` in
        fields: the two are read together, from one sample, because
        reading them separately would straddle a delta boundary and let
        them disagree about the same instant.
        """
        raise NotImplementedError


class TimeSource(Source):
    """The current local time. Formatted, not drawn."""

    kind = MOMENT
    fields = ("value",)
    summary = "The local time; format it with strftime codes, e.g. {value:%H:%M}"
    formats = ("{value:%H:%M:%S}", "{value:%H:%M}", "{value:%I:%M %p}", "{value:%a, %d %b %Y}", "{value:%A}", "{value:%d/%m/%Y}",)

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        return None, {"value": datetime.now()}


class CpuSource(Source):
    """Busy fraction since this source's previous read.

    A delta, not a reading, so it keeps its own previous snapshot. The
    first read happens at construction, so the first frame already has a
    baseline to measure against rather than reporting zero.
    """

    kind = SCALAR
    fields = ("value", "percent")
    summary = "Busy fraction of all CPUs since the last read"
    formats = ("{percent:.0f}%", "CPU {percent:.0f}%",)

    def __init__(self, options: Table, widget: str) -> None:
        self._previous = _cpu_times()

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        idle, total = _cpu_times()
        idle_delta = idle - self._previous[0]
        total_delta = total - self._previous[1]
        self._previous = (idle, total)
        busy = 1.0 - idle_delta / total_delta if total_delta > 0 else 0.0
        busy = min(1.0, max(0.0, busy))
        return busy, {"value": busy, "percent": busy * 100}


class MemorySource(Source):
    """Used fraction of physical memory.

    Used is total minus MemAvailable, which is the kernel's own estimate
    of what a new process could claim -- closer to what a person means
    by "used" than total minus free, which counts cache as used.
    """

    kind = SCALAR
    fields = ("value", "percent", "used", "total", "available", "used_gb",
              "total_gb")
    summary = "Used fraction of physical memory"
    formats = ("{percent:.0f}%", "{used_gb:.1f}/{total_gb:.0f}G", "{used_gb:.1f}G used",)

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        info = _meminfo()
        total = info["MemTotal"]
        available = info.get("MemAvailable", info["MemFree"])
        used = total - available
        fraction = used / total if total else 0.0
        return fraction, {
            "value": fraction,
            "percent": fraction * 100,
            "used": used,
            "total": total,
            "available": available,
            "used_gb": used / 2**30,
            "total_gb": total / 2**30,
        }


class DiskSource(Source):
    """Used fraction of a filesystem.

    Reads `path`, which is any location on the filesystem of interest --
    a mount point, or anything under it.
    """

    kind = SCALAR
    schema = Schema(
        string("path", "/", group="Data",
               doc="Any path on the filesystem to measure: a mount point or "
                   "anything under it",
               suggest=lambda values: mount_choices()),
    )
    fields = ("value", "percent", "used", "free", "total", "used_gb",
              "free_gb", "total_gb")
    summary = "Used fraction of a filesystem"
    formats = ("{percent:.0f}%", "{free_gb:.0f}G free", "{used_gb:.0f}/{total_gb:.0f}G",)

    def __init__(self, options: Table, widget: str) -> None:
        self._path = self.schema.read(options).path
        try:
            os.statvfs(self._path)
        except OSError as exc:
            raise SourceError(
                f"widget {widget!r}: cannot read {self._path!r}: {exc}"
            ) from exc

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        info = os.statvfs(self._path)
        total = info.f_blocks * info.f_frsize
        free = info.f_bavail * info.f_frsize
        used = total - free
        fraction = used / total if total else 0.0
        return fraction, {
            "value": fraction,
            "percent": fraction * 100,
            "used": used,
            "free": free,
            "total": total,
            "used_gb": used / 2**30,
            "free_gb": free / 2**30,
            "total_gb": total / 2**30,
        }


class NetworkSource(Source):
    """Bytes per second in each direction since this source's last read.

    Reads `interface`; the default sums every interface except loopback,
    which is what "how busy is my network" usually means. Rates are
    unbounded, so a graph showing this wants a log scale.
    """

    kind = PAIR
    schema = Schema(
        string("interface", None, group="Data",
               doc="One interface, e.g. eno1; leave empty to sum every "
                   "interface except loopback",
               suggest=lambda values: interface_choices()),
    )
    fields = ("up", "down", "up_kb", "down_kb", "up_mb", "down_mb")
    summary = "Bytes per second sent (up) and received (down)"
    formats = ("↓{down_mb:.1f} ↑{up_mb:.1f} MB/s", "↓{down_kb:.0f} KB/s", "↑{up_kb:.0f} KB/s",)

    def __init__(self, options: Table, widget: str) -> None:
        self._name = self.schema.read(options).interface
        self._previous = _net_bytes(self._name)
        self._when: float | None = None
        if self._name is not None and self._previous == (0, 0):
            known = ", ".join(sorted(_net_interfaces())) or "none"
            raise SourceError(
                f"widget {widget!r}: no interface {self._name!r}; found: {known}"
            )

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        received, sent = _net_bytes(self._name)
        elapsed = 0.0 if self._when is None else now - self._when
        self._when = now
        if elapsed <= 0:
            down = up = 0.0
        else:
            down = max(0.0, (received - self._previous[0]) / elapsed)
            up = max(0.0, (sent - self._previous[1]) / elapsed)
        self._previous = (received, sent)
        return None, {
            "up": up,
            "down": down,
            "up_kb": up / 1024,
            "down_kb": down / 1024,
            "up_mb": up / 2**20,
            "down_mb": down / 2**20,
        }


class TemperatureSource(Source):
    """A temperature sensor, in degrees Celsius.

    Read from what the kernel already exposes: hwmon chips first (CPU
    packages, GPUs, NVMe drives), then thermal zones, so no extra
    program is needed. With no `sensor` named it picks the CPU -- the
    first of the usual CPU drivers present -- which is what "the
    temperature" usually means.

    `value` is in degrees, not 0..1: what counts as hot is a display
    decision, so a gauge or graph sets its own min and max (20 and 100,
    say) the way a graph does for network rates.
    """

    kind = SCALAR
    schema = Schema(
        string("sensor", None, group="Data",
               doc="Sensor chip, e.g. k10temp, coretemp, amdgpu, nvme; "
                   "empty picks the CPU",
               suggest=lambda values: sensor_choices()),
        string("sensor_label", None, group="Data",
               doc="One reading of that chip, e.g. Tctl or 'Package id 0'; "
                   "empty takes its main one",
               suggest=lambda values: sensor_label_choices(values.get("sensor"))),
    )
    fields = ("value", "celsius", "fahrenheit", "sensor", "sensor_label")
    range = (20.0, 100.0)
    summary = "A temperature sensor in degrees C (CPU unless told otherwise)"
    formats = ("{celsius:.0f}°C", "{celsius:.1f}°C", "{fahrenheit:.0f}°F", "{sensor} {celsius:.0f}°C",)

    def __init__(self, options: Table, widget: str) -> None:
        o = self.schema.read(options)
        found = _temperature_file(o.sensor, o.sensor_label)
        self.unavailable: str | None = None
        if found is None:
            if o.sensor is None and o.sensor_label is None:
                wanted = "no CPU temperature sensor"
            else:
                wanted = "no temperature sensor " + " ".join(
                    part for part in (o.sensor, repr(o.sensor_label) if o.sensor_label else None)
                    if part)
            known = ", ".join(
                f"{chip}/{label}" for chip, label, _ in _temperature_sensors()
            ) or "none"
            message = f"widget {widget!r}: {wanted}; found: {known}"
            if wrap is None:
                raise SourceError(message)
            # A tool standing in for readings (an editor on a machine
            # without this sensor) can still draw it; it says why.
            self.unavailable = message
            self._file, self._chip, self._label = None, o.sensor or "", o.sensor_label or ""
            return
        self._chip, self._label, self._file = found
        self.read(0.0)  # fail now, not on the first frame, if unreadable

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        if self._file is None:
            celsius = 0.0
        else:
            with open(self._file, "rb") as handle:
                celsius = int(handle.read().strip()) / 1000
        return celsius, temperature_fields(celsius, self._chip, self._label)


class HostnameSource(Source):
    """This machine's name."""

    kind = TEXT
    fields = ("value", "short")
    summary = "This machine's name; {short} drops the domain"
    formats = ("{short}", "{value}",)

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        name = socket.gethostname()
        return None, {"value": name, "short": name.split(".")[0]}


class UptimeSource(Source):
    """How long since boot.

    `value` is the compact form people write -- "3d 4h 12m", "4h 12m",
    "12m" -- and the parts are there for any other layout.
    """

    kind = TEXT
    fields = ("value", "days", "hours", "minutes", "seconds", "total_hours")
    summary = "Time since boot, e.g. 3d 4h 12m"
    formats = ("{value}", "up {value}", "{days}d {hours}h", "{total_hours}h",)

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        with open("/proc/uptime", "rb") as handle:
            seconds = int(float(handle.read().split()[0]))
        return None, uptime_fields(seconds)


def uptime_fields(seconds: int) -> dict[str, Any]:
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = ([f"{days}d"] if days else []) + ([f"{hours}h"] if days or hours else [])
    return {
        "value": " ".join(parts + [f"{minutes}m"]),
        "days": days,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds,
        "total_hours": seconds // 3600,
    }


class KernelSource(Source):
    """The running kernel."""

    kind = TEXT
    fields = ("value", "release", "version", "machine", "system")
    summary = "The running kernel's release, e.g. 6.11.2-arch1-1"
    formats = ("{value}", "{system} {release}",)

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        info = os.uname()
        return None, {
            "value": info.release,
            "release": info.release,
            "version": info.version,
            "machine": info.machine,
            "system": info.sysname,
        }


class IpSource(Source):
    """This machine's local IPv4 address.

    With no `interface`, the address of the one that carries the default
    route -- the address other machines on the network reach this one
    at. Found by asking the kernel which address it would send from; no
    packet is sent. With no network, the value is `no_address`.
    """

    kind = TEXT
    schema = Schema(
        string("interface", None, group="Data",
               doc="One interface, e.g. eno1; empty for the one with the "
                   "default route",
               suggest=lambda values: interface_choices()),
        string("no_address", "offline", group="Data",
               doc="What to show when there is no address"),
    )
    fields = ("value", "interface")
    summary = "Local IPv4 address, of the default route or a named interface"
    formats = ("{value}",)

    def __init__(self, options: Table, widget: str) -> None:
        o = self.schema.read(options)
        self._interface = o.interface
        self._missing = o.no_address
        if self._interface is not None:
            known = _net_interfaces()
            if self._interface not in known:
                raise SourceError(
                    f"widget {widget!r}: no interface {self._interface!r}; found: "
                    f"{', '.join(sorted(known)) or 'none'}")

    def read(self, now: float) -> tuple[None, dict[str, Any]]:
        address = (_interface_address(self._interface) if self._interface
                   else _default_address())
        return None, {"value": address or self._missing,
                      "interface": self._interface or ""}


def _default_address() -> str | None:
    """The source address for the default route, without sending."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed anywhere real
            address = probe.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("0.") else address


def _interface_address(name: str) -> str | None:
    """An interface's IPv4 address, via the SIOCGIFADDR ioctl."""
    import fcntl
    import struct

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            packed = fcntl.ioctl(probe.fileno(), 0x8915,  # SIOCGIFADDR
                                 struct.pack("256s", name.encode()[:15]))
    except OSError:
        return None
    return socket.inet_ntoa(packed[20:24])


class MediaSource(Source):
    """What a media player is playing, via playerctl.

    Every widget following the same player shares one playerctl process
    (see media.py), so a title, an artist and a progress bar cost one
    child between them. `value` is the track's progress, 0..1, for a bar
    or gauge; the rest are fields for labels.
    """

    kind = SCALAR
    schema = Schema(
        string("player", None, group="Data",
               doc="Which player to follow, e.g. spotify, mpv, firefox; empty "
                   "follows whichever is active",
               suggest=lambda values: [(name, name) for name in _media().players()]),
        string("idle", "nothing playing", group="Data",
               doc="The title shown when nothing is playing"),
    )
    fields = ("title", "artist", "album", "status", "player", "position", "length",
              "remaining", "position_s", "length_s", "value", "percent")
    formats = ("{title}", "{artist} · {title}", "{artist}", "{album}",
               "{position} / {length}", "{remaining}", "{status}")
    summary = "What a media player is playing (needs playerctl)"

    def __init__(self, options: Table, widget: str) -> None:
        o = self.schema.read(options)
        self._player = o.player
        self._idle = o.idle
        self._stream = None
        self.unavailable: str | None = None
        if not _media().installed():
            message = (f"widget {widget!r}: playerctl is not installed "
                       f"(pacman -S playerctl)")
            if wrap is None:
                raise SourceError(message)
            self.unavailable = message

    def _may_start(self) -> bool:
        if self.unavailable or wrap is not None:
            return False   # a tool is standing in: no child process
        from .plugins.base import dry_run
        return not dry_run()   # --check validates, it does not start things

    def read(self, now: float) -> tuple[float, dict[str, Any]]:
        media = _media()
        if self._stream is None and self._may_start():
            self._stream = media.acquire(self._player)
        track = self._stream.read(now) if self._stream is not None else media.Track(at=now)
        fields = media_fields(track, now, self._idle)
        return fields["value"], fields

    def close(self) -> None:
        if self._stream is not None:
            _media().release(self._stream)
            self._stream = None


def _media():
    from . import media
    return media


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def media_fields(track, now: float, idle: str) -> dict[str, Any]:
    """Format fields for a track, as of `now`."""
    position = track.position_now(now)
    length = track.length
    progress = min(1.0, position / length) if length else 0.0
    nothing = track.status == "Stopped" or not (track.title or track.artist)
    return {
        "title": idle if nothing else track.title,
        "artist": "" if nothing else track.artist,
        "album": "" if nothing else track.album,
        "status": track.status,
        "player": track.player,
        "position": _clock(position) if not nothing else "",
        "length": _clock(length) if length and not nothing else "",
        "remaining": f"-{_clock(max(0.0, length - position))}" if length and not nothing else "",
        "position_s": position,
        "length_s": length,
        "value": 0.0 if nothing else progress,
        "percent": 0.0 if nothing else progress * 100,
    }


def temperature_fields(celsius: float, chip: str, label: str) -> dict[str, Any]:
    return {
        "value": celsius,
        "celsius": celsius,
        "fahrenheit": celsius * 9 / 5 + 32,
        "sensor": chip,
        "sensor_label": label,
    }


# Where the kernel publishes sensors; module-level so tests can point
# them at a fake tree.
HWMON = Path("/sys/class/hwmon")
THERMAL = Path("/sys/class/thermal")

# The usual CPU drivers, in the order to prefer them, and the reading of
# each that means "the package" rather than one core.
_CPU_CHIPS = ("k10temp", "zenpower", "coretemp", "cpu_thermal", "x86_pkg_temp",
              "acpitz")
_MAIN_LABELS = ("Tctl", "Tdie", "Package id 0", "edge", "Composite")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _temperature_sensors() -> list[tuple[str, str, Path]]:
    """(chip, label, file) for every temperature reading present."""
    out: list[tuple[str, str, Path]] = []
    try:
        chips = sorted(HWMON.glob("hwmon*"), key=lambda p: (len(p.name), p.name))
    except OSError:
        chips = []
    for chip in chips:
        name = _read_text(chip / "name") or chip.name
        for reading in sorted(chip.glob("temp*_input"),
                              key=lambda p: (len(p.name), p.name)):
            label = _read_text(reading.with_name(reading.name.replace("_input", "_label")))
            out.append((name, label or reading.name.removesuffix("_input"), reading))
    try:
        zones = sorted(THERMAL.glob("thermal_zone*"), key=lambda p: (len(p.name), p.name))
    except OSError:
        zones = []
    for zone in zones:
        if (zone / "temp").exists():
            out.append((_read_text(zone / "type") or zone.name, zone.name, zone / "temp"))
    return out


def _temperature_file(sensor: str | None, label: str | None) -> tuple[str, str, Path] | None:
    """The reading a theme asked for, or the CPU's if it named none."""
    found = _temperature_sensors()
    if sensor is not None:
        found = [f for f in found if f[0].lower() == sensor.lower()]
    else:
        ranked = sorted(
            (f for f in found if f[0] in _CPU_CHIPS),
            key=lambda f: _CPU_CHIPS.index(f[0]),
        )
        found = ranked or found
    if label is not None:
        found = [f for f in found if f[1].lower() == label.lower()]
    if not found:
        return None
    main = [f for f in found if f[1] in _MAIN_LABELS]
    return (main or found)[0]


# -- what this machine offers, for tools to suggest --


def sensor_choices() -> list[tuple[str, str]]:
    """Each temperature chip once, with its main reading now."""
    out: dict[str, str] = {}
    for chip, label, file in _temperature_sensors():
        if chip in out:
            continue
        main = _temperature_file(chip, None)
        reading = _celsius(main[2]) if main else None
        out[chip] = f"{chip}  ({reading:.0f} °C)" if reading is not None else chip
    return list(out.items())


def sensor_label_choices(sensor: str | None) -> list[tuple[str, str]]:
    """The readings of one chip -- the CPU's when none is named."""
    chosen = _temperature_file(sensor, None)
    if chosen is None:
        return []
    out = []
    for chip, label, file in _temperature_sensors():
        if chip == chosen[0]:
            reading = _celsius(file)
            out.append((label, f"{label}  ({reading:.0f} °C)" if reading is not None else label))
    return out


def _celsius(file: Path) -> float | None:
    try:
        return int(file.read_text().strip()) / 1000
    except (OSError, ValueError):
        return None


# Filesystems that hold files someone would want to watch fill up, as
# opposed to the kernel's own (proc, sysfs, cgroup ...) and RAM ones.
_DISK_TYPES = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "zfs", "vfat",
               "exfat", "ntfs", "ntfs3", "fuseblk", "jfs", "reiserfs", "bcachefs",
               "nfs", "nfs4", "cifs", "smb3"}


def mount_choices() -> list[tuple[str, str]]:
    """Mounted filesystems worth a disk widget, with type and size."""
    out: dict[str, str] = {}
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or parts[2] not in _DISK_TYPES:
            continue
        # /proc/mounts escapes spaces and the like as octal.
        mount = parts[1].encode().decode("unicode_escape")
        if mount in out:
            continue
        try:
            info = os.statvfs(mount)
        except OSError:
            continue
        total = info.f_blocks * info.f_frsize / 2**30
        shown = f"{total:.1f}" if total < 10 else f"{total:.0f}"
        out[mount] = f"{mount}  ({parts[2]}, {shown} G)"
    if "/" not in out:
        out = {"/": "/", **out}
    return sorted(out.items(), key=lambda item: (item[0] != "/", item[0]))


def interface_choices() -> list[tuple[str, str]]:
    """Network interfaces other than loopback."""
    try:
        names = _net_interfaces()
    except OSError:
        return []
    return [(name, name) for name in sorted(names - {"lo"})]


def _net_interfaces() -> set[str]:
    with open("/proc/net/dev", "rb") as handle:
        lines = handle.read().decode().splitlines()[2:]
    return {line.split(":", 1)[0].strip() for line in lines if ":" in line}


def _net_bytes(name: str | None) -> tuple[int, int]:
    """(received, sent) totals, for one interface or all but loopback."""
    received = sent = 0
    with open("/proc/net/dev", "rb") as handle:
        for line in handle.read().decode().splitlines()[2:]:
            face, _, rest = line.partition(":")
            face = face.strip()
            if name is None:
                if face == "lo":
                    continue
            elif face != name:
                continue
            values = rest.split()
            received += int(values[0])
            sent += int(values[8])
    return received, sent


def _cpu_times() -> tuple[int, int]:
    with open("/proc/stat", "rb") as handle:
        parts = handle.readline().split()[1:]
    values = [int(part) for part in parts]
    # user nice system idle iowait irq softirq steal ...
    return values[3] + values[4], sum(values)


def _meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    with open("/proc/meminfo", "rb") as handle:
        for line in handle:
            name, _, rest = line.partition(b":")
            info[name.decode()] = int(rest.split()[0]) * 1024
    return info


_REGISTRY: dict[str, type[Source]] = {
    "time": TimeSource,
    "cpu": CpuSource,
    "memory": MemorySource,
    "disk": DiskSource,
    "network": NetworkSource,
    "temperature": TemperatureSource,
    "hostname": HostnameSource,
    "uptime": UptimeSource,
    "kernel": KernelSource,
    "ip": IpSource,
    "media": MediaSource,
}


# A seam for tools, unset in the daemon. When set, every source create()
# makes is passed through it, so an editor can stand a simulated or
# frozen reading in front of the real one without the widget knowing.
wrap: Callable[[Source, str], Source] | None = None


def create(name: str, options: Table, widget: str) -> Source:
    """Instantiate a source by name."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise SourceError(
            f"widget {widget!r}: unknown source {name!r}; available: "
            f"{', '.join(sorted(_REGISTRY))}"
        )
    try:
        made = factory(options, widget)
    except OSError as exc:
        raise SourceError(f"widget {widget!r}: source {name!r} is unavailable: {exc}") from exc
    return wrap(made, name) if wrap is not None else made


def resolve(options: Table, widget: str, accepts: tuple[str, ...]) -> Source | None:
    """Read the `source` option and check the shape can draw it.

    A shape that plots numbers cannot show a clock, and saying so at
    load is better than formatting None into the picture.
    """
    name = options.string("source", None)
    if name is None:
        return None
    source = create(name, options, widget)
    if source.kind not in accepts:
        raise SourceError(
            f"widget {widget!r}: source {name!r} produces a {source.kind}, "
            f"but this widget needs {' or a '.join(accepts)}"
        )
    return source


def available() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def default_format(source) -> str | None:
    """The format a label uses for `source` (an instance, class or
    stand-in) when the theme gives none."""
    formats = getattr(source, "formats", None)
    return formats[0] if formats else None


def format_fields(text: str) -> set[str] | None:
    """The field names a format string uses, or None if it is not one."""
    import string as string_module

    try:
        return {
            name.split(".")[0].split("[")[0]
            for _, name, _, _ in string_module.Formatter().parse(text)
            if name is not None
        }
    except ValueError:
        return None


def lookup(name: str) -> type[Source] | None:
    """The class registered as `name`, without constructing it.

    For tools that need to know what a source is -- its kind, fields and
    options -- without reading /proc or checking a path exists.
    """
    return _REGISTRY.get(name)
