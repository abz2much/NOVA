"""Keyless drive-time lookup for leave alerts.

A destination string is geocoded with Nominatim (OpenStreetMap) unless it is
already a ``lat,lon`` literal, then routed with OSRM (the base URL can point
at a self-hosted server). Network access is async on Home Assistant's shared
aiohttp session. Every failure yields None and leaves a plain-language reason
for :func:`failure_reason`. Geocode hits are cached for the process lifetime
to stay within Nominatim's usage policy.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_OSRM = "https://router.project-osrm.org"
_TIMEOUT = 8
_UA = "nova-home-assistant (Home Assistant integration)"

_GEO_CACHE: dict = {}
_LAST_FAILURE: dict = {}

_NO_INPUT = "no starting position or no destination"
_GEO_SILENT = "the map search service did not answer"
_GEO_MISS = "the place could not be found on the map"
_ROUTE_SILENT = "the routing service did not answer"
_ROUTE_MISS = "the routing service found no route"

_NUM = r"-?\d+(?:\.\d+)?"
_COORDS_RE = re.compile(rf"\s*({_NUM})\s*,\s*({_NUM})\s*")


def _coords_from_str(s) -> Optional[tuple[float, float]]:
    """``"lat, lon"`` → ``(lat, lon)``; anything else → None."""
    found = _COORDS_RE.fullmatch(str(s or ""))
    if not found:
        return None
    return float(found.group(1)), float(found.group(2))


def _parse_geocode(data) -> Optional[tuple[float, float]]:
    """First Nominatim hit as ``(lat, lon)``, or None."""
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    try:
        return float(first["lat"]), float(first["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def _route_url(origin, dest, base: Optional[str] = None) -> str:
    """OSRM driving-route URL; OSRM takes lon,lat pairs."""
    root = (base or DEFAULT_OSRM).rstrip("/")
    return "%s/route/v1/driving/%f,%f;%f,%f?overview=false" % (
        root, origin[1], origin[0], dest[1], dest[0],
    )


def _parse_route(data) -> Optional[float]:
    """Minutes (one decimal) of the first OSRM route, or None."""
    if (data or {}).get("code") != "Ok":
        return None
    try:
        return round(float(data["routes"][0]["duration"]) / 60, 1)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


async def _get_json(hass, url: str, params: Optional[dict] = None):
    """GET ``url`` and return its JSON body, or None on any problem."""
    try:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
        async with session.get(
            url,
            params=params,
            headers={"User-Agent": _UA},
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Nova travel: request to %s failed: %s", url, exc)
        return None


def _key(location: str) -> str:
    return (location or "").strip().lower()


async def _geocode(hass, location: str) -> Optional[tuple]:
    """Resolve a place name to coordinates, caching hits only."""
    key = _key(location)
    if not key:
        return None
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    data = await _get_json(
        hass, NOMINATIM_URL, {"q": location, "format": "json", "limit": "1"}
    )
    hit = _parse_geocode(data)
    if hit is not None:
        _GEO_CACHE[key] = hit
        return hit
    _LAST_FAILURE[key] = _GEO_SILENT if data is None else _GEO_MISS
    return None


def failure_reason(dest_location: str) -> Optional[str]:
    """Why the last lookup for this destination failed, if it did."""
    return _LAST_FAILURE.get(_key(dest_location))


async def travel_minutes(hass, origin, dest_location: str,
                         osrm_url: Optional[str] = None) -> Optional[float]:
    """Driving minutes from ``origin`` (lat, lon) to ``dest_location``."""
    key = _key(dest_location)
    if not origin or not dest_location:
        _LAST_FAILURE[key] = _NO_INPUT
        return None

    dest: Any = _coords_from_str(dest_location)
    if not dest:
        dest = await _geocode(hass, dest_location)
    if not dest:
        _LAST_FAILURE.setdefault(key, _GEO_MISS)
        return None

    data = await _get_json(hass, _route_url(origin, dest, osrm_url))
    if data is None:
        _LAST_FAILURE[key] = _ROUTE_SILENT
        return None
    minutes = _parse_route(data)
    if minutes is None:
        _LAST_FAILURE[key] = _ROUTE_MISS
        return None
    _LAST_FAILURE.pop(key, None)
    return minutes
