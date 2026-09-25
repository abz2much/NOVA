"""Read-only inventory of Home Assistant automations.

Nova's pattern learner needs to know that an observed device change may already
be automated.  Home Assistant has already loaded and expanded automations from
YAML, packages, UI storage, and blueprints, so the runtime automation component
is the authoritative place to inspect them.  This module deliberately does not
read ``automations.yaml`` and never writes automation configuration.

The runtime API has changed shape across Home Assistant releases.  Inventory
therefore degrades in layers: full runtime entities when available, then the
entity registry/state machine.  A missing private detail means "metadata only",
never "no automations".
"""
from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional

from .models import AutomationRecord

_LOGGER = logging.getLogger(__name__)

EVENT_AUTOMATION_RELOADED = "automation_reloaded"
_NOVA_ID_PREFIX = "nova_auto_"
_NOVA_ALIAS_PREFIX = "Nova · "


def _safe_set(value: Any) -> tuple[str, ...]:
    """Return a stable, bounded tuple from a runtime reference collection."""
    try:
        return tuple(sorted(str(item) for item in (value or ()) if item))
    except Exception:
        return ()


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _state_value(state: Any, key: str, default: Any = None) -> Any:
    try:
        return (state.attributes or {}).get(key, default)
    except Exception:
        return default


def _origin(unique_id: str, name: str, raw_config: Any) -> str:
    """Identify Nova-created automations without relying on the entity slug."""
    alias = ""
    if isinstance(raw_config, Mapping):
        alias = str(raw_config.get("alias") or "")
    if (str(unique_id or "").startswith(_NOVA_ID_PREFIX)
            or alias.startswith(_NOVA_ALIAS_PREFIX)
            or str(name or "").startswith(_NOVA_ALIAS_PREFIX)):
        return "nova"
    return "existing"


def _runtime_entities(hass: Any) -> list[Any]:
    """Return HA's loaded automation entities, or [] on an incompatible core."""
    try:
        from homeassistant.components.automation import DATA_COMPONENT
        component = hass.data.get(DATA_COMPONENT)
        return list(component.entities) if component is not None else []
    except Exception:
        return []


def _registry_unique_id(hass: Any, entity_id: str) -> str:
    try:
        from homeassistant.helpers import entity_registry as er
        entry = er.async_get(hass).async_get(entity_id)
        return str(entry.unique_id or "") if entry else ""
    except Exception:
        return ""


def _record_from_runtime(hass: Any, entity: Any) -> Optional[AutomationRecord]:
    entity_id = str(_safe_attr(entity, "entity_id", "") or "")
    if not entity_id.startswith("automation."):
        return None
    state = None
    try:
        state = hass.states.get(entity_id)
    except Exception:
        pass
    unique_id = str(_safe_attr(entity, "unique_id", "") or "")
    if not unique_id:
        unique_id = _registry_unique_id(hass, entity_id)
    raw_config = _safe_attr(entity, "raw_config")
    blueprint = _safe_attr(entity, "referenced_blueprint")
    if raw_config is not None:
        understanding = "partial" if blueprint else "full"
    else:
        understanding = "metadata_only"
    name = str(_safe_attr(entity, "name", "") or _state_value(
        state, "friendly_name", "") or entity_id)
    enabled = bool(_safe_attr(entity, "is_on", False))
    if state is not None:
        enabled = str(_safe_attr(state, "state", "off")) == "on"
    return AutomationRecord(
        entity_id=entity_id,
        unique_id=unique_id,
        name=name,
        enabled=enabled,
        last_triggered=_state_value(state, "last_triggered"),
        origin=_origin(unique_id, name, raw_config),
        understanding=understanding,
        blueprint=str(blueprint) if blueprint else None,
        referenced_entities=_safe_set(_safe_attr(entity, "referenced_entities", ())),
        referenced_devices=_safe_set(_safe_attr(entity, "referenced_devices", ())),
        referenced_areas=_safe_set(_safe_attr(entity, "referenced_areas", ())),
        raw_config=raw_config,
    )


def _metadata_records(hass: Any) -> list[AutomationRecord]:
    """Compatibility fallback when HA's runtime component cannot be inspected."""
    try:
        states = list(hass.states.async_all("automation"))
    except Exception:
        states = []
    records = []
    for state in states:
        entity_id = str(_safe_attr(state, "entity_id", "") or "")
        if not entity_id:
            continue
        unique_id = _registry_unique_id(hass, entity_id)
        name = str(_state_value(state, "friendly_name", "") or entity_id)
        records.append(AutomationRecord(
            entity_id=entity_id,
            unique_id=unique_id,
            name=name,
            enabled=str(_safe_attr(state, "state", "off")) == "on",
            last_triggered=_state_value(state, "last_triggered"),
            origin=_origin(unique_id, name, None),
        ))
    return records


class AutomationInventory:
    """Small in-memory snapshot refreshed only at startup or HA reload."""

    def __init__(self, hass: Any):
        self.hass = hass
        self._records: dict[str, AutomationRecord] = {}
        self.refreshed_at = 0.0
        self._unsub = None

    def refresh(self) -> list[AutomationRecord]:
        runtime = _runtime_entities(self.hass)
        records = []
        for entity in runtime:
            record = _record_from_runtime(self.hass, entity)
            if record is not None:
                records.append(record)
        if not records:
            records = _metadata_records(self.hass)
        self._records = {record.entity_id: record for record in records}
        self.refreshed_at = time.time()
        return self.records()

    def start(self) -> None:
        self.refresh()
        if self._unsub is None:
            try:
                self._unsub = self.hass.bus.async_listen(
                    EVENT_AUTOMATION_RELOADED, self._on_reloaded)
            except Exception:
                self._unsub = None

    def _on_reloaded(self, _event: Any) -> None:
        self.refresh()

    def records(self) -> list[AutomationRecord]:
        return sorted(self._records.values(), key=lambda item: item.entity_id)

    def get(self, entity_id: str) -> Optional[AutomationRecord]:
        return self._records.get(entity_id)

    def public_items(self) -> list[dict[str, Any]]:
        return [record.public_dict() for record in self.records()]

    def close(self) -> None:
        if callable(self._unsub):
            try:
                self._unsub()
            except Exception:
                pass
        self._unsub = None
        self._records.clear()


def get_inventory(hass: Any) -> Optional[AutomationInventory]:
    """Nova's per-entry inventory, owned by the entry's NovaRuntime.

    None when no Nova entry is loaded or the inventory was never built; a
    loaded entry without its runtime raises NovaRuntimeUnavailable."""
    from ..runtime import domain_runtime
    runtime = domain_runtime(hass)
    inventory = runtime.automation_inventory if runtime is not None else None
    return inventory if isinstance(inventory, AutomationInventory) else None
