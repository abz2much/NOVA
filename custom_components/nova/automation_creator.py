"""Compatibility module: automation installation now lives in
``automation/installation.py``. Every name here is the package object
itself, including the process-wide ``_WRITE_LOCK``."""
from __future__ import annotations

from .automation import (
    _compat,
    installation as _installation,
)
from .automation.installation import (
    _WRITE_LOCK,
    AutomationWriteError,
    _atomic_write_yaml,
    _confirm_loaded,
    _duplicate_automation,
    _read_automations,
    _restore_original,
    _validate_with_home_assistant,
    create_automation,
)

__all__ = [
    "AutomationWriteError",
    "_WRITE_LOCK",
    "_atomic_write_yaml",
    "_confirm_loaded",
    "_duplicate_automation",
    "_read_automations",
    "_restore_original",
    "_validate_with_home_assistant",
    "create_automation",
]

_compat.install(__name__, (_installation,))
