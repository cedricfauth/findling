"""Optional, source-specific extensions to the indexing pipeline.

The core pipeline only handles generic formats. Quirks of particular data sources
(e.g. the juris markup in German law texts) live here, as one module per extension,
and are switched on per config file:

    extensions = ["juris"]

An extension is a module in this package that defines any of these hooks:

    prepare_html(root: lxml.html.HtmlElement) -> None
        Rewrite a parsed HTML page in place before docling converts it,
        e.g. turn non-standard markup into plain HTML.

Changing an extension's code reprocesses the files indexed with it.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

HOOKS = ("prepare_html",)


def available() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_"))


def load(names: tuple[str, ...] | list[str]) -> list[ModuleType]:
    """Import the named extensions; raises ValueError for unknown names."""
    known = available()
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(
            f"unknown extension(s): {', '.join(unknown)} (available: {', '.join(known) or 'none'})"
        )
    modules = [importlib.import_module(f"{__name__}.{n}") for n in names]
    for module in modules:
        if not any(callable(getattr(module, hook, None)) for hook in HOOKS):
            raise ValueError(f"extension {module.__name__} defines none of the hooks {HOOKS}")
    return modules
