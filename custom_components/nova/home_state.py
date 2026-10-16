"""Plain-text snapshot of the home for the conversation system prompt.

Covers the time, occupancy, room temperatures, lights, unlocked locks, open
covers, open doors and windows, alarm state and playing media. The result is
cached for a minute. Read-only: nothing here acts on the home.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant

from .prompt_fence import one_line

_LOGGER = logging.getLogger(__name__)

_cache: dict = {"summary": "", "ts": 0.0}
_TTL = 60.0

_ROOM_WORDS = ("room", "bedroom", "kitchen", "living", "garage", "office",
               "hallway", "basement")
_OPENING_CLASSES = ("door", "window", "garage_door")


def get_home_summary(hass: HomeAssistant) -> str:
    """The cached snapshot, rebuilt when older than the TTL."""
    now = time.time()
    if now - _cache["ts"] < _TTL and _cache["summary"]:
        return _cache["summary"]
    summary = _build_summary(hass)
    _cache["summary"] = summary
    _cache["ts"] = now
    return summary


def _area_name(hass: HomeAssistant, area_id: str) -> str:
    """Human name of an area, or its id."""
    try:
        from homeassistant.helpers import area_registry as ar
        area = ar.async_get(hass).async_get_area(area_id)
        return area.name if area else area_id
    except Exception:  # noqa: BLE001
        return area_id


def _name(state: Any) -> str:
    return one_line(state.attributes.get("friendly_name", state.entity_id))


def _occupancy_line(hass: HomeAssistant) -> str:
    from . import audio_routing
    areas = audio_routing.currently_occupied_areas(hass)
    if areas:
        return "Occupied areas: " + ", ".join(_area_name(hass, a) for a in areas)
    if audio_routing.anyone_home(hass):
        return "Someone is home but no specific room occupancy detected"
    return "No one appears to be home"


def _temperature_items(hass: HomeAssistant) -> list[str]:
    try:
        default_unit = hass.config.units.temperature_unit
    except Exception:  # noqa: BLE001
        default_unit = "°F"
    items: list[str] = []
    for state in hass.states.async_all("sensor"):
        if state.attributes.get("device_class") != "temperature":
            continue
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            continue
        name = _name(state)
        unit = state.attributes.get("unit_of_measurement", default_unit)
        lowered = name.lower()
        if any(word in lowered for word in _ROOM_WORDS):
            items.append(f"{name}: {'%.0f' % value}{unit}")
    return items


def _build_summary(hass: HomeAssistant) -> str:
    """Assemble the snapshot lines (time line first)."""
    lines: list[str] = []

    try:
        lines.append(_occupancy_line(hass))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova home state: occupancy unavailable: %s", exc)

    temps = _temperature_items(hass)
    if temps:
        lines.append("Temperatures: " + "; ".join(temps[:6]))

    lights = hass.states.async_all("light")
    lit = [_name(s) for s in lights if s.state == "on"]
    if lit:
        lines.append(f"Lights on ({len(lit)}/{len(lights)}): " + ", ".join(lit[:8]))
    else:
        lines.append(f"All {len(lights)} lights are off")

    from .cognitive_core import _lockdown_exempt_locks
    exempt = _lockdown_exempt_locks()
    unlocked = [_name(s) for s in hass.states.async_all("lock")
                if s.state == "unlocked" and s.entity_id not in exempt]
    if unlocked:
        lines.append("Unlocked: " + ", ".join(unlocked))

    covers = [_name(s) for s in hass.states.async_all("cover") if s.state == "open"]
    if covers:
        lines.append("Open covers: " + ", ".join(covers))

    openings = [_name(s) for s in hass.states.async_all("binary_sensor")
                if s.attributes.get("device_class") in _OPENING_CLASSES
                and s.state == "on"]
    if openings:
        lines.append("Open doors/windows: " + ", ".join(openings[:6]))

    from . import alarm_source
    for panel in alarm_source.states(hass):
        lines.append(f"Alarm: {panel.state}")

    media = []
    for state in hass.states.async_all("media_player"):
        if state.state != "playing":
            continue
        item = _name(state)
        title = state.attributes.get("media_title")
        if title:
            item += f" ({one_line(title)})"
        media.append(item)
    if media:
        lines.append("Playing media: " + ", ".join(media[:4]))

    from homeassistant.util import dt as dt_util
    from .locale_format import format_date
    now = dt_util.now()
    lines.insert(
        0,
        f"Current time: {now.strftime('%I:%M %p')}, "
        f"{format_date(now, hass, pad_month_first_day=True)}",
    )
    return "\n".join(lines)
