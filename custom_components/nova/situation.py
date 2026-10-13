"""The "Situation now" block for the agent's system prompt.

One line each for the time, who is home, the weather, the next calendar
events, the current power draw and recent activity. Every source is read on
its own and guarded: a missing or failing one is simply left out. State reads
only; the agent loop runs this in the executor.
"""
from __future__ import annotations

import functools
import logging
from typing import Callable

_LOGGER = logging.getLogger(__name__)

_Builder = Callable[[object], str]


def _quiet_on_error(build: _Builder) -> _Builder:
    """A source that fails contributes no line."""
    @functools.wraps(build)
    def run(hass) -> str:
        try:
            return build(hass) or ""
        except Exception:
            return ""
    return run


@_quiet_on_error
def _time(hass) -> str:
    from homeassistant.util import dt as dt_util
    now = dt_util.now()
    clock = now.strftime("%I:%M %p").lstrip("0")
    return f"Time: {now.strftime('%A')} {clock}"


def _where(household, hass, person) -> str:
    name = (person.attributes.get("friendly_name")
            or person.entity_id.split(".", 1)[-1])
    reading = household._person_reading(hass, person)
    if reading == household.HOME:
        return f"{name} is home"
    if reading == household.UNKNOWN:
        return f"{name}'s whereabouts are unknown"
    zone = str(person.state or "")
    if zone in ("not_home", "away"):
        return f"{name} is away"
    return f"{name} is at {zone.replace('_', ' ')}"


def _occupied(hass) -> str:
    try:
        from . import presence
        rooms = (presence.get_presence_summary(hass) or {}).get("rooms") or {}
        return f" Occupied areas: {', '.join(rooms)}." if rooms else ""
    except Exception:
        return ""


@_quiet_on_error
def _presence(hass) -> str:
    from . import household
    people = list(hass.states.async_all("person"))
    if not people:
        return ""
    said = ". ".join(_where(household, hass, p) for p in people)
    return f"Presence: {said}." + _occupied(hass)


@_quiet_on_error
def _weather(hass) -> str:
    found = list(hass.states.async_all("weather"))
    if not found:
        return ""
    first = found[0]
    sky = str(first.state or "").replace("_", " ").strip()
    if not sky:
        return ""
    degrees = first.attributes.get("temperature")
    if degrees is None:
        return f"Weather: {sky}"
    return f"Weather: {sky}, {degrees}{first.attributes.get('temperature_unit', '')}"


@_quiet_on_error
def _calendar(hass) -> str:
    from . import comms
    agenda = comms.agenda(hass, 12) or {}
    upcoming = agenda.get("events") or []
    if not upcoming:
        return ""
    clashes = agenda.get("conflicts")
    tail = f" ({clashes[0]})" if clashes else ""
    return "Next up: " + "; ".join(upcoming[:2]) + tail


@_quiet_on_error
def _energy(hass) -> str:
    from . import energy
    status = energy.power_status(hass) or {}
    if status.get("kw") is None:
        return ""
    peak = " — over peak" if status.get("over_peak") else ""
    return f"Power: {status['kw']:.1f} kW{peak}"


@_quiet_on_error
def _activity(hass) -> str:
    from . import observer
    recent = (observer.get_recent_context(600) or "").strip()
    if not recent or recent.startswith("quiet"):
        return ""
    kept = [row.strip() for row in recent.splitlines() if row.strip()][-3:]
    return "Recent activity: " + "; ".join(kept)


def snapshot(hass) -> str:
    """The situation lines joined by newlines ("" when nothing is known)."""
    builders = (_time, _presence, _weather, _calendar, _energy, _activity)
    return "\n".join(line for line in (build(hass) for build in builders) if line)
