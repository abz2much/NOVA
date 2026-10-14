"""Nova's hazard monitor: weather warnings plus three public hazard feeds.

Weather warnings come from one source at a time, chosen in the panel: Met
Éireann for chosen counties (hazard_met_eireann), the US National Weather
Service, or a custom CAP feed (hazard_cap). Met Éireann and CAP warnings share
hazard_warnings' lifecycle: told once, told again when the level moves, and a
phone notice when cancelled. Pushing starts at hazard_push_level (yellow by
default), speaking at hazard_speak_level (orange). What was told is kept on
disk across restarts.

The other feeds need no key and are polled on a timer; only new, nearby,
significant events are passed on:

  - USGS earthquakes inside a radius and above a magnitude
  - NWS active alerts at the home point (the "us" source)
  - NASA EONET open natural events (wildfires, volcanoes, storms) nearby

An Irish home (country IE, or a home point on the island with no country set)
has Met Éireann on and these feeds off until the user saves otherwise.

The centre is Home Assistant's home point unless the panel sets a latitude and
longitude. Every feed remembers the ids it has passed on, so a standing event
is told once. A failed fetch is logged and skipped, never turned into an
alert. NWS turns away requests without a User-Agent, so one is always sent.

Nothing runs while hazard_monitor_enabled is off."""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

_USGS_FDSN = "https://earthquake.usgs.gov/fdsnws/event/1/query"
_NWS_ACTIVE = "https://api.weather.gov/alerts/active"
_EONET_EVENTS = "https://eonet.gsfc.nasa.gov/api/v3/events"

_LOGGER = logging.getLogger(__name__)

# Names the project, never the user.
_USER_AGENT = "Nova Home Assistant hazard monitor (github.com/abz2much/nova)"

# Defaults, each one a setting.
_DEF_QUAKE_RADIUS_KM = 300.0
_DEF_QUAKE_MIN_MAG = 2.5
_DEF_DISASTER_RADIUS_KM = 300.0
_DEF_DISASTER_DAYS = 3
_DEF_WX_SEVERITIES = ("Extreme", "Severe")
_DEF_PUSH_LEVEL = "yellow"
_DEF_SPEAK_LEVEL = "orange"
_DEF_NIGHT_SPEAK_LEVEL = "red"

# Ids each feed has passed on during this run. Past the cap a feed sheds about
# half its ids, whichever the set gives up first.
_SEEN: dict = {"quake": set(), "wx": set(), "disaster": set()}
_SEEN_CAP = 500

# The delivery source for each kind of alert.
_ACTION = {"quake": "hazard_earthquake", "wx": "hazard_weather",
           "disaster": "hazard_disaster", "warning": "hazard_weather"}

# Unsaved on/off settings: (Irish home, anywhere else).
_REGION_DEFAULTS = {
    "hazard_met_eireann_on": (True, False),
    "hazard_quakes_on": (False, True),
    "hazard_weather_on": (False, True),
    "hazard_disasters_on": (False, True),
}
HAZARD_SOURCES = ("met_eireann", "us", "custom")
_LEVELS = ("yellow", "orange", "red")
_EARTH_RADIUS_KM = 6371.0
_KM_PER_DEGREE = 111.0

# The warnings told so far ({source: {id: entry}}), read from disk on the first
# poll, and each source's live warnings from its last good poll (the panel's).
_STORE: Optional[dict] = None
_LAST_WARNINGS: dict = {}


# ── settings ────────────────────────────────────────────────────────────────

def _saved(key: str):
    """The user's own saved value; None when there is none."""
    try:
        from . import nova_config
        return nova_config.get(key, None)
    except Exception:
        return None


def _cfg(key: str, default=None):
    """A saved value, or the default when it is unset or None."""
    try:
        from . import nova_config
        value = nova_config.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _enabled() -> bool:
    return bool(_cfg("hazard_monitor_enabled", False))


def _clean(value) -> str:
    return str(value or "").strip().lower()


def _level_setting(key: str, default: str) -> str:
    level = _clean(_saved(key))
    return level if level in _LEVELS else default


def _night_level_setting() -> str:
    level = _clean(_saved("hazard_night_speak_level"))
    return level if level in (*_LEVELS, "off") else _DEF_NIGHT_SPEAK_LEVEL


def _json_list(key: str) -> list:
    """A saved list of strings, stored as a list or as JSON text."""
    import json
    value = _saved(key)
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _runtime(hass, key: str, default):
    """The panel's live config, as package_monitor reads it."""
    from .runtime import domain_runtime_config
    live = domain_runtime_config(hass)
    return live[key] if key in live else default


def _in_quiet_hours(hass) -> bool:
    """delivery's quiet hours; a bad read counts as not quiet."""
    from . import delivery
    return delivery.in_quiet_hours(hass)


def _config_attr(hass, name: str, default=None):
    try:
        return getattr(hass.config, name, default)
    except Exception:
        return default


def _tz(hass) -> Optional[str]:
    try:
        return hass.config.time_zone
    except Exception:
        return None


def _lang(hass) -> str:
    try:
        return str(hass.config.language or "")
    except Exception:
        return ""


# ── where home is ───────────────────────────────────────────────────────────

def base_home_latlon(hass) -> Optional[tuple[float, float]]:
    """Home Assistant's own home point (zone.home first), ignoring Nova's
    override."""
    try:
        zone = hass.states.get("zone.home")
        point = (zone.attributes.get("latitude"), zone.attributes.get("longitude")) if zone else ()
        if point and None not in point:
            return float(point[0]), float(point[1])
    except Exception:
        pass
    try:
        config = hass.config
        return float(config.latitude), float(config.longitude)
    except Exception:
        return None


def _home_latlon(hass) -> Optional[tuple[float, float]]:
    """The point to watch around: the panel's override when both halves are
    set and numeric, else home."""
    lat, lon = _cfg("hazard_lat", None), _cfg("hazard_lon", None)
    if lat not in (None, "") and lon not in (None, ""):
        try:
            return float(lat), float(lon)
        except Exception:
            pass
    return base_home_latlon(hass)


def is_irish_home(hass) -> bool:
    from . import hazard_met_eireann
    return hazard_met_eireann.in_ireland(_config_attr(hass, "country"), _home_latlon(hass))


def effective_flag(hass, key: str) -> bool:
    """A source switch: the user's saved value always, else the region's."""
    saved = _saved(key)
    if saved is not None:
        return bool(saved)
    irish_default, other_default = _REGION_DEFAULTS[key]
    return irish_default if is_irish_home(hass) else other_default


def default_source(hass) -> str:
    """The warnings source for a home where nothing is chosen."""
    country = str(_config_attr(hass, "country", "") or "").upper()
    if country == "IE" or (not country and is_irish_home(hass)):
        return "met_eireann"
    return "us" if country == "US" else "custom"


def effective_source(hass) -> str:
    """The chosen warnings source: the saved one, else one read from older
    settings, else the region's. Nothing is written back on the way."""
    chosen = _saved("hazard_source")
    if chosen in HAZARD_SOURCES:
        return chosen
    if _saved("hazard_met_eireann_on") is True:
        return "met_eireann"
    cap_url = str(_saved("hazard_cap_url") or "").strip()
    if cap_url and _saved("hazard_cap_on") is True:
        return "custom"
    if _saved("hazard_weather_on") is True:
        return "us"
    return default_source(hass)


# ── geometry and memory ─────────────────────────────────────────────────────

def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    half_dphi = math.radians(lat2 - lat1) / 2
    half_dlam = math.radians(lon2 - lon1) / 2
    h = math.sin(half_dphi) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(half_dlam) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def _bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """A rough box of radius_km around a point, (min lat, max lat, min lon,
    max lon), to narrow a feed query; distance is checked exactly afterwards.
    The longitude span is capped near the poles."""
    lat_span = radius_km / _KM_PER_DEGREE
    lon_span = radius_km / (_KM_PER_DEGREE * max(0.01, math.cos(math.radians(lat))))
    return lat - lat_span, lat + lat_span, lon - lon_span, lon + lon_span


def _remember(feed: str, event_id: str) -> bool:
    """Mark an id as passed on; True only the first time."""
    seen = _SEEN.setdefault(feed, set())
    if event_id in seen:
        return False
    seen.add(event_id)
    if len(seen) > _SEEN_CAP:
        for _ in range(len(seen) - _SEEN_CAP // 2):
            seen.pop()
    return True


async def _get_json(hass, url: str, params: dict | None = None) -> Optional[dict]:
    """Fetch JSON with Nova's User-Agent. Any failure is None: no raise, no
    made up data."""
    try:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        request = async_get_clientsession(hass).get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": _USER_AGENT, "Accept": "application/geo+json"})
        async with request as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            _LOGGER.debug("hazard: %s gave HTTP %s", url, resp.status)
            return None
    except Exception as exc:
        _LOGGER.debug("hazard: fetching %s failed: %s", url, exc)
        return None


# ── the three feeds ─────────────────────────────────────────────────────────

def _f4(value: float) -> str:
    return f"{value:.4f}"


async def _check_earthquakes(hass, lat: float, lon: float) -> list[dict]:
    """New USGS quakes from the past hour within the radius."""
    radius = float(_cfg("hazard_quake_radius_km", _DEF_QUAKE_RADIUS_KM))
    min_mag = float(_cfg("hazard_quake_min_mag", _DEF_QUAKE_MIN_MAG))
    south, north, west, east = _bbox(lat, lon, radius)
    hour_ago = time.gmtime(time.time() - 3600)
    query = {"format": "geojson", "starttime": time.strftime("%Y-%m-%dT%H:%M:%S", hour_ago),
             "minlatitude": _f4(south), "maxlatitude": _f4(north),
             "minlongitude": _f4(west), "maxlongitude": _f4(east),
             "minmagnitude": f"{min_mag:.1f}", "orderby": "time"}
    data = await _get_json(hass, _USGS_FDSN, query)
    if not data or "features" not in data:
        return []
    found = []
    for feature in data.get("features", []):
        try:
            quake_id = feature.get("id")
            info = feature.get("properties", {}) or {}
            point = (feature.get("geometry", {}) or {}).get("coordinates", [])
            if not quake_id or len(point) < 2:
                continue
            km = _haversine_km(lat, lon, float(point[1]), float(point[0]))
            # The box is square, so the radius is checked here.
            if km > radius or not _remember("quake", quake_id):
                continue
            found.append({"id": quake_id, "mag": info.get("mag"),
                          "place": info.get("place") or "unknown location",
                          "dist_km": round(km), "url": info.get("url")})
        except Exception:
            continue
    return found


async def _check_weather(hass, lat: float, lon: float) -> list[dict]:
    """New NWS alerts at the home point of a notable severity."""
    wanted = _cfg("hazard_wx_severities", None) or _DEF_WX_SEVERITIES
    if not isinstance(wanted, (list, tuple)):
        wanted = _DEF_WX_SEVERITIES
    wanted = tuple(wanted)
    data = await _get_json(hass, _NWS_ACTIVE, {"point": f"{_f4(lat)},{_f4(lon)}"})
    if not data or "features" not in data:
        return []
    found = []
    for feature in data.get("features", []):
        try:
            alert_id = feature.get("id")
            info = feature.get("properties", {}) or {}
            severity = info.get("severity") or "Unknown"
            if severity not in wanted or not alert_id or not _remember("wx", alert_id):
                continue
            found.append({"id": alert_id, "event": info.get("event") or "Weather alert",
                          "severity": severity, "headline": info.get("headline") or "",
                          "area": info.get("areaDesc") or "",
                          "instruction": info.get("instruction") or ""})
        except Exception:
            continue
    return found


def _nearest_km(lat: float, lon: float, geometry) -> Optional[float]:
    """Distance to an event's nearest usable point; None when it has none."""
    best = None
    for shape in geometry or []:
        point = shape.get("coordinates")
        if not point or len(point) < 2:
            continue
        try:
            km = _haversine_km(lat, lon, float(point[1]), float(point[0]))
        except Exception:
            continue
        best = km if best is None else min(best, km)
    return best


async def _check_disasters(hass, lat: float, lon: float) -> list[dict]:
    """New open EONET events whose nearest point is within the radius."""
    radius = float(_cfg("hazard_disaster_radius_km", _DEF_DISASTER_RADIUS_KM))
    days = int(_cfg("hazard_disaster_days", _DEF_DISASTER_DAYS))
    south, north, west, east = _bbox(lat, lon, radius)
    # EONET wants the box as west, north, east, south.
    box = ",".join(_f4(v) for v in (west, north, east, south))
    data = await _get_json(hass, _EONET_EVENTS, {"status": "open", "days": str(days), "bbox": box})
    if not data or "events" not in data:
        return []
    found = []
    for event in data.get("events", []):
        try:
            event_id = event.get("id")
            if not event_id:
                continue
            km = _nearest_km(lat, lon, event.get("geometry", []))
            if km is None or km > radius or not _remember("disaster", event_id):
                continue
            kinds = ", ".join(c.get("title", "") for c in event.get("categories", []) if c)
            links = event.get("sources")
            found.append({"id": event_id, "title": event.get("title") or "Natural event",
                          "category": kinds or "event", "dist_km": round(km),
                          "url": links[0].get("url") if links else None})
        except Exception:
            continue
    return found


# ── wording ─────────────────────────────────────────────────────────────────

def _addressed(honorific: str) -> str:
    """", sir" style address; nothing when nobody is addressed."""
    return f", {honorific}" if honorific else ""


def _fmt_quake(q: dict, honorific: str) -> str:
    mag = q.get("mag")
    size = f"magnitude {mag:.1f}" if isinstance(mag, (int, float)) else "an earthquake"
    # Preserved quirk: with no magnitude this reads "A an earthquake earthquake".
    return (f"Seismic alert{_addressed(honorific)}. A {size} earthquake was just "
            f"recorded {q['dist_km']} km away — {q['place']}.")


def _fmt_weather(w: dict, honorific: str) -> str:
    where = f" for {w['area']}" if w.get("area") else ""
    text = f"{w['severity']} weather alert{_addressed(honorific)}: {w['event']}{where}."
    if w.get("instruction"):
        text = f"{text} {w['instruction'][:200]}"
    return text


def _fmt_disaster(d: dict, honorific: str) -> str:
    return (f"Natural hazard nearby{_addressed(honorific)}: {d['title']} "
            f"({d['category']}), about {d['dist_km']} km away.")


# ── delivery ────────────────────────────────────────────────────────────────

async def _deliver(hass, push_text: str, action_key: str, *, speak_text: str = "",
                   night: bool = False) -> None:
    """Hand one hazard to delivery: every phone always, and speak_text on the
    whole house broadcast when given. The caller has already applied the level
    rules; night says the night level let it speak in quiet hours. Shush, the
    Announcements switch, sleep and notifications only still apply in
    delivery. Never raises."""
    try:
        from . import audio_routing, delivery, tts_helper
        speakers = []
        if speak_text:
            speakers = audio_routing.broadcast_target(
                hass, broadcast_group=_cfg("broadcast_group", "") or None,
                announcement_speakers=_cfg("announcement_speakers", None))
        request = delivery.DeliveryRequest(
            source=_ACTION.get(action_key, action_key), message=push_text,
            spoken=speak_text, urgency="high", category="hazard",
            speakers=tuple(speakers or ()), phone_only=not speak_text,
            quiet_ok=night, sleep_ok=night, push=delivery.PUSH_ALWAYS,
            all_phones=True, push_first=True, tts_context="hazard",
            tts_entity=tts_helper.find_best_tts_entity(hass) or "", record_kind="hazard")
        await delivery.async_deliver(hass, request)
    except Exception as exc:
        _LOGGER.debug("hazard: delivery failed: %s", exc)


# ── weather warnings ────────────────────────────────────────────────────────

def _cap_settings() -> dict:
    return {"url": str(_saved("hazard_cap_url") or "").strip(),
            "codes": _json_list("hazard_cap_area_codes"),
            "names": _json_list("hazard_cap_area_names")}


async def _fetch_warnings(hass) -> dict:
    """{source: (complete, warnings)} for the chosen source; empty when it is
    the US source or has nothing to ask about."""
    from . import hazard_cap, hazard_met_eireann
    home = _home_latlon(hass)
    source = effective_source(hass)
    result: dict = {}
    if source == "met_eireann":
        counties = hazard_met_eireann.chosen_counties(_saved("hazard_counties"), home)
        if counties:
            complete, warnings, _how = await hazard_met_eireann.fetch(
                hass, counties, lang=_lang(hass), user_agent=_USER_AGENT)
            result[hazard_met_eireann.SOURCE] = (complete, warnings)
    cap = _cap_settings()
    if source == "custom" and cap["url"]:
        complete, alerts = await hazard_cap.collect(
            hass, cap["url"], lang=_lang(hass), user_agent=_USER_AGENT)
        matched = hazard_cap.to_warnings(alerts, home=home, area_codes=cap["codes"],
                                         area_names=cap["names"])
        result[hazard_cap.SOURCE] = (complete, matched)
    return result


def _active(warnings: list[dict], now) -> list[dict]:
    """The warnings in force: a known level, not a Cancel, not expired."""
    from . import hazard_warnings as hw

    def live(w):
        if w.get("msg_type") == "Cancel" or w.get("level") not in hw.LEVELS:
            return False
        return not hw._expired(w.get("expiry", ""), now)
    return [w for w in warnings if live(w)]


def _note_situations() -> None:
    """Tell the open situations list how many warnings are live."""
    try:
        from . import situations
        situations.note_weather(sum(len(found) for found in _LAST_WARNINGS.values()))
    except Exception:
        pass


async def _poll_warnings(hass, honorific: str, *, in_quiet: bool) -> dict:
    """Poll the warnings source once; returns {source: events}."""
    global _STORE
    from datetime import datetime, timezone
    from . import hazard_warnings as hw
    now = datetime.now(timezone.utc)
    fetched = await _fetch_warnings(hass)
    if not fetched:
        return {}
    if _STORE is None:
        _STORE = await hass.async_add_executor_job(hw.load_store, now)
    push_level = _level_setting("hazard_push_level", _DEF_PUSH_LEVEL)
    speak_level = _level_setting("hazard_speak_level", _DEF_SPEAK_LEVEL)
    night_level = _night_level_setting()
    tz = _tz(hass)
    counts: dict = {}
    dirty = False
    # Deliveries wait until the store is saved: a restart in between must not
    # tell the same warning twice.
    pending: list = []
    for source, (complete, warnings) in fetched.items():
        known = _STORE.get(source, {})
        updated, events = hw.reconcile(known, warnings, complete=complete, now=now,
                                       push_level=push_level)
        if updated != known:
            _STORE[source] = updated
            dirty = True
        if complete or warnings:
            _LAST_WARNINGS[source] = _active(warnings, now)
            _note_situations()
        for event in events:
            kind, warning = event[0], event[1]
            if kind == "cancelled":
                pending.append((hw.pushed(kind, warning, honorific, tz), "", False))
                continue
            was = event[2] if len(event) > 2 else ""
            # Spoken as well as pushed only at or above the speak level, and at
            # night only at the night level. Anything else (a warning lowered
            # after it was told, say) goes to the phone.
            say = hw.speak_allowed(warning["level"], in_quiet, speak_level, push_level,
                                   night_level)
            words = hw.spoken(kind, warning, honorific, tz, was) if say else ""
            pending.append((hw.pushed(kind, warning, honorific, tz, was), words,
                            say and in_quiet))
        counts[source] = len(events)
    if dirty:
        await hass.async_add_executor_job(hw.save_store, _STORE, now)
    for push_text, words, at_night in pending:
        await _deliver(hass, push_text, "warning", speak_text=words, night=at_night)
    return counts


# ── entry points ────────────────────────────────────────────────────────────

def _rounded(point, places: int = 3) -> list:
    return [round(point[0], places), round(point[1], places)]


async def periodic_check(hass, honorific: str = "sir") -> dict:
    """The timed poll: every enabled feed once, telling each new event.
    Returns a short summary and never raises."""
    if not _enabled():
        return {"skipped": "disabled"}
    in_quiet = _in_quiet_hours(hass)
    try:
        warnings = await _poll_warnings(hass, honorific, in_quiet=in_quiet)
    except Exception as exc:
        _LOGGER.debug("hazard: warnings poll failed: %s", type(exc).__name__)
        warnings = {}
    centre = _home_latlon(hass)
    if centre is None:
        _LOGGER.debug("hazard: no home coordinates, nothing to check around")
        return {"skipped": "no_location", "warnings": warnings}
    lat, lon = centre
    # These feeds share no severity scale that could justify speaking at
    # night, so in quiet hours they only reach the phones.
    feeds = (("quake", effective_flag(hass, "hazard_quakes_on"), _check_earthquakes, _fmt_quake),
             ("wx", effective_source(hass) == "us", _check_weather, _fmt_weather),
             ("disaster", effective_flag(hass, "hazard_disasters_on"), _check_disasters,
              _fmt_disaster))
    fired = {key: 0 for key, *_rest in feeds}
    for key, wanted, check, word in feeds:
        if not wanted:
            continue
        for item in await check(hass, lat, lon):
            text = word(item, honorific)
            await _deliver(hass, text, key, speak_text="" if in_quiet else text)
            fired[key] += 1
    if any(fired.values()):
        _LOGGER.info("hazard: %d new alert(s): %s", sum(fired.values()), fired)
    return {"checked": True, "fired": fired, "center": _rounded(centre), "warnings": warnings}


async def scan_now(hass, honorific: str = "sir") -> dict:
    """What is active near home now, for the agent tool and the panel. It
    reads only: nothing is announced and the seen ids are put back after, so
    asking never hides a later alert. Feeds that are off are not asked."""
    from datetime import datetime, timezone
    from . import hazard_warnings as hw
    centre = _home_latlon(hass)
    now = datetime.now(timezone.utc)
    warnings: list = []
    try:
        for _complete, found in (await _fetch_warnings(hass)).values():
            warnings.extend(hw.for_panel(w, _tz(hass)) for w in _active(found, now))
    except Exception as exc:
        _LOGGER.debug("hazard: warnings scan failed: %s", type(exc).__name__)
    if centre is None:
        return {"ok": False, "error": "no home location configured",
                "warnings": warnings, "counts": {"warnings": len(warnings)}}
    lat, lon = centre
    kept = {feed: set(ids) for feed, ids in _SEEN.items()}
    try:
        quakes = disasters = wx = []
        if effective_flag(hass, "hazard_quakes_on"):
            quakes = await _check_earthquakes(hass, lat, lon)
        if effective_source(hass) == "us":
            wx = await _check_weather(hass, lat, lon)
        if effective_flag(hass, "hazard_disasters_on"):
            disasters = await _check_disasters(hass, lat, lon)
    finally:
        _SEEN.clear()
        _SEEN.update(kept)
    counts = {"earthquakes": len(quakes), "weather": len(wx), "disasters": len(disasters),
              "warnings": len(warnings)}
    return {"ok": True, "center": _rounded(centre), "earthquakes": quakes, "weather": wx,
            "disasters": disasters, "warnings": warnings, "counts": counts}


def _county(code, names) -> dict:
    return {"code": code, "name": names(code)}


async def status(hass) -> dict:
    """The panel's view of the settings and what would be watched. Reads
    only."""
    from . import hazard_met_eireann as met, hazard_warnings as hw
    centre = _home_latlon(hass)
    home = base_home_latlon(hass)
    source = effective_source(hass)
    nearest = met.nearest_county(*centre) if centre else None
    chosen = met.chosen_counties(_saved("hazard_counties"), centre)
    table = sorted(met.COUNTIES.items(), key=lambda row: row[1][0])
    shown: list = []
    if source in ("met_eireann", "custom"):
        last = _LAST_WARNINGS.get(met.SOURCE if source == "met_eireann" else "cap", [])
        tz = _tz(hass)
        shown = [hw.for_panel(w, tz) for w in last]
    return {
        "enabled": _enabled(),
        "center": _rounded(centre) if centre else None,
        "using_override": bool(_cfg("hazard_lat", None) and _cfg("hazard_lon", None)),
        "quake_radius_km": float(_cfg("hazard_quake_radius_km", _DEF_QUAKE_RADIUS_KM)),
        "quake_min_mag": float(_cfg("hazard_quake_min_mag", _DEF_QUAKE_MIN_MAG)),
        "disaster_radius_km": float(_cfg("hazard_disaster_radius_km", _DEF_DISASTER_RADIUS_KM)),
        "feeds": {"earthquakes": effective_flag(hass, "hazard_quakes_on"),
                  "weather": source == "us",
                  "disasters": effective_flag(hass, "hazard_disasters_on")},
        "in_ireland": is_irish_home(hass),
        "source": source,
        "home_center": _rounded(home, 6) if home else None,
        "sources": {"met_eireann": source == "met_eireann", "cap": source == "custom",
                    "cap_configured": bool(_cap_settings()["url"])},
        "detected_county": _county(nearest, met.county_name) if nearest else None,
        "counties": [_county(code, met.county_name) for code in chosen],
        "county_table": [{"code": code, "name": row[0]} for code, row in table],
        "push_level": _level_setting("hazard_push_level", _DEF_PUSH_LEVEL),
        "speak_level": _level_setting("hazard_speak_level", _DEF_SPEAK_LEVEL),
        "night_speak_level": _night_level_setting(),
        "warnings": shown,
    }
