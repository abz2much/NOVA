"""Room, satellite and camera helpers for the panel (nova/get_panel_data).

They read the area, entity and device registries and the state machine to
build the room cards and the setup lists: satellites, cast speakers,
cameras and the first run checklist. Nothing from Home Assistant is
imported at import time. websocket.py imports these names, so they are
also reachable through it.

Finding a room's entities means walking the whole entity registry. One
panel refresh asks that for every room several times over, so a request
can wrap its work in ``area_scan(hass)``: the registry is then walked once
and every room is looked up in that index. Outside the wrapper each call
walks the registry itself, exactly as before.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from typing import TYPE_CHECKING, Iterator, Optional

from . import audio_routing

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# The current request's room index: {"hass": hass, "rooms": {area: [ids]}},
# where "rooms" stays None until first needed. Per task, so two panel
# requests never share one.
_SCAN: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "nova_area_scan", default=None)

# Capability codes in the order the panel shows them.
_CAPABILITY_ORDER = ("sat", "spkr", "mmwave", "cam", "light", "switch", "lock",
                     "climate", "door", "leak", "alarm")
_DOMAIN_CAPABILITY = {"assist_satellite": "sat", "media_player": "spkr", "camera": "cam",
                      "light": "light", "switch": "switch", "lock": "lock",
                      "climate": "climate"}
_SENSOR_CAPABILITY = {
    "occupancy": "mmwave", "motion": "mmwave", "presence": "mmwave",
    "door": "door", "window": "door", "garage_door": "door", "opening": "door",
    "moisture": "leak",
    "smoke": "alarm", "gas": "alarm", "carbon_monoxide": "alarm",
    "safety": "alarm", "tamper": "alarm", "problem": "alarm",
}
_PRESENCE_CLASSES = ("occupancy", "motion", "presence")
_OUTDOOR_WORDS = ("yard", "garden", "driveway", "patio", "deck", "porch",
                  "pool", "outdoor", "outside", "exterior", "lawn")


@contextlib.contextmanager
def area_scan(hass: HomeAssistant) -> Iterator[None]:
    """Within this block, every room lookup for ``hass`` shares one walk of
    the entity registry."""
    token = _SCAN.set({"hass": hass, "rooms": None})
    try:
        yield
    finally:
        _SCAN.reset(token)


def _area_name(hass: HomeAssistant, area_id: str) -> str:
    """An area's friendly name, else its id."""
    try:
        from homeassistant.helpers import area_registry as ar
        area = ar.async_get(hass).async_get_area(area_id)
    except Exception:
        return area_id
    return (area.name or area_id) if area else area_id


def _rooms(hass: HomeAssistant) -> dict[str, list[str]]:
    """Every area's entity ids, from one walk of the registry. An entity
    with no area of its own takes its device's."""
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    devices = dr.async_get(hass)
    rooms: dict[str, list[str]] = {}
    for entry in er.async_get(hass).entities.values():
        area = entry.area_id
        if not area and entry.device_id:
            device = devices.async_get(entry.device_id)
            area = device.area_id if device else None
        if area:
            rooms.setdefault(area, []).append(entry.entity_id)
    return rooms


def _entities_in_area(hass: HomeAssistant, area_id: str) -> list[str]:
    """The entity ids in an area, by the entity's own area or its device's,
    without the ones the user excluded."""
    scan = _SCAN.get()
    if scan is None or scan["hass"] is not hass:
        rooms = _rooms(hass)
    else:
        if scan["rooms"] is None:
            scan["rooms"] = _rooms(hass)
        rooms = scan["rooms"]
    try:
        from .entity_filter import is_excluded
    except Exception:
        return list(rooms.get(area_id, []))
    # Excluded entities are never shown or counted on a room card.
    return [e for e in rooms.get(area_id, []) if not is_excluded(hass, e)]


def _area_capabilities(hass: HomeAssistant, area_id: str) -> list[str]:
    """The capability codes present in an area (the panel draws each as an
    icon and a label), in the panel's order."""
    found: set[str] = set()
    for entity_id in _entities_in_area(hass, area_id):
        domain = entity_id.split(".", 1)[0]
        if domain == "binary_sensor":
            state = hass.states.get(entity_id)
            kind = state.attributes.get("device_class") if state else None
            code = _SENSOR_CAPABILITY.get(kind)
        else:
            code = _DOMAIN_CAPABILITY.get(domain)
        if code:
            found.add(code)
    return [code for code in _CAPABILITY_ORDER if code in found]


def _is_outdoor_area(hass: HomeAssistant, area_id: str) -> bool:
    """A rough guess: does the area's name sound like outside?"""
    name = (_area_name(hass, area_id) or "").lower()
    return any(word in name for word in _OUTDOOR_WORDS)


def _dominant_area(hass: HomeAssistant) -> str | None:
    """The liveliest occupied area: the one whose presence sensor changed
    most recently. Indoor areas win over outdoor ones (nobody lives in the
    garden); outdoor ones count only when nothing indoors is occupied."""
    occupied = audio_routing.currently_occupied_areas(hass)
    if not occupied:
        return None
    indoor = [a for a in occupied if not _is_outdoor_area(hass, a)]
    choices = indoor or [a for a in occupied if _is_outdoor_area(hass, a)]
    liveliest, latest = None, 0.0
    for area_id in choices:
        for entity_id in audio_routing.presence_entities_in_area(hass, area_id):
            state = hass.states.get(entity_id)
            try:
                changed = state.last_changed.timestamp() if state is not None else None
            except Exception:
                changed = None
            if changed is not None and changed > latest:
                liveliest, latest = area_id, changed
    return liveliest or choices[0]


def _area_light_state(hass: HomeAssistant, area_id: str) -> tuple[int, int]:
    """(lights on, lights known) in an area, for the room's light toggle on
    the floor plan."""
    lit = known = 0
    for entity_id in _entities_in_area(hass, area_id):
        if not entity_id.startswith("light."):
            continue
        state = hass.states.get(entity_id)
        if state is not None:
            known += 1
            lit += state.state == "on"
    return lit, known


def _area_temp_humidity_entities(hass: HomeAssistant, area_id: str) -> tuple[Optional[str], Optional[str]]:
    """The first temperature and the first humidity sensor in an area (or
    None). The room cards and the sparkline history both use this, so they
    always agree."""
    temperature = humidity = None
    for entity_id in _entities_in_area(hass, area_id):
        if temperature and humidity:
            break
        if not entity_id.startswith("sensor."):
            continue
        state = hass.states.get(entity_id)
        kind = state.attributes.get("device_class") if state is not None else None
        if kind == "temperature" and temperature is None:
            temperature = entity_id
        elif kind == "humidity" and humidity is None:
            humidity = entity_id
    return temperature, humidity


def _shown_temperature(state) -> Optional[str]:
    try:
        degrees = int(round(float(state.state)))
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement", "")
    return f"{degrees}°{unit.replace('°', '')[:1] or 'F'}"


def _shown_humidity(state) -> Optional[str]:
    try:
        return f"{int(round(float(state.state)))}%"
    except (TypeError, ValueError):
        return None


def _area_live_readings(hass: HomeAssistant, area_id: str) -> dict:
    """A room card's live line: temperature, humidity, lights and how long
    since motion."""
    from .entity_filter import is_object_sensor
    temp = humidity = None
    lit = lights = 0
    since_motion = None
    for entity_id in _entities_in_area(hass, area_id):
        state = hass.states.get(entity_id)
        if state is None:
            continue
        domain = entity_id.split(".", 1)[0]
        kind = state.attributes.get("device_class")
        if domain == "sensor" and kind == "temperature" and temp is None:
            temp = _shown_temperature(state)
        elif domain == "sensor" and kind == "humidity" and humidity is None:
            humidity = _shown_humidity(state)
        elif domain == "light":
            lights += 1
            lit += state.state == "on"
        elif (domain == "binary_sensor" and kind in _PRESENCE_CLASSES
              and not is_object_sensor(entity_id, state.attributes.get("friendly_name"))):
            # A camera's car, animal or parcel sensor is not motion in the
            # room, so it never sets the "last motion" age.
            try:
                age = time.time() - state.last_changed.timestamp()
            except Exception:
                continue
            if since_motion is None or age < since_motion:
                since_motion = age
    shown = None
    if lights == 1:
        shown = "ON" if lit else "OFF"
    elif lights > 1:
        shown = f"{lit}/{lights}"
    return {"temp": temp, "humidity": humidity, "lights": shown,
            "last_motion_seconds": since_motion}


def _format_duration(seconds: float | None) -> str:
    """Seconds as the panel's short age: 45s, 12m, 3h."""
    if seconds is None:
        return "—"
    for size, suffix in ((3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{suffix}"
    return f"{int(seconds)}s"


def _satellite_count(hass: HomeAssistant) -> tuple[int, int]:
    """(satellites answering, satellites known)."""
    states = hass.states.async_all("assist_satellite")
    up = sum(1 for s in states if s.state not in ("unavailable", "unknown"))
    return up, len(states)


def _get_satellites(hass: HomeAssistant) -> list[dict]:
    """Each satellite's entity_id, name and area (its device's area)."""
    def row(state, area: str) -> dict:
        return {"entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name", state.entity_id),
                "area": area}
    satellites = hass.states.async_all("assist_satellite")
    try:
        from homeassistant.helpers import area_registry as ar
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        entities, devices, areas = er.async_get(hass), dr.async_get(hass), ar.async_get(hass)
        rows = []
        for state in satellites:
            entry = entities.async_get(state.entity_id)
            device = devices.async_get(entry.device_id) if entry and entry.device_id else None
            area = ""
            if device and device.area_id:
                found = areas.async_get_area(device.area_id)
                area = found.name if found else device.area_id
            rows.append(row(state, area))
        return rows
    except Exception:
        return [row(state, "") for state in hass.states.async_all("assist_satellite")]


def _text_map(key: str) -> dict:
    """A {text: text} map saved in the runtime config, or {}."""
    try:
        from . import nova_config
        saved = nova_config.get(key, {}) or {}
    except Exception:
        return {}
    if not isinstance(saved, dict):
        return {}
    return {str(k): str(v) for k, v in saved.items()}


def _get_camera_overrides() -> dict:
    """camera_overrides (camera -> the camera its frames come from), so the
    panel resolves sources the same way the server does. Never raises."""
    return _text_map("camera_overrides")


def _get_camera_names() -> dict:
    """camera_names (camera -> the name Nova alone uses for it). Never
    raises."""
    return _text_map("camera_names")


def _saved(key: str, default=None):
    """A runtime setting, or ``default`` when it cannot be read."""
    try:
        from . import nova_config
        return nova_config.get(key, default)
    except Exception:
        return default


def _get_onboarding_state(hass: HomeAssistant, entry, current_notify: str) -> dict:
    """The first run checklist: which useful setup steps are done, so the
    panel can welcome a new household and step aside once they are set up
    or dismiss it. The model key was already taken by the setup screens;
    this covers "what now?" after install."""
    dismissed = bool(_saved("onboarding_dismissed", False))
    has_notify = bool(current_notify)
    try:
        has_cameras = bool(_get_cameras(hass))
    except Exception:
        has_cameras = False
    # Voice counts only when Setup Doctor's own pipeline check passes (an
    # Assist pipeline really using Nova), not just because a satellite
    # exists. The check is cheap and makes no model call, so it is safe on
    # the panel's 20 second poll.
    try:
        from . import setup_health
        voice = setup_health._check_assist_pipeline(hass).get("status") == "ok"
    except Exception:
        voice = False
    briefings = (bool(_saved("briefing_morning_enabled", False))
                 or bool(_saved("briefing_evening_enabled", False)))
    steps = [
        {"id": "notify", "done": has_notify, "jump": "Notifications",
         "label": "Set an alert destination",
         "hint": "Where Nova sends security alerts and notifications (your phone)."},
        {"id": "cameras", "done": has_cameras, "jump": "Cameras",
         "label": "Connect cameras (optional)",
         "hint": "Nest/Frigate cameras enable doorbell analysis, package detection, the live floor plan."},
        {"id": "voice", "label": "Set up voice (optional)",
         "hint": "On HA OS/Supervised Nova installs the voice stack for you. Done when "
                 "an Assist pipeline uses Nova as its conversation agent "
                 "(Settings → Voice assistants).",
         "done": voice},
        {"id": "banter", "label": "Pick a personality level",
         "hint": "Plain, dry, or full — how much of Nova's quiet wit comes through. Settings → Character.",
         "jump": "Nova Character", "done": _saved("banter_level", None) is not None},
        {"id": "briefings", "label": "Turn on daily briefings",
         "hint": "Morning and evening summaries of weather, calendar, overnight "
                 "events, and energy, in Settings under Briefings. Anticipation "
                 "and cross-session memory live under Anticipation and Memory.",
         "jump": "Briefings", "done": briefings},
    ]
    done = sum(1 for step in steps if step["done"])
    fresh = bool(_saved("welcome_pending", False))
    return {
        "dismissed": dismissed,
        "show": not dismissed and (not has_notify or done < 2),
        # A fresh install: the panel also keeps the card up while Setup
        # Doctor reports problems, until it is dismissed.
        "fresh": fresh,
        "steps": steps,
        "done_count": done,
        "total": len(steps),
    }


def _get_cameras(hass: HomeAssistant) -> list[dict]:
    """Cameras for the picker and chips, by name. ``name`` follows Nova's own
    camera_names; ``raw_name`` keeps Home Assistant's name so the rename box
    can show what clearing it goes back to."""
    names = _get_camera_names()
    cameras = []
    try:
        from . import outdoor
        from .camera import _disabled_cameras, display_name
        indoor_list = outdoor._cfg_list("indoor_entities")
        outdoor_list = outdoor._cfg_list("outdoor_entities")
        off = _disabled_cameras()
        for state in hass.states.async_all("camera"):
            entity_id = state.entity_id
            shown = state.attributes.get("friendly_name", entity_id)
            cameras.append({
                "entity_id": entity_id,
                "name": display_name(entity_id, shown, names),
                "raw_name": shown,
                "enabled": entity_id not in off,
                # Inside or outside matters to the whole cognitive stack
                # (intrusion, notable events, the motion scan).
                "outdoor": outdoor.is_outdoor(hass, entity_id, shown),
                "location_mode": outdoor.location_mode(entity_id, indoor_list, outdoor_list),
            })
    except Exception:
        pass
    return sorted(cameras, key=lambda c: c["name"])


_CAST_HINTS = ("google", "nest", "lenovo", "sonos")
_PLAYS_MEDIA = 16384


def _get_cast_devices(hass: HomeAssistant) -> list[dict]:
    """Media players that look like cast speakers or groups, unless
    unavailable."""
    found = []
    for state in hass.states.async_all("media_player"):
        entity_id = state.entity_id
        shown = state.attributes.get("friendly_name", entity_id)
        lowered = shown.lower()
        castable = ("cast" in state.attributes.get("platform", "").lower()
                    or any(hint in lowered for hint in _CAST_HINTS)
                    or "group" in entity_id
                    or state.attributes.get("supported_features", 0) & _PLAYS_MEDIA)
        if castable and state.state != "unavailable":
            found.append({"entity_id": entity_id, "name": shown})
    return found


def _all_areas_with_anything(hass: HomeAssistant) -> list[str]:
    """Areas with at least one thing the room cards show (a satellite,
    speaker, sensor, light and so on)."""
    try:
        from homeassistant.helpers import area_registry as ar
        area_ids = [area.id for area in ar.async_get(hass).async_list_areas()]
    except Exception:
        return []
    return [area_id for area_id in area_ids if _area_capabilities(hass, area_id)]


def _get_all_people(hass: HomeAssistant) -> list[dict]:
    """Every person, home or away, so the honorifics card lists the whole
    household and not only whoever is in when the panel loads."""
    try:
        from . import honorific
        return honorific.all_people(hass)
    except Exception:
        return []


def _get_speaker_assignable_areas(hass: HomeAssistant) -> list[dict]:
    """The areas the Room Speakers card can give a speaker to (the same set
    as _all_areas_with_anything), with their names."""
    try:
        from homeassistant.helpers import area_registry as ar
        registry = ar.async_get(hass)
        rows = []
        for area_id in _all_areas_with_anything(hass):
            area = registry.async_get_area(area_id)
            rows.append({"area_id": area_id, "name": area.name if area else area_id})
        return rows
    except Exception:
        return []
