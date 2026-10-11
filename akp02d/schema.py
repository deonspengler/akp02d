"""Option schemas: what a plugin or source accepts, declared once.

A plugin used to read its options imperatively -- `options.string(
"color_by", "level")` in the middle of `__init__` -- which works, but
leaves nothing to ask. A theme editor needs to know what a gauge accepts
without building one; documentation wants the same list; and a default
that lives only in a call site cannot be shown to anyone.

So each plugin (and each source with settings of its own) declares a
`Schema`, and reads its options *through* it:

    @register("gauge")
    class GaugeWidget(Widget):
        schema = Schema(
            source(accepts=(SCALAR,), required=True),
            choice("color_by", ("position", "level"), "level"),
            ...
        )

        def __init__(self, spec, theme):
            super().__init__(spec, theme)
            o = self.schema.read(spec.options, spec)
            ...

One declaration serves both. `read` goes through the same `Table`
methods as before, in the order the options are declared, so type
checks, ranges, error wording, and `reject_unknown` behave exactly as
they did. Cross-option rules (max above min, stereo needs an even bar
count) stay in the plugin, where they read as code rather than as a
little language.

Two things here exist only for tools and change nothing about reading:

* `when` says whether an option matters given the others -- `inner`
  only shapes a radial spectrum -- so an editor can hide what does
  nothing. Reading still reads every option, so a stray key is still
  refused.
* `Auto` is a default that depends on other options or on the widget's
  box, with a phrase describing it, so an editor can say "log for a
  pair, else linear" instead of showing a number that would be wrong
  half the time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from .table import REQUIRED, Table

__all__ = [
    "Auto", "Option", "Schema", "Values",
    "STRING", "FORMAT", "FONT", "INTEGER", "NUMBER", "BOOLEAN", "CHOICE",
    "COLOR", "COLORS", "SOURCE", "SECTIONS",
    "string", "text_format", "font", "integer", "number", "boolean",
    "choice", "color", "colors", "source", "sections",
]

# What kind of value an option holds. An editor picks its control from
# this, so it names the meaning ("a font"), not just the TOML type.
STRING = "string"
FORMAT = "format"      # a Python format string, filled from a source
FONT = "font"          # a theme-relative file or a system family
INTEGER = "integer"
NUMBER = "number"
BOOLEAN = "boolean"
CHOICE = "choice"
COLOR = "color"        # read as an (r, g, b) tuple
COLORS = "colors"      # a gradient: a non-empty list of colours
SOURCE = "source"      # a data source by name; read as a Source
SECTIONS = "sections"  # a table of tables, passed to another program


class Values(SimpleNamespace):
    """The options as read: attribute access, plus `in` and `get`."""

    def __contains__(self, key: str) -> bool:
        return key in self.__dict__

    def get(self, key: str, default: Any = None) -> Any:
        return self.__dict__.get(key, default)


@dataclass(frozen=True, slots=True)
class Auto:
    """A default computed from the options read so far.

    `compute(values, spec)` sees every option declared earlier and the
    widget's spec (for its box); `describe` is what a person reads.
    """

    compute: Callable[[Values, Any], Any]
    describe: str


@dataclass(frozen=True, slots=True)
class Option:
    """One key a plugin or source accepts.

    `default` is a value, REQUIRED, an Auto, or None. None means the key
    is optional and its absence means something ("no fill"), so it reads
    as None rather than as a value.
    """

    key: str
    kind: str
    default: Any = None
    low: float | None = None
    high: float | None = None
    choices: tuple[str, ...] = ()
    accepts: tuple[str, ...] = ()  # SOURCE: the source kinds a widget can draw
    group: str = "General"
    doc: str = ""
    when: Callable[[Values], bool] | None = None

    @property
    def required(self) -> bool:
        return self.default is REQUIRED

    def default_for(self, values: Values, spec: Any) -> Any:
        if isinstance(self.default, Auto):
            return self.default.compute(values, spec)
        return self.default

    def applies(self, values: Values) -> bool:
        """Whether this option does anything, given the others."""
        if self.when is None:
            return True
        try:
            return bool(self.when(values))
        except Exception:  # a half-edited theme; show the option anyway
            return True

    # -- reading --

    def read(self, table: Table, values: Values, spec: Any) -> Any:
        default = self.default_for(values, spec)
        key, kind = self.key, self.kind
        if default is None and key not in table and kind not in (
            STRING, FORMAT, FONT, SOURCE, SECTIONS
        ):
            table.raw(key)  # mark it read; absent means "none"
            return None
        if default is None and kind in (INTEGER, NUMBER, COLOR):
            default = REQUIRED  # present, so it must be valid
        if kind in (STRING, FORMAT, FONT):
            return table.string(key, default)
        if kind == INTEGER:
            return table.integer(key, default, *_bounds(self, int))
        if kind == NUMBER:
            return table.number(key, default, *_bounds(self, float))
        if kind == BOOLEAN:
            return table.boolean(key, default)
        if kind == CHOICE:
            value = table.string(key, default)
            if value not in self.choices:
                raise table.fail(key, _expected(self.choices), value)
            return value
        if kind == COLOR:
            return table.color(key, default)
        if kind == COLORS:
            return table.colors(key, default)
        if kind == SECTIONS:
            return table.sections(key)
        if kind == SOURCE:
            # Imported here: sources declare schemas of their own, so
            # this module is imported by sources.py.
            from . import sources

            widget_id = getattr(spec, "id", "?")
            found = sources.resolve(table, widget_id, self.accepts)
            if found is None and self.required:
                plugin = getattr(spec, "plugin", "this widget")
                raise sources.SourceError(
                    f"widget {widget_id!r}: {plugin} needs a source"
                )
            return found
        raise ValueError(f"option {key!r} has unknown kind {kind!r}")


def _bounds(option: Option, cast: type) -> tuple[Any, ...]:
    """The range arguments Table expects, leaving its defaults alone."""
    if option.low is None and option.high is None:
        return ()
    low = option.low if option.low is not None else (
        -(2**31) if cast is int else float("-inf"))
    high = option.high if option.high is not None else (
        2**31 if cast is int else float("inf"))
    return (cast(low), cast(high))


def _expected(choices: tuple[str, ...]) -> str:
    if len(choices) == 2:
        return " or ".join(choices)
    return f"one of {', '.join(choices)}"


@dataclass(frozen=True, slots=True)
class Schema:
    """An ordered set of options."""

    options: tuple[Option, ...] = field(default_factory=tuple)

    def __init__(self, *options: Option) -> None:
        keys = [option.key for option in options]
        duplicate = {key for key in keys if keys.count(key) > 1}
        if duplicate:
            raise ValueError(f"duplicate option(s): {', '.join(sorted(duplicate))}")
        object.__setattr__(self, "options", tuple(options))

    def __iter__(self) -> Iterator[Option]:
        return iter(self.options)

    def __len__(self) -> int:
        return len(self.options)

    def __getitem__(self, key: str) -> Option:
        for option in self.options:
            if option.key == key:
                return option
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return any(option.key == key for option in self.options)

    def keys(self) -> tuple[str, ...]:
        return tuple(option.key for option in self.options)

    def read(self, table: Table, spec: Any = None) -> Values:
        """Read every option, in order, through `table`.

        Does not call reject_unknown: a widget's table also holds the
        base class's keys and its source's, so the plugin rejects once
        everyone has read.
        """
        values = Values()
        for option in self.options:
            setattr(values, option.key, option.read(table, values, spec))
        return values

    def effective(
        self, raw: Mapping[str, Any], spec: Any = None, resolve: Callable | None = None
    ) -> Values:
        """What each option would be, without validating anything.

        For tools: the theme's own value where there is one, else the
        default, with Auto defaults computed in order. `resolve` turns a
        SOURCE name into something with a `.kind` (the source class), so
        Auto and `when` can ask what kind of source was picked.
        """
        values = Values()
        for option in self.options:
            if option.key in raw:
                value = raw[option.key]
                if option.kind == SOURCE and resolve is not None:
                    value = resolve(value)
            else:
                try:
                    value = option.default_for(values, spec)
                except Exception:
                    value = None
                if value is REQUIRED:
                    value = None
            setattr(values, option.key, value)
        return values


# -- constructors, so a declaration reads as a list of keys --


def string(key: str, default: Any = None, **kw: Any) -> Option:
    return Option(key, STRING, default, **kw)


def text_format(key: str = "format", default: Any = None, **kw: Any) -> Option:
    return Option(key, FORMAT, default, **kw)


def font(key: str = "font", **kw: Any) -> Option:
    return Option(key, FONT, None, **kw)


def integer(key: str, default: Any, low: int | None = None,
            high: int | None = None, **kw: Any) -> Option:
    return Option(key, INTEGER, default, low, high, **kw)


def number(key: str, default: Any, low: float | None = None,
           high: float | None = None, **kw: Any) -> Option:
    return Option(key, NUMBER, default, low, high, **kw)


def boolean(key: str, default: bool, **kw: Any) -> Option:
    return Option(key, BOOLEAN, default, **kw)


def choice(key: str, choices: tuple[str, ...], default: Any, **kw: Any) -> Option:
    return Option(key, CHOICE, default, choices=tuple(choices), **kw)


def color(key: str, default: Any = None, **kw: Any) -> Option:
    return Option(key, COLOR, default, **kw)


def colors(key: str, default: tuple[str, ...], **kw: Any) -> Option:
    return Option(key, COLORS, tuple(default), **kw)


def source(key: str = "source", accepts: tuple[str, ...] = (),
           required: bool = False, **kw: Any) -> Option:
    kw.setdefault("group", "Data")
    return Option(key, SOURCE, REQUIRED if required else None,
                  accepts=tuple(accepts), **kw)


def sections(key: str, **kw: Any) -> Option:
    return Option(key, SECTIONS, None, **kw)
