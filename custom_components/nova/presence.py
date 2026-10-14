"""Who is home and which rooms are occupied.

People come from Home Assistant's ``person.*`` entities; rooms come from
occupancy binary sensors (mmWave style) that are currently on. The result
is a structured summary for other modules and a one-line description for
the conversation agent's prompt. Read-only: no state writes, no service
calls, no storage. Everything here runs on the event loop.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_ROOM_WORDS = ("presence", "occupancy")
_NO_PEOPLE = "No person entities configured in Home Assistant."


def person_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """Friendly name of a person entity, else the part of the id after the
    first dot; None when the entity has no state."""
    st = hass.states.get(entity_id)
    if not st:
        return None
    return st.attributes.get("friendly_name", entity_id.split(".", 1)[-1])


def _person_state(hass: HomeAssistant, entity_id: str) -> dict:
    st = hass.states.get(entity_id)
    if not st:
        return {}
    attrs = st.attributes
    return {
        "name": person_name(hass, entity_id),
        "state": st.state,
        "latitude": attrs.get("latitude"),
        "longitude": attrs.get("longitude"),
    }


def _filters():
    """The entity filter's two checks, or harmless stand-ins when it cannot
    be imported."""
    try:
        from .entity_filter import is_excluded, is_object_sensor
    except Exception:
        def is_excluded(hass, entity_id, exclusions=None):  # type: ignore[misc]
            return False

        def is_object_sensor(entity_id, friendly_name=None):  # type: ignore[misc]
            return False
    return is_excluded, is_object_sensor


def _room_from(name: str) -> str:
    room = name.lower()
    for word in _ROOM_WORDS:
        room = room.replace(word, "")
    return room.strip()


def _occupied_rooms(hass: HomeAssistant) -> dict:
    excluded, is_object = _filters()
    rooms: dict[str, list] = {}
    for st in hass.states.async_all("binary_sensor"):
        if st.state != "on":
            continue
        if st.attributes.get("device_class") != "occupancy":
            continue
        if excluded(hass, st.entity_id):
            continue
        name = st.attributes.get("friendly_name", st.entity_id)
        if is_object(st.entity_id, name):
            continue
        room = _room_from(name)
        if room:
            rooms.setdefault(room, [])
    return rooms


def get_presence_summary(hass: HomeAssistant) -> dict:
    """People (with their state) and occupied rooms."""
    people = []
    for st in hass.states.async_all("person"):
        entry = _person_state(hass, st.entity_id)
        if entry:
            people.append(entry)
    home = sum(1 for p in people if p["state"] == "home")
    return {
        "total_people": len(people),
        "home_count": home,
        "away_count": len(people) - home,
        "people": people,
        "rooms": _occupied_rooms(hass),
        "anyone_home": home > 0,
    }


def _clause(person: dict) -> str:
    name, state = person["name"], person["state"]
    if state == "home":
        return f"{name} is home"
    if state == "not_home":
        return f"{name} is away"
    return f"{name} is at {state.replace('_', ' ')}"


def presence_context_string(hass: HomeAssistant) -> str:
    """One line for the system prompt, e.g.
    "Alex is home. Sam is away. Occupied areas: kitchen, office." """
    summary = get_presence_summary(hass)
    people = summary["people"]
    if not people:
        return _NO_PEOPLE
    text = ". ".join(_clause(p) for p in people) + "."
    rooms = summary["rooms"]
    if rooms:
        text += " Occupied areas: " + ", ".join(rooms) + "."
    return text
