"""Compatibility module: deterministic automation matching now lives in
``automation/matching.py``. Every name here is the package object itself, and assigning one here
assigns it in the package (see ``automation/_compat.py``)."""
from __future__ import annotations

from .automation import (
    _compat,
    matching as _matching,
)
from .automation.matching import (
    _TOP_LEVEL_IGNORED,
    _entity_values,
    _list,
    _plain,
    action_effects,
    canonical_config,
    classify,
    classify_result,
    fingerprint,
)

__all__ = [
    "_TOP_LEVEL_IGNORED",
    "_entity_values",
    "_list",
    "_plain",
    "action_effects",
    "canonical_config",
    "classify",
    "classify_result",
    "fingerprint",
]

_compat.install(__name__, (_matching,))
