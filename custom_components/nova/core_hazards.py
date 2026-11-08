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

An alarm is only an event when a sensor turns on, so follow_up() (run by the
core's own tick) covers what an event cannot: one already on when Nova starts,
one that stays on, and one that clears. See follow_up.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from homeassistant.core import Event, HomeAssistant

from . import core_common as _m_common
from . import core_delivery as _m_delivery
from .cognitive import evaluators as _evaluators

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

ACTION_TYPE = "critical_hazard"
CLEARED_TYPE = "hazard_cleared"

# While a sensor stays on it is repeated this often, this many times, then left.
REPEAT_EVERY_S = 300.0
MAX_REPEATS = 6
# An all clear is only said for an alarm that was on at least this long, so a
# blip of a second does not make two announcements.
CLEARED_AFTER_S = 60.0
# A sensor that is off, or reads as normal. Anything else that is not an alarm
# (unavailable, unknown) is not an all clear: a dead battery is not one.
_CLEAR_STATES = frozenset({"off", "clear", "dry", "safe"})

# Alarms announced and still on: entity id -> {"since", "last_told", "repeats"}.
# In memory only, so after a restart one still on is announced once more.
_active: dict[str, dict] = {}


def _now() -> float:
    return time.time()


def reset() -> None:
    """Forget every announced alarm (the core is starting)."""
    _active.clear()


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
    friendly_name = new_state.attributes.get("friendly_name") or entity_id
    message = _alarm_sentence(
        hass, device_class, new_state.state, entity_id, friendly_name,
        f"{friendly_name} ({entity_id}) changed from {old_state.state} to {new_state.state}")
    if not message:
        return None
    return {
        "type": ACTION_TYPE,
        "urgency": "critical",
        "message": message,
        "entity_id": entity_id,
    }


def _alarm_sentence(hass: HomeAssistant, device_class: str, state: str, entity_id: str,
                    friendly_name: str, summary: str) -> str:
    """The fixed hazard sentence for a sensor in its alarm state, or ""."""
    from . import reasoning_loop
    decision = reasoning_loop._structured_hazard(
        device_class, state, summary, entity_id, friendly_name,
        _m_common._live_honorific(hass))
    if not decision or not decision.get("speak") or not decision.get("message"):
        return ""
    return decision["message"]


def _excluded(hass: HomeAssistant, entity_id: str) -> bool:
    try:
        from .entity_filter import is_excluded
        return bool(is_excluded(hass, entity_id))
    except Exception:
        return False


def follow_up(hass: HomeAssistant, config: dict) -> list[dict]:
    """What an event cannot tell: called on each core tick, it looks at every
    smoke, carbon monoxide, gas and leak sensor and returns the actions to
    deliver.

    * One already on that was never announced (Nova has just started) is
      announced, as critical, saying it is still active.
    * One that stays on is repeated every REPEAT_EVERY_S, MAX_REPEATS times.
    * One that goes back to a normal reading after CLEARED_AFTER_S or more
      gets one all clear, as a high urgency action that is pushed.
    * One that goes unavailable or unknown is dropped quietly.

    The user's own entity exclusion applies. Never raises."""
    from .cognitive import evaluators
    now = _now()
    actions: list[dict] = []
    seen: set[str] = set()
    for state in list(hass.states.async_all("binary_sensor")):
        device_class = (state.attributes.get("device_class") or "")
        if device_class not in evaluators.CRITICAL_HAZARD_CLASSES:
            continue
        entity_id = state.entity_id
        seen.add(entity_id)
        if _excluded(hass, entity_id):
            _active.pop(entity_id, None)
            continue
        reading = str(state.state).strip().lower()
        name = state.attributes.get("friendly_name") or entity_id
        known = _active.get(entity_id)
        if reading in evaluators.ACTIVE_TRIGGER_STATES:
            if known is None:
                note = " It is still active."
            elif known["repeats"] < MAX_REPEATS and now - known["last_told"] >= REPEAT_EVERY_S:
                minutes = round((now - known["since"]) / 60)
                note = f" It has been active for {minutes} minutes."
            else:
                continue
            sentence = _alarm_sentence(hass, device_class, state.state, entity_id, name,
                                       f"{name} ({entity_id}) is {state.state}")
            if not sentence:
                continue
            if known is None:
                _active[entity_id] = {"since": now, "last_told": now, "repeats": 0}
            else:
                known["repeats"] += 1
                known["last_told"] = now
            actions.append({"type": ACTION_TYPE, "urgency": "critical",
                            "message": sentence + note, "entity_id": entity_id})
        elif reading in _CLEAR_STATES:
            known = _active.pop(entity_id, None)
            if known is not None and now - known["since"] >= CLEARED_AFTER_S:
                actions.append({"type": CLEARED_TYPE, "urgency": "high",
                                "message": f"{name} has cleared and is back to normal.",
                                "entity_id": entity_id})
        else:
            _active.pop(entity_id, None)
    for entity_id in [e for e in _active if e not in seen]:
        del _active[entity_id]
    return actions


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
    # Remembered, so follow_up repeats it while it stays on and says when it clears.
    _active[action["entity_id"]] = {"since": _now(), "last_told": _now(), "repeats": 0}
    _LOGGER.warning("Nova: %s alarm from %s", ACTION_TYPE, action["entity_id"])
    return _m_common.run_on_loop(
        hass, lambda: async_announce(hass, config, action), "nova_hazard_announce")
