"""Authoritative security alarm selection for Nova.

Nova must not infer the home's security posture from every alarm panel in Home
Assistant. Camera hubs and vendor bridges often expose alarm shaped entities
whose armed state is unrelated to the household alarm.
"""
from __future__ import annotations

import logging

from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)
CONF_SECURITY_ALARM_ENTITY = "security_alarm_entity"


def _configured(hass, config: dict | None) -> str:
    from .runtime import domain_runtime_config
    runtime = domain_runtime_config(hass)
    if CONF_SECURITY_ALARM_ENTITY in runtime:
        return str(runtime[CONF_SECURITY_ALARM_ENTITY] or "").strip()
    if config and config.get(CONF_SECURITY_ALARM_ENTITY):
        return str(config[CONF_SECURITY_ALARM_ENTITY]).strip()
    try:
        from . import nova_config
        return str(nova_config.get(CONF_SECURITY_ALARM_ENTITY, "") or "").strip()
    except Exception:
        return ""


def _alarmo_entities(hass) -> tuple[str, ...]:
    try:
        registry = er.async_get(hass)
        found = []
        for entity_id, entry in registry.entities.items():
            eid = getattr(entry, "entity_id", None) or entity_id
            if (str(eid).startswith("alarm_control_panel.")
                    and getattr(entry, "platform", "") == "alarmo"):
                found.append(str(eid))
        return tuple(sorted(set(found)))
    except Exception as exc:
        _LOGGER.debug("Alarmo source discovery failed: %s", exc)
        return ()


def entity_ids(hass, config: dict | None = None) -> tuple[str, ...]:
    """Return the selected security alarm entity, or one clear Alarmo source.

    An explicit setting always wins. Without one, exactly one registered
    Alarmo panel is safe to discover automatically. Zero or multiple matches
    fail quiet until the user selects the intended panel.
    """
    configured = _configured(hass, config)
    if configured:
        if (configured.startswith("alarm_control_panel.")
                and hass.states.get(configured) is not None):
            return (configured,)
        return ()
    alarmo = _alarmo_entities(hass)
    if len(alarmo) != 1 or hass.states.get(alarmo[0]) is None:
        return ()
    return alarmo


def states(hass, config: dict | None = None) -> list:
    return [state for eid in entity_ids(hass, config)
            if (state := hass.states.get(eid)) is not None]


def is_selected(hass, entity_id: str, config: dict | None = None) -> bool:
    return entity_id in entity_ids(hass, config)


def available(hass) -> list[dict]:
    """Panel options for every alarm entity, including its integration."""
    registry = er.async_get(hass)
    result = []
    for state in hass.states.async_all("alarm_control_panel"):
        entry = registry.async_get(state.entity_id)
        result.append({
            "entity_id": state.entity_id,
            "name": state.attributes.get("friendly_name", state.entity_id),
            "platform": getattr(entry, "platform", "") if entry else "",
        })
    return sorted(result, key=lambda item: item["name"].lower())
