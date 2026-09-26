"""How the agent names entities to people.

Human-facing text (tool ``message`` fields, clarification questions,
confirmation prompts) names an entity by its Home Assistant friendly name.
When another entity shares that name, the area is added; with no usable
name, or when even the area cannot tell them apart, the entity_id is used.

Presentation only: matching, policy checks and service calls always use the
entity_id, never anything returned here. Never raises.
"""
from __future__ import annotations

from typing import Iterable, Optional


def _friendly_name(hass, entity_id: str) -> Optional[str]:
    try:
        state = hass.states.get(entity_id)
        name = state.attributes.get("friendly_name") if state is not None else None
    except Exception:
        name = None
    name = str(name).strip() if name else ""
    return name or None


def _area_name(hass, entity_id: str) -> Optional[str]:
    try:
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        entry = er.async_get(hass).async_get(entity_id)
        if entry is None:
            return None
        area_id = entry.area_id
        if not area_id and entry.device_id:
            device = dr.async_get(hass).async_get(entry.device_id)
            area_id = getattr(device, "area_id", None)
        if not area_id:
            return None
        area = ar.async_get(hass).async_get_area(area_id)
        return getattr(area, "name", None) or None
    except Exception:
        return None


def _name_counts(hass) -> dict:
    counts: dict = {}
    try:
        for state in hass.states.async_all():
            name = state.attributes.get("friendly_name")
            if name:
                key = str(name).strip().casefold()
                counts[key] = counts.get(key, 0) + 1
    except Exception:
        pass
    return counts


def display_names(hass, entity_ids: Iterable[str]) -> dict:
    """entity_id -> the name to show a person, for several entities at once."""
    ids = [str(e) for e in entity_ids if e]
    counts = _name_counts(hass)
    out: dict = {}
    chosen: dict = {}
    for eid in ids:
        name = _friendly_name(hass, eid)
        if not name:
            out[eid] = eid
            continue
        if counts.get(name.casefold(), 0) > 1:
            area = _area_name(hass, eid)
            name = f"{name} ({area})" if area else f"{name} ({eid})"
        out[eid] = name
        chosen.setdefault(name.casefold(), []).append(eid)
    # Still colliding (same name and same area): only the entity_id tells
    # them apart.
    for key, same in chosen.items():
        if len(set(same)) > 1:
            for eid in same:
                out[eid] = f"{out[eid]} ({eid})" if eid not in out[eid] else out[eid]
    return out


def display_name(hass, entity_id: str) -> str:
    """The name to show a person for one entity."""
    return display_names(hass, [entity_id]).get(str(entity_id), str(entity_id))
