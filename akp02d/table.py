"""One typed, self-locating view over a TOML table.

Config, themes, and plugin options all need the same three things: read
a value with a type and a range, report the failure with the key's full
path, and refuse keys nobody read. This is that, once.

Refusing unread keys is the point of `reject_unknown`. A misspelled
option that silently does nothing is a worse bug than a startup
failure, because it is found much later and by confusion rather than by
a message.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from PIL import ImageColor

__all__ = ["REQUIRED", "SettingsError", "Table", "parse_color"]


class SettingsError(Exception):
    """A setting is missing, of the wrong type, or out of range."""


def parse_color(value: str, where: str = "colour") -> tuple[int, int, int]:
    """Any colour Pillow understands ("#rrggbb", "#rgb", "white"), as RGB."""
    try:
        return ImageColor.getrgb(value)[:3]
    except (ValueError, AttributeError) as exc:
        raise SettingsError(f'{where}: cannot read "{value}" as a colour') from exc


class _Missing:
    def __repr__(self) -> str:
        return "<required>"


REQUIRED = _Missing()


class Table:
    """A TOML table that knows where it came from."""

    __slots__ = ("_data", "_seen", "_where")

    def __init__(self, data: Any, where: str = "") -> None:
        if not isinstance(data, Mapping):
            raise SettingsError(f"{where or 'top level'} must be a table")
        self._data = data
        self._where = where
        self._seen: set[str] = set()

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def path(self, key: str) -> str:
        return f"{self._where}.{key}" if self._where else key

    def fail(self, key: str, expected: str, value: Any) -> SettingsError:
        return SettingsError(f"{self.path(key)} must be {expected}, got {value!r}")

    def raw(self, key: str, default: Any = None) -> Any:
        """Read a value with no checking, marking the key as read."""
        self._seen.add(key)
        return self._data.get(key, default)

    def string(self, key: str, default: str | None | _Missing = REQUIRED) -> Any:
        value = self.raw(key, default)
        if isinstance(value, _Missing):
            raise SettingsError(f"{self.path(key)} is required")
        if value is None or isinstance(value, str):
            return value
        raise self.fail(key, "a string", value)

    def boolean(self, key: str, default: bool) -> bool:
        value = self.raw(key, default)
        if not isinstance(value, bool):
            raise self.fail(key, "true or false", value)
        return value

    def number(
        self,
        key: str,
        default: float | _Missing,
        low: float = float("-inf"),
        high: float = float("inf"),
    ) -> float:
        value = self.raw(key, default)
        if isinstance(value, _Missing):
            raise SettingsError(f"{self.path(key)} is required")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise self.fail(key, "a number", value)
        if not low <= value <= high:
            raise self.fail(key, f"between {low} and {high}", value)
        return float(value)

    def integer(
        self, key: str, default: int | _Missing, low: int = -(2**31), high: int = 2**31
    ) -> int:
        value = self.raw(key, default)
        if isinstance(value, _Missing):
            raise SettingsError(f"{self.path(key)} is required")
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.fail(key, "an integer", value)
        if not low <= value <= high:
            raise self.fail(key, f"between {low} and {high}", value)
        return value

    def choice(self, key: str, default: str, allowed: Mapping[str, Any]) -> Any:
        value = self.string(key, default)
        if value not in allowed:
            raise self.fail(key, f"one of {', '.join(sorted(allowed))}", value)
        return allowed[value]

    def one_of(self, key: str, default: Any, allowed: Sequence[Any]) -> Any:
        value = self.raw(key, default)
        if value not in allowed:
            raise self.fail(key, f"one of {allowed}", value)
        return value

    def color(self, key: str, default: str) -> tuple[int, int, int]:
        """A colour setting. Lives here so plugins need not import the
        theme module, which imports the plugin registry."""
        return parse_color(self.string(key, default), self.path(key))

    def colors(
        self, key: str, default: Sequence[str]
    ) -> tuple[tuple[int, int, int], ...]:
        """A list of colours, for gradients."""
        value = self.raw(key, default)
        if isinstance(value, str) or not isinstance(value, Sequence) or not value:
            raise self.fail(key, "a non-empty list of colours", value)
        return tuple(
            parse_color(v, f"{self.path(key)}[{i}]") for i, v in enumerate(value)
        )

    def scalars(self, key: str) -> dict[str, Any]:
        """A table of plain scalars, to hand to another program verbatim.

        For settings akp02d deliberately does not model -- the keys and
        units belong to whatever reads them, and guessing at them is how
        a value ends up silently wrong.
        """
        value = self.raw(key, None)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise self.fail(key, "a table of settings", value)
        for name, item in value.items():
            if not isinstance(item, (str, int, float, bool)):
                raise SettingsError(
                    f"{self.path(key)}.{name} must be a string, number or "
                    f"boolean, got {item!r}"
                )
        return dict(value)

    def sections(self, key: str) -> dict[str, dict[str, Any]]:
        """A table of tables of scalars, to hand to another program."""
        value = self.raw(key, None)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise self.fail(key, "a table of sections", value)
        out: dict[str, dict[str, Any]] = {}
        for name, body in value.items():
            if not isinstance(body, Mapping):
                raise SettingsError(
                    f"{self.path(key)}.{name} must be a table of settings"
                )
            for setting, item in body.items():
                if not isinstance(item, (str, int, float, bool)):
                    raise SettingsError(
                        f"{self.path(key)}.{name}.{setting} must be a string, "
                        f"number or boolean, got {item!r}"
                    )
            out[name] = dict(body)
        return out

    def table(self, key: str) -> Table:
        return Table(self.raw(key, {}) or {}, self.path(key))

    def tables(self, key: str) -> list[Table]:
        """Read an array of tables ([[key]]), or an empty list."""
        entries = self.raw(key, [])
        if isinstance(entries, Mapping) or not isinstance(entries, Sequence):
            raise self.fail(key, "an array of tables ([[...]])", entries)
        return [
            Table(entry, f"{self.path(key)}[{index}]")
            for index, entry in enumerate(entries)
        ]

    def names(self) -> tuple[str, ...]:
        """Every key in this table, all marked read.

        For tables whose keys are names rather than settings -- plugin
        names, say -- validated by looking them up rather than by
        reject_unknown.
        """
        self._seen.update(self._data)
        return tuple(self._data)

    def reject_unknown(self) -> None:
        extra = sorted(set(self._data) - self._seen)
        if extra:
            raise SettingsError(
                f"unknown key(s) in {self._where or 'top level'}: "
                f"{', '.join(extra)}"
            )
