"""Plugin registry, plus the built-in plugins.

Importing this package registers everything that ships with akp02d.
Adding a plugin is a module here with an @register("name") class and one
import line below.
"""

from __future__ import annotations

from .base import PluginError, Widget, WidgetSpec, available, create, register
from . import gauge as _gauge  # noqa: F401  (registers "gauge")
from . import graph as _graph  # noqa: F401  (registers "graph")
from . import label as _label  # noqa: F401  (registers "label")
from . import spectrum as _spectrum  # noqa: F401  (registers "spectrum")

__all__ = [
    "PluginError",
    "Widget",
    "WidgetSpec",
    "available",
    "create",
    "register",
]
