"""
Nova — situational snapshot (v6.87.0).

Composites the live signals Nova already tracks — time, who's home, weather,
the next calendar events, current power draw, and recent activity — into one
concise picture for the agent's system prompt, so judgments are grounded in the
current situation rather than a static device inventory alone.

Each signal is gathered independently and guarded: a missing or failing source
is omitted, never an error. Dependency-light and synchronous (cheap state
reads); the agent calls it in the executor alongside the home-context build.
"""
from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


def _time(hass) -> str:
    try:
        from homeassistant.util import dt as dt_util
        dt = dt_util.now()
        return "Time: " + dt.strftime("%A") + " " + dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return ""


def _presence(hass) -> str:
    """Who is home, by the same rule as the rest of Nova (household.py,
    8.46.0): a person counts as home when they, or a device tracker linked
    to them, read home; a person who cannot be read is unknown, never away."""
    try:
        from . import household
        people = list(hass.states.async_all("person"))
        if not people:
            return ""
        bits = []
        for person in people:
            name = person.attributes.get("friendly_name") or person.entity_id.split(".", 1)[-1]
            reading = household._person_reading(hass, person)
            where = str(person.state or "")
            if reading == household.HOME:
                bits.append(f"{name} is home")
            elif reading == household.UNKNOWN:
                bits.append(f"{name}'s whereabouts are unknown")
            elif where in ("not_home", "away"):
                bits.append(f"{name} is away")
            else:
                bits.append(f"{name} is at {where.replace('_', ' ')}")
        line = "Presence: " + ". ".join(bits) + "."
        try:
            from . import presence
            rooms = (presence.get_presence_summary(hass) or {}).get("rooms") or {}
            if rooms:
                line += f" Occupied areas: {', '.join(rooms)}."
        except Exception:
            pass
        return line
    except Exception:
        return ""


def _weather(hass) -> str:
    try:
        states = list(hass.states.async_all("weather"))
        if not states:
            return ""
        st = states[0]
        cond = str(st.state or "").replace("_", " ").strip()
        temp = st.attributes.get("temperature")
        unit = st.attributes.get("temperature_unit", "")
        if cond and temp is not None:
            return "Weather: %s, %s%s" % (cond, temp, unit)
        return ("Weather: " + cond) if cond else ""
    except Exception:
        return ""


def _calendar(hass) -> str:
    try:
        from . import comms
        ag = comms.agenda(hass, 12) or {}
        evs = ag.get("events") or []
        if not evs:
            return ""
        line = "Next up: " + "; ".join(evs[:2])
        confs = ag.get("conflicts") or []
        if confs:
            line += " (" + confs[0] + ")"
        return line
    except Exception:
        return ""


def _energy(hass) -> str:
    try:
        from . import energy
        ps = energy.power_status(hass) or {}
        kw = ps.get("kw")
        if kw is None:
            return ""
        return "Power: %.1f kW%s" % (kw, " — over peak" if ps.get("over_peak") else "")
    except Exception:
        return ""


def _activity(hass) -> str:
    try:
        from . import observer
        s = (observer.get_recent_context(600) or "").strip()
        if not s or s.startswith("quiet"):
            return ""
        lines = [ln.strip() for ln in s.splitlines() if ln.strip()][-3:]
        return "Recent activity: " + "; ".join(lines)
    except Exception:
        return ""


def snapshot(hass) -> str:
    """One concise situational block composited from the live signals. Empty
    string when nothing meaningful is available. Never raises."""
    parts = []
    for fn in (_time, _presence, _weather, _calendar, _energy, _activity):
        try:
            line = fn(hass)
        except Exception:
            line = ""
        if line:
            parts.append(line)
    return "\n".join(parts)
