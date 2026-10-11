"""A theme.toml that can be edited without being rewritten.

A theme is a file people write by hand, with comments that explain the
numbers (`max = 12500000  # 100 Mbit`) and an order that groups a
caption with the graph it labels. A TOML library that loads and dumps
the whole document loses some of that: tomlkit, for one, regroups
interleaved `[[widget.label]]` / `[[widget.graph]]` blocks the moment
anything changes. So this edits the text itself, one line at a time:

* setting a key that exists replaces only its value, keeping the
  comment after it;
* setting a new key appends one line to the end of that widget's keys;
* removing a key removes its line (or lines, for a multi-line array);
* adding a widget appends a block at the end of the file;
* removing a widget removes its block.

Everything else in the file is left byte-for-byte as it was. The
structure is found by headers, and every value is located by asking
tomllib where it ends, so a multi-line gradient or an inline table is a
value like any other. After each edit the whole text is parsed again,
so an edit that would leave invalid TOML is refused rather than saved.

Widgets are addressed the way the daemon names them: by plugin and the
1-based position among that plugin's entries, which is what `label#3`
means when a widget has no explicit id.
"""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["DocumentError", "Ref", "ThemeDocument", "toml_value"]

_ARRAY_HEADER = re.compile(r"^\s*\[\[\s*([^\]]+?)\s*\]\]\s*(#.*)?$")
_TABLE_HEADER = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(#.*)?$")
_KEY_LINE = re.compile(r"^(\s*)([A-Za-z0-9_-]+|\"[^\"]*\")\s*=\s*(.*)$")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


class DocumentError(Exception):
    """An edit could not be applied, or would make the file invalid."""


@dataclass(frozen=True, slots=True)
class Ref:
    """A widget, as the daemon numbers it: `label#2` is ("label", 2)."""

    plugin: str
    index: int

    @property
    def default_id(self) -> str:
        return f"{self.plugin}#{self.index}"


@dataclass(slots=True)
class _Block:
    """A run of lines under one header, up to the next top-level header.

    `kind` is "preamble" (before any header), "theme", "widget", or
    "other". A sub-table header that belongs to a widget -- `[widget.
    spectrum.cava]` would be an odd thing to write, but legal -- stays
    inside that widget's block.
    """

    kind: str
    start: int          # header line, or 0 for the preamble
    end: int            # exclusive
    plugin: str | None = None

    @property
    def body(self) -> int:
        return self.start if self.kind == "preamble" else self.start + 1


def toml_value(value: Any) -> str:
    """One value as TOML source, on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DocumentError(f"cannot write {value!r} to a theme")
        # Twelve significant digits hides float noise (0.1 + 0.2) without
        # rounding small values to nothing.
        text = repr(float(f"{value:.12g}"))
        return text if any(c in text for c in ".en") else text + ".0"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        return "{ " + ", ".join(
            f"{_key(k)} = {toml_value(v)}" for k, v in value.items()
        ) + " }"
    if isinstance(value, Sequence):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    raise DocumentError(f"cannot write {type(value).__name__} to a theme")


def _string(value: str) -> str:
    out = ['"']
    for char in value:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _key(name: str) -> str:
    return name if _BARE_KEY.match(name) else _string(name)


def _parses(value_text: str) -> bool:
    try:
        tomllib.loads(f"v = {value_text}\n")
    except tomllib.TOMLDecodeError:
        return False
    return True


class ThemeDocument:
    """The text of one theme.toml, with structured edits on top."""

    def __init__(self, text: str, path: Path | None = None) -> None:
        self.path = path
        self._lines: list[str] = []
        self._data: dict[str, Any] = {}
        self._set_text(text)

    # -- loading and saving --

    @classmethod
    def open(cls, path: Path) -> ThemeDocument:
        """Read `path` (a theme.toml, or the directory holding one)."""
        if path.is_dir():
            path = path / "theme.toml"
        return cls(path.read_text(encoding="utf-8"), path)

    @classmethod
    def new(cls, orientation: str = "portrait", path: Path | None = None) -> ThemeDocument:
        return cls(
            "[theme]\n"
            f"orientation = {_string(orientation)}\n"
            'background = "#000000"\n',
            path,
        )

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path
        if target is None:
            raise DocumentError("no file to save to")
        if target.is_dir() or target.suffix != ".toml":
            target = target / "theme.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(self.text, encoding="utf-8")
        tmp.replace(target)  # atomic, so a watching daemon never reads half
        self.path = target
        return target

    @property
    def directory(self) -> Path | None:
        return self.path.parent if self.path is not None else None

    @property
    def text(self) -> str:
        return "".join(self._lines)

    @property
    def data(self) -> dict[str, Any]:
        """The parsed document. Treat as read-only."""
        return self._data

    def _set_text(self, text: str) -> None:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise DocumentError(f"not valid TOML: {exc}") from exc
        self._lines = text.splitlines(keepends=True)
        if self._lines and not self._lines[-1].endswith("\n"):
            self._lines[-1] += "\n"
        self._data = data

    def _commit(self, lines: list[str]) -> None:
        """Adopt `lines` if they still parse; otherwise refuse the edit."""
        self._set_text("".join(lines))

    # -- reading --

    @property
    def theme(self) -> dict[str, Any]:
        value = self._data.get("theme", {})
        return value if isinstance(value, dict) else {}

    def refs(self) -> list[Ref]:
        """Every widget, in the order the daemon builds them."""
        widgets = self._data.get("widget", {})
        if not isinstance(widgets, dict):
            return []
        return [
            Ref(plugin, index)
            for plugin, entries in widgets.items()
            if isinstance(entries, list)
            for index in range(1, len(entries) + 1)
        ]

    def file_order(self) -> list[Ref]:
        """Every widget, in the order they appear in the file."""
        counts: dict[str, int] = {}
        out = []
        for block in self._blocks():
            if block.kind == "widget":
                assert block.plugin is not None
                counts[block.plugin] = counts.get(block.plugin, 0) + 1
                out.append(Ref(block.plugin, counts[block.plugin]))
        return out

    def entry(self, ref: Ref) -> dict[str, Any]:
        try:
            return self._data["widget"][ref.plugin][ref.index - 1]
        except (KeyError, IndexError, TypeError) as exc:
            raise DocumentError(f"no widget {ref.default_id}") from exc

    def widget_id(self, ref: Ref) -> str:
        value = self.entry(ref).get("id")
        return value if isinstance(value, str) and value else ref.default_id

    def find(self, widget_id: str) -> Ref | None:
        for ref in self.refs():
            if self.widget_id(ref) == widget_id:
                return ref
        return None

    # -- editing --

    def set(self, ref: Ref, key: str, value: Any) -> None:
        """Set a widget key; None removes it."""
        self._set_in(self._widget_block(ref), key, value)

    def update(self, ref: Ref, values: Mapping[str, Any]) -> None:
        """Set several keys of one widget as one edit."""
        lines = list(self._lines)
        try:
            for key, value in values.items():
                self._set_in(self._widget_block(ref), key, value)
        except DocumentError:
            self._lines = lines
            self._set_text("".join(lines))
            raise

    def set_theme(self, key: str, value: Any) -> None:
        """Set a [theme] key; None removes it. Creates [theme] if needed."""
        block = next((b for b in self._blocks() if b.kind == "theme"), None)
        if block is None:
            if value is None:
                return
            lines = list(self._lines)
            at = self._first_header()
            insert = ["[theme]\n", f"{key} = {toml_value(value)}\n"]
            if at < len(lines):
                insert.append("\n")
            lines[at:at] = insert
            self._commit(lines)
            return
        self._set_in(block, key, value)

    def add_widget(self, plugin: str, values: Mapping[str, Any]) -> Ref:
        """Append a [[widget.<plugin>]] block; returns its ref."""
        if not _BARE_KEY.match(plugin):
            raise DocumentError(f"bad plugin name {plugin!r}")
        lines = list(self._lines)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("\n")
        lines.append(f"[[widget.{plugin}]]\n")
        lines += [f"{_key(k)} = {toml_value(v)}\n" for k, v in values.items()
                  if v is not None]
        self._commit(lines)
        count = len(self._data["widget"][plugin])
        return Ref(plugin, count)

    def remove_widget(self, ref: Ref) -> None:
        block = self._widget_block(ref)
        lines = list(self._lines)
        start, end = block.start, self._own_end(block)
        # Take the comment lines directly above the header with it: they
        # are almost always about this widget. Stop at a blank line.
        while start > 0 and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        # The block already ends with the blank lines before the next
        # header; if it is the last block, drop the separator above it
        # instead, so the file does not end in a gap.
        if end >= len(lines):
            while start > 0 and not lines[start - 1].strip():
                start -= 1
        del lines[start:end]
        self._commit(lines)

    def duplicate_widget(self, ref: Ref, changes: Mapping[str, Any]) -> Ref:
        """Append a copy of a widget's block, with `changes` applied."""
        block = self._widget_block(ref)
        body = self._lines[block.body:self._own_end(block)]
        while body and not body[-1].strip():
            body.pop()
        lines = list(self._lines)
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append("\n")
        lines.append(f"[[widget.{ref.plugin}]]\n")
        lines += body
        self._commit(lines)
        new = Ref(ref.plugin, len(self._data["widget"][ref.plugin]))
        if "id" in self.entry(new):
            changes = {"id": None, **changes}
        self.update(new, changes)
        return new

    # -- structure --

    def _first_header(self) -> int:
        for block in self._blocks():
            if block.kind != "preamble":
                return block.start
        return len(self._lines)

    def _blocks(self) -> list[_Block]:
        blocks: list[_Block] = []
        current = _Block("preamble", 0, 0)
        # Tracks whether we are inside a multi-line value, where a line
        # starting with "[" is part of an array, not a header.
        pending: list[str] = []
        for number, line in enumerate(self._lines):
            if pending:
                pending.append(line)
                if _parses("".join(pending)):
                    pending = []
                continue
            stripped = line.strip()
            header = None
            array = _ARRAY_HEADER.match(line)
            table = None if array else _TABLE_HEADER.match(line)
            if array:
                header = ("array", array.group(1).replace(" ", ""))
            elif table:
                header = ("table", table.group(1).replace(" ", ""))
            if header is None:
                key = _KEY_LINE.match(line)
                if key and stripped and not stripped.startswith("#"):
                    rest = key.group(3)
                    if not self._value_extent_ok(rest):
                        pending = [rest]
                continue
            shape, name = header
            parts = name.split(".")
            # A sub-table of the current widget stays in its block.
            if (
                current.kind == "widget"
                and shape == "table"
                and len(parts) > 2
                and parts[0] == "widget"
                and parts[1] == current.plugin
            ):
                continue
            current.end = number
            blocks.append(current)
            if shape == "array" and len(parts) == 2 and parts[0] == "widget":
                current = _Block("widget", number, number, parts[1])
            elif shape == "table" and name == "theme":
                current = _Block("theme", number, number)
            else:
                current = _Block("other", number, number)
        current.end = len(self._lines)
        blocks.append(current)
        return blocks

    @staticmethod
    def _value_extent_ok(rest: str) -> bool:
        """Whether a key's value is complete on its own line."""
        if _parses(rest):
            return True
        return _split_comment(rest) is not None

    def _own_end(self, block: _Block) -> int:
        """Where a block's own lines end: before any comment lines that
        sit directly above the next header, which are about that one."""
        end = block.end
        if end >= len(self._lines):
            return end
        while end > block.body and self._lines[end - 1].lstrip().startswith("#"):
            end -= 1
        return end

    def _widget_block(self, ref: Ref) -> _Block:
        count = 0
        for block in self._blocks():
            if block.kind == "widget" and block.plugin == ref.plugin:
                count += 1
                if count == ref.index:
                    return block
        raise DocumentError(f"no widget {ref.default_id}")

    def _keys(self, block: _Block) -> Iterator[tuple[str, int, int]]:
        """(key, first line, end line exclusive) for each key in a block's
        own table, stopping at a sub-table header."""
        number = block.body
        while number < block.end:
            line = self._lines[number]
            if _ARRAY_HEADER.match(line) or _TABLE_HEADER.match(line):
                return
            match = _KEY_LINE.match(line)
            if not match or line.lstrip().startswith("#"):
                number += 1
                continue
            name = match.group(2)
            if name.startswith('"'):
                name = tomllib.loads(f"{name} = 0")
                name = next(iter(name))
            end = number + 1
            if not self._value_extent_ok(match.group(3)):
                gathered = [match.group(3)]
                while end < block.end:
                    gathered.append(self._lines[end])
                    end += 1
                    if _parses("".join(gathered)):
                        break
            yield name, number, end
            number = end

    def _set_in(self, block: _Block, key: str, value: Any) -> None:
        lines = list(self._lines)
        found = next(((s, e) for name, s, e in self._keys(block) if name == key), None)
        if found is not None:
            start, end = found
            if value is None:
                del lines[start:end]
            else:
                match = _KEY_LINE.match(lines[start])
                assert match is not None
                indent, raw_key = match.group(1), match.group(2)
                comment = ""
                if end == start + 1:
                    split = _split_comment(match.group(3))
                    if split is not None:
                        comment = split[1]
                new = f"{indent}{raw_key} = {toml_value(value)}"
                if comment:
                    new += "  " + comment if not comment.startswith(" ") else comment
                lines[start:end] = [new.rstrip() + "\n"]
        else:
            if value is None:
                return
            keys = list(self._keys(block))
            if keys:
                at = keys[-1][2]
                indent = _KEY_LINE.match(lines[keys[0][1]]).group(1)  # type: ignore[union-attr]
            else:
                at = block.body
                indent = ""
            lines.insert(at, f"{indent}{_key(key)} = {toml_value(value)}\n")
        self._commit(lines)


def _split_comment(rest: str) -> tuple[str, str] | None:
    """Split "value  # comment" at the first # that ends a valid value."""
    rest = rest.rstrip("\n")
    for match in re.finditer("#", rest):
        value = rest[: match.start()].rstrip()
        if value and _parses(value):
            return value, rest[len(value):].rstrip()
    return None
