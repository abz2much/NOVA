"""Smoke, carbon monoxide, gas and water leak alarms, announced by the
cognitive core on every install (8.41.0).

Until 8.41.0 these were only announced by the Observer, which is off by
default, so a new install heard nothing when a smoke alarm went off. The
core now starts on every install and owns them. The message is the fixed
hazard sentence from the cognitive evaluators, never an AI reply, and it is
delivered as a critical core action: spoken everywhere, quiet hours and
sleep included, and pushed to the phones.

Only the user's own entity exclusion can stop one. Ignore rules, debounces
and rate limits never apply to a new alarm.
"""
from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import Event, HomeAssistant

from . import core_common as _m_common
from . import core_delivery as _m_delivery
from .cognitive import evaluators as _evaluators

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

ACTION_TYPE = "critical_hazard"


def hazard_action(hass: HomeAssistant, event: Event) -> Optional[dict]:
    """The critical action for a hazard sensor entering its alarm state,
    or None for any other state change."""
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")
    if old_state is None or new_state is None:
        return None
    device_class = new_state.attributes.get("device_class") or ""
    if not _evaluators.is_critical_hazard_activation(
            device_class, old_state.state, new_state.state):
        return None
    entity_id = event.data.get("entity_id", "")
    try:
        from .entity_filter import is_excluded
        if is_excluded(hass, entity_id):
            return None
    except Exception:
        pass
    from . import reasoning_loop
    friendly_name = new_state.attributes.get("friendly_name") or entity_id
    decision = reasoning_loop._structured_hazard(
        device_class, new_state.state,
        f"{friendly_name} ({entity_id}) changed from {old_state.state} to {new_state.state}",
        entity_id, friendly_name, _m_common._live_honorific(hass))
    if not decision or not decision.get("speak") or not decision.get("message"):
        return None
    return {
        "type": ACTION_TYPE,
        "urgency": "critical",
        "message": decision["message"],
        "entity_id": entity_id,
    }


async def async_announce(hass: HomeAssistant, config: dict, action: dict) -> None:
    """Deliver one hazard action through the core's normal alert path."""
    sleeping = False
    try:
        from . import sleep_detection
        sleeping, _ = sleep_detection.is_sleeping(
            hass,
            bedroom_area_ids=config.get("bedroom_areas", []) or [],
            quiet_start=config.get("observer_quiet_start", "22:00"),
            quiet_end=config.get("observer_quiet_end", "07:00"),
        )
    except Exception:
        sleeping = False
    try:
        await _m_delivery._emit_action(hass, config, action, sleeping)
    except Exception as exc:
        _LOGGER.error("Nova: hazard alarm delivery failed for %s: %s",
                      action.get("entity_id"), exc)


def on_state_changed(hass: HomeAssistant, config: dict, event: Event) -> bool:
    """Schedule the announcement when this event is a new hazard alarm.
    Returns True when one was scheduled."""
    try:
        action = hazard_action(hass, event)
    except Exception as exc:
        _LOGGER.warning("Nova: hazard check failed: %s", exc)
        return False
    if action is None:
        return False
    _LOGGER.warning("Nova: %s alarm from %s", ACTION_TYPE, action["entity_id"])
    hass.async_create_task(async_announce(hass, config, action))
    return True
