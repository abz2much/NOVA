"""Compatibility module: automation trials now live in
``automation/trials.py``. Every name here is the package object itself;
``_DEFAULT_DB`` is read through to the package module."""
from __future__ import annotations

from .automation import (
    _compat,
    trials as _trials,
)
from .automation.trials import (
    _connect,
    _resolve,
    async_handle_triggered,
    create,
    list_trials,
    record_run,
    set_manual_outcome,
)

__all__ = [
    "_connect",
    "_resolve",
    "async_handle_triggered",
    "create",
    "list_trials",
    "record_run",
    "set_manual_outcome",
]

_compat.install(
    __name__, (_trials,),
    live={
        "_DEFAULT_DB": _trials,
    },
)
