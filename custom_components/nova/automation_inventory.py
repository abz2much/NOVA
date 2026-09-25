"""Compatibility module: the automation inventory now lives in
``automation/inventory.py`` and execution-context attribution in
``automation/attribution.py``. Every name here is the package object itself, and assigning one here
assigns it in the package (see ``automation/_compat.py``)."""
from __future__ import annotations

from .automation import (
    _compat,
    inventory as _inventory,
    attribution as _attribution,
    models as _models,
)
from .automation.attribution import AutomationContextTracker
from .automation.inventory import (
    _NOVA_ALIAS_PREFIX,
    _NOVA_ID_PREFIX,
    EVENT_AUTOMATION_RELOADED,
    AutomationInventory,
    _metadata_records,
    _origin,
    _record_from_runtime,
    _registry_unique_id,
    _runtime_entities,
    _safe_attr,
    _safe_set,
    _state_value,
    get_inventory,
)
from .automation.models import AutomationRecord, SourceAttribution

__all__ = [
    "AutomationContextTracker",
    "AutomationInventory",
    "AutomationRecord",
    "EVENT_AUTOMATION_RELOADED",
    "SourceAttribution",
    "_NOVA_ALIAS_PREFIX",
    "_NOVA_ID_PREFIX",
    "_metadata_records",
    "_origin",
    "_record_from_runtime",
    "_registry_unique_id",
    "_runtime_entities",
    "_safe_attr",
    "_safe_set",
    "_state_value",
    "get_inventory",
]

_compat.install(__name__, (_inventory, _attribution, _models,))
