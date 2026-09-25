"""Write-through compatibility for the root automation modules.

The automation code moved into this package, but callers, tests and the
evaluation harness still import and patch it through five root modules
(``automation_creator``, ``automation_inventory``, ``automation_matcher``,
``automation_trials`` and ``pattern_analyzer``). A plain re-export would keep
imports working but silently break two things those callers rely on:

* process configuration that is rebound at runtime (``set_thresholds``
  rebinding ``CONFIDENCE_THRESHOLD``) would be frozen at its import-time
  value in the root module;
* replacing a function on the root module (a test double, or a patched
  threshold) would no longer reach the package code that calls it.

``install()`` gives a root module a module class that reads live names
through to their package module and writes every re-exported name through to
the package module that owns it, so the root module behaves exactly as it did
before the move. Stdlib only; it holds no state of its own.
"""
from __future__ import annotations

import sys
import types
from typing import Iterable


class _CompatModule(types.ModuleType):
    """A root module whose names are owned by package modules."""

    def __getattr__(self, name: str):
        live = self.__dict__.get("_compat_live", {})
        if name in live:
            return getattr(live[name], name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        owners = self.__dict__.get("_compat_owners", {})
        live = self.__dict__.get("_compat_live", {})
        if name in live:
            setattr(live[name], name, value)
            return
        if name in owners:
            setattr(owners[name], name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        live = self.__dict__.get("_compat_live", {})
        if name in live:
            delattr(live[name], name)
            return
        super().__delattr__(name)


def _owner(name: str, value, sources: Iterable[types.ModuleType]):
    """The package module that defines ``name`` (or, for constants, the
    first source that holds the identical object)."""
    sources = list(sources)
    defined_in = getattr(value, "__module__", None)
    for source in sources:
        if source.__name__ == defined_in and getattr(source, name, None) is value:
            return source
    for source in sources:
        if getattr(source, name, None) is value:
            return source
    return None


def install(module_name: str, sources: Iterable[types.ModuleType],
            live: dict[str, types.ModuleType] | None = None) -> None:
    """Make ``module_name`` a write-through view of ``sources``.

    Every name in the module's ``__all__`` is written through to the package
    module that owns it. Names in ``live`` are not stored on the root module
    at all: they are read from and written to their package module."""
    module = sys.modules[module_name]
    sources = tuple(sources)
    owners = {}
    for name in getattr(module, "__all__", ()):
        owner = _owner(name, module.__dict__.get(name), sources)
        if owner is None:
            raise RuntimeError(f"{module_name}.{name} has no package owner")
        owners[name] = owner
    for name in (live or {}):
        module.__dict__.pop(name, None)
    module.__dict__["_compat_owners"] = owners
    module.__dict__["_compat_live"] = dict(live or {})
    module.__class__ = _CompatModule
