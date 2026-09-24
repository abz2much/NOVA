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

from dataclasses import dataclass, field
from collections import OrderedDict
import logging
import time
from typing import Any, Mapping, Optional

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


@dataclass(slots=True)
class AutomationRecord:
    """One loaded automation. ``raw_config`` never leaves the backend."""

    entity_id: str
    unique_id: str = ""
    name: str = ""
    enabled: bool = False
    last_triggered: Any = None
    origin: str = "existing"
    understanding: str = "metadata_only"
    blueprint: Optional[str] = None
    referenced_entities: tuple[str, ...] = ()
    referenced_devices: tuple[str, ...] = ()
    referenced_areas: tuple[str, ...] = ()
    raw_config: Any = field(default=None, repr=False, compare=False)

    def public_dict(self) -> dict[str, Any]:
        """A bounded panel-safe representation; never expose raw automation data."""
        return {
            "entity_id": self.entity_id,
            "unique_id": self.unique_id,
            "name": self.name or self.entity_id,
            "enabled": bool(self.enabled),
            "last_triggered": self.last_triggered,
            "origin": self.origin,
            "understanding": self.understanding,
            "blueprint": self.blueprint,
            "referenced_entities": list(self.referenced_entities),
            "referenced_devices": list(self.referenced_devices),
            "referenced_areas": list(self.referenced_areas),
        }


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
    """Find Nova's per-entry inventory without adding a global singleton."""
    try:
        from .const import DOMAIN
        for value in hass.data.get(DOMAIN, {}).values():
            if isinstance(value, dict):
                inventory = value.get("automation_inventory")
                if isinstance(inventory, AutomationInventory):
                    return inventory
    except Exception:
        pass
    return None


@dataclass(frozen=True, slots=True)
class SourceAttribution:
    """Bounded provenance attached to one observed state change."""

    kind: str = "unknown"
    entity_id: str = ""
    confidence: float = 0.0


class AutomationContextTracker:
    """Correlate HA automation contexts with the state changes they cause.

    ``automation_triggered`` is emitted before the action script runs and uses
    the same context passed into its service calls.  A fixed-size TTL map makes
    attribution a constant-time lookup on the state-change hot path.
    """

    def __init__(self, inventory: Optional[AutomationInventory] = None, *,
                 ttl: float = 21600.0, max_entries: int = 4096):
        self.inventory = inventory
        self.ttl = max(60.0, float(ttl))
        self.max_entries = max(64, int(max_entries))
        self._contexts: OrderedDict[str, tuple[float, SourceAttribution]] = OrderedDict()

    @staticmethod
    def _context_id(context: Any, field_name: str = "id") -> str:
        value = _safe_attr(context, field_name, "") if context is not None else ""
        return str(value or "")

    def record_trigger(self, event: Any, now: Optional[float] = None) -> None:
        entity_id = str(getattr(event, "data", {}).get("entity_id") or "")
        context_id = self._context_id(getattr(event, "context", None))
        if not entity_id or not context_id:
            return
        record = self.inventory.get(entity_id) if self.inventory else None
        kind = "nova_automation" if record and record.origin == "nova" else "automation"
        stamp = time.monotonic() if now is None else float(now)
        self._contexts[context_id] = (
            stamp, SourceAttribution(kind, entity_id, 1.0))
        self._contexts.move_to_end(context_id)
        self._prune(stamp)

    def resolve_state(self, state: Any, now: Optional[float] = None) -> SourceAttribution:
        context = _safe_attr(state, "context")
        if context is None:
            return SourceAttribution()
        # A user_id is direct evidence of a user-initiated state/service action.
        if _safe_attr(context, "user_id"):
            return SourceAttribution("user", "", 1.0)
        stamp = time.monotonic() if now is None else float(now)
        self._prune(stamp)
        for context_id in (self._context_id(context),
                           self._context_id(context, "parent_id")):
            found = self._contexts.get(context_id)
            if found is not None:
                return found[1]
        # HA supplied a context but it was not one of the automation runs Nova
        # observed. It may be an integration or physical-device event; do not
        # claim a more precise source than the evidence supports.
        return SourceAttribution("device_or_integration", "", 0.5)

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl
        while self._contexts:
            _key, (stamp, _source) = next(iter(self._contexts.items()))
            if stamp >= cutoff and len(self._contexts) <= self.max_entries:
                break
            self._contexts.popitem(last=False)

    def close(self) -> None:
        self._contexts.clear()
        self.inventory = None
