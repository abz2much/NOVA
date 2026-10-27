"""The chosen alarm going off, announced by the cognitive core (8.83.0).

Before, the observer turned it into "a security alert has been triggered.
Immediate attention required." with nothing about where, and said the same
when the alarm only began its entry countdown (pending) as a resident came
home. The alarm itself decides whether someone got in (8.81.0); Nova adds
what the alarm cannot: which sensor tripped, read from the alarm's
open_sensors (Alarmo sets it), and what the camera for that room shows,
checked by Nova's own vision.

One critical action: spoken everywhere and pushed to every phone with the
snapshot. The camera check is capped at CAMERA_WAIT_S, and a slow or broken
one never stops the alert.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.core import Event, HomeAssistant

from . import core_common as _m_common

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

ACTION_TYPE = "alarm_triggered"
CAMERA_WAIT_S = 8.0
_NOT_A_CHANGE = ("triggered", "unavailable", "unknown")


def went_off(hass: HomeAssistant, config: dict, event: Event) -> bool:
    """The chosen alarm has just entered triggered. A restart reading it
    again, the entry countdown and any other alarm panel do not count."""
    entity_id = event.data.get("entity_id", "")
    old, new = event.data.get("old_state"), event.data.get("new_state")
    if not entity_id.startswith("alarm_control_panel.") or old is None or new is None:
        return False
    if new.state != "triggered" or str(old.state).lower() in _NOT_A_CHANGE:
        return False
    from . import alarm_source
    return alarm_source.is_selected(hass, entity_id, config)


def tripped(state) -> list:
    """The sensors the alarm says tripped it, from open_sensors (a dict of
    entity id to state, or a list of ids)."""
    value = state.attributes.get("open_sensors")
    if isinstance(value, dict):
        return [str(eid) for eid in value]
    if isinstance(value, (list, tuple)):
        return [str(eid) for eid in value]
    return []


def _name(hass, entity_id: str) -> str:
    try:
        state = hass.states.get(entity_id)
        return (state.attributes.get("friendly_name") if state else None) or entity_id
    except Exception:
        return entity_id


async def _camera_answer(safety, entity_id: Optional[str]) -> dict:
    """What the camera for the tripped sensor's room shows, or {} when there
    is no camera, no safety manager, or the check is slow or fails."""
    if safety is None or not entity_id:
        return {}
    try:
        return await asyncio.wait_for(safety.camera_answer(entity_id), CAMERA_WAIT_S) or {}
    except Exception as exc:
        _LOGGER.info("alarm: camera check skipped (%s)", exc or type(exc).__name__)
        return {}


async def alarm_action(hass: HomeAssistant, config: dict, state, safety=None) -> dict:
    """The critical action for the alarm going off."""
    sensors = tripped(state)
    first = sensors[0] if sensors else None
    where = ", ".join(_name(hass, eid) for eid in sensors)
    text = f"the alarm is going off: {where}." if where else "the alarm is going off."
    answer = await _camera_answer(safety, first)
    if answer.get("camera"):
        vision = answer.get("vision")
        if vision is True:
            text += " The camera sees a person."
        elif vision is False:
            text += " No one is on the camera."
        else:
            text += " I couldn't tell from the camera."
        if answer.get("snapshot_url"):
            text += " A snapshot is attached."
    action = {
        "type": ACTION_TYPE, "urgency": "critical",
        "message": _m_common._persona().lead_in(_m_common._live_honorific(hass), text),
        "auto_act": False, "notify_all": True, "entity_id": first or "",
    }
    for key in ("camera", "snapshot_url", "snapshot_path"):
        if answer.get(key):
            action[key] = answer[key]
    return action


async def async_announce(hass: HomeAssistant, config: dict, state) -> None:
    from . import core_hazards
    from . import core_state as _m_state
    safety = getattr(_m_state._CORE, "safety_mgr", None)
    try:
        action = await alarm_action(hass, config, state, safety)
    except Exception as exc:
        _LOGGER.error("Nova: building the alarm alert failed, sending it plain: %s", exc)
        action = {"type": ACTION_TYPE, "urgency": "critical", "notify_all": True,
                  "message": "The alarm is going off."}
    await core_hazards.async_announce(hass, config, action)


def on_state_changed(hass: HomeAssistant, config: dict, event: Event) -> bool:
    """Schedule the alert when the chosen alarm has just gone off. Returns
    True when one was scheduled."""
    try:
        if not went_off(hass, config, event):
            return False
    except Exception as exc:
        _LOGGER.warning("Nova: alarm check failed: %s", exc)
        return False
    _LOGGER.warning("Nova: the alarm is going off (%s)", event.data.get("entity_id"))
    hass.async_create_task(async_announce(hass, config, event.data["new_state"]))
    return True
