"""
Nova — Presence awareness.

Reads HA's person.* entities, zone states, and (when available) mmWave room
sensors to know who is home and where. Provides a concise summary string for
the conversation agent's context.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def person_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """The name presence (and so identity) uses for a person.* entity: its
    friendly name, else the part after "person.". None when it isn't there.
    Nova Voice ID's person ids map to identity votes through this (8.37.0)."""
    state = hass.states.get(entity_id)
    if not state:
        return None
    return state.attributes.get("friendly_name", entity_id.split(".", 1)[-1])


def _person_state(hass: HomeAssistant, entity_id: str) -> dict:
    """Extract useful data from a person.* entity."""
    state = hass.states.get(entity_id)
    if not state:
        return {}
    return {
        "name":  person_name(hass, entity_id),
        "state": state.state,  # 'home', 'not_home', or a zone name
        "latitude":  state.attributes.get("latitude"),
        "longitude": state.attributes.get("longitude"),
    }


def get_presence_summary(hass: HomeAssistant) -> dict:
    """
    Return a summary of who's home and where.

    Shape:
      {
        "total_people":  int,
        "home_count":    int,
        "away_count":    int,
        "people":        [{"name": "Username", "state": "home", ...}, ...],
        "rooms":         {"kitchen": ["Username"], "office": ["Username2"]},
        "anyone_home":   bool,
      }
    """
    people = []
    for state in hass.states.async_all("person"):
        info = _person_state(hass, state.entity_id)
        if info:
            people.append(info)

    home_count = sum(1 for p in people if p.get("state") == "home")
    away_count = len(people) - home_count

    # Room detection via mmWave sensors — Aqara FP2 exposes sensor.*_presence
    # with occupancy attributes, or binary_sensor.*_occupancy. We scan both.
    rooms: dict[str, list[str]] = {}
    try:
        from .entity_filter import is_excluded as _excl, is_object_sensor as _is_object
    except Exception:
        _excl = lambda _h, _e: False
        _is_object = lambda _e, _n=None: False
    for state in hass.states.async_all("binary_sensor"):
        if state.state != "on":
            continue
        if state.attributes.get("device_class") != "occupancy":
            continue
        if _excl(hass, state.entity_id):
            continue  # user excluded this sensor (e.g. a virtual occupancy sensor)
        name = state.attributes.get("friendly_name", state.entity_id)
        if _is_object(state.entity_id, name):
            continue  # a car, animal or package is not a person (8.14.0)
        # Try to extract the room from the name (e.g. "Kitchen Presence")
        room = name.lower().replace("presence", "").replace("occupancy", "").strip()
        if room:
            rooms.setdefault(room, [])

    return {
        "total_people": len(people),
        "home_count":   home_count,
        "away_count":   away_count,
        "people":       people,
        "rooms":        rooms,
        "anyone_home":  home_count > 0,
    }


def presence_context_string(hass: HomeAssistant) -> str:
    """
    One-line summary suitable for injecting into the LLM system prompt.
    Example: "Username is home. Username2 is away. 3 presence sensors active: kitchen, office."
    """
    data = get_presence_summary(hass)
    if not data["people"]:
        return "No person entities configured in Home Assistant."

    bits: list[str] = []
    for p in data["people"]:
        state = p["state"]
        if state == "home":
            bits.append(f"{p['name']} is home")
        elif state == "not_home":
            bits.append(f"{p['name']} is away")
        else:
            # A zone name like 'Work' or 'School'
            bits.append(f"{p['name']} is at {state.replace('_', ' ')}")

    summary = ". ".join(bits) + "."
    if data["rooms"]:
        summary += f" Occupied areas: {', '.join(data['rooms'].keys())}."
    return summary
