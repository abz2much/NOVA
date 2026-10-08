"""
Nova — Google Maps Travel Time lookups for leave alerts (8.9.0).

Home Assistant's Google Maps Travel Time integration (when the owner has set
it up) offers two actions that return route times as response data:

  google_travel_time.get_travel_times   mode: driving | walking | bicycling
  google_travel_time.get_transit_times  public transport, optional arrival_time

Nova calls them for the walk, drive and public transport legs of a leave
alert. Home Assistant holds the Google API key; Nova never sees or stores it.
Both actions need the id of a Google Maps Travel Time config entry, and
return {"routes": [{"duration": <seconds>, ...}, ...]}.

Every call can cost money once a Google account's free allowance is used, so
this module keeps a daily call limit and returns None with a plain reason
instead of raising. The caller falls back to other estimates.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Optional

_LOGGER = logging.getLogger(__name__)

DOMAIN = "google_travel_time"
SERVICE_TRAVEL = "get_travel_times"
SERVICE_TRANSIT = "get_transit_times"
DAILY_CAP = 40                  # lookups per local day, a guard against runaway cost
_TIMEOUT = 20                   # seconds to wait for one lookup

# Nova's mode name -> (service, extra data)
_MODES = {
    "walk": (SERVICE_TRAVEL, {"mode": "walking"}),
    "drive": (SERVICE_TRAVEL, {"mode": "driving"}),
    "transit": (SERVICE_TRANSIT, {}),
}

_USAGE = {"day": None, "count": 0}


def calls_today(now: float = None) -> int:
    """How many lookups Nova has made today (local day), for diagnostics."""
    _roll(now)
    return int(_USAGE["count"])


def _roll(now: float = None) -> None:
    import time
    today = datetime.date.fromtimestamp(now or time.time()).toordinal()
    if _USAGE["day"] != today:
        _USAGE["day"] = today
        _USAGE["count"] = 0


def _loaded_entry(hass):
    """The first loaded Google Maps Travel Time config entry, or None."""
    try:
        entries = list(hass.config_entries.async_entries(DOMAIN))
    except Exception:
        return None
    for entry in entries:
        state = getattr(entry, "state", None)
        if str(getattr(state, "value", state)) == "loaded":
            return entry
    return None


def available(hass) -> bool:
    """Whether the integration is set up and both actions exist (they arrived in
    a later Home Assistant release than the integration itself)."""
    if hass is None or _loaded_entry(hass) is None:
        return False
    try:
        return (hass.services.has_service(DOMAIN, SERVICE_TRAVEL)
                and hass.services.has_service(DOMAIN, SERVICE_TRANSIT))
    except Exception:
        return False


def _place(value) -> str:
    """A (lat, lon) pair as the 'lat,lon' text the actions accept; anything else
    (an address) unchanged."""
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return "%.6f,%.6f" % (float(value[0]), float(value[1]))
    return str(value)


def parse_minutes(response) -> Optional[float]:
    """The shortest route in an action response, in minutes. Pure."""
    try:
        routes = (response or {}).get("routes") or []
        seconds = [float(r["duration"]) for r in routes
                   if isinstance(r, dict) and r.get("duration") is not None]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return round(min(seconds) / 60.0, 1) if seconds else None


def _reason_for(exc: Exception) -> str:
    text = str(exc).lower()
    if "permission" in text or "not enabled" in text or "routes api" in text:
        return "the Routes API is not enabled for your Google key"
    if isinstance(exc, asyncio.TimeoutError):
        return "Google did not answer in time"
    return "Google Maps Travel Time returned an error"


async def route_minutes(hass, mode: str, origin, destination: str, *,
                        arrive_by: datetime.datetime = None,
                        now: float = None) -> tuple:
    """Minutes to travel from origin to destination by mode ('walk', 'drive' or
    'transit'), and why not when it cannot be worked out: (minutes, None) or
    (None, reason). arrive_by only applies to transit: the route is chosen to
    arrive by that time of day (Home Assistant moves a time already past today
    to tomorrow, so it must be a future time). Never raises."""
    if mode not in _MODES:
        return None, "unknown travel mode"
    entry = _loaded_entry(hass)
    if entry is None:
        return None, "the Google Maps Travel Time integration is not set up"
    _roll(now)
    if _USAGE["count"] >= DAILY_CAP:
        return None, "the daily limit of %d Google lookups was reached" % DAILY_CAP
    service, extra = _MODES[mode]
    data = {"config_entry_id": entry.entry_id, "origin": _place(origin),
            "destination": _place(destination), **extra}
    if mode == "transit" and arrive_by is not None:
        data["arrival_time"] = arrive_by.strftime("%H:%M:%S")
    _USAGE["count"] += 1
    try:
        response = await asyncio.wait_for(
            hass.services.async_call(DOMAIN, service, data,
                                     blocking=True, return_response=True),
            timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - any failure means "no estimate"
        _LOGGER.debug("Google %s lookup failed: %s", mode, exc)
        return None, _reason_for(exc)
    minutes = parse_minutes(response)
    if minutes is None:
        return None, "Google found no %s route" % mode
    return minutes, None
