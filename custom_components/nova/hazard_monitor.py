"""Real-time multi-hazard monitor (v6.71.0; warnings sources 8.8.0).

Weather warnings (8.8.0; single source 8.8.2): Met Éireann's warnings for the
chosen counties (hazard_met_eireann.py), the US National Weather Service, or a
custom CAP feed (hazard_cap.py). The CAP sources share one lifecycle
(hazard_warnings.py): a warning is
announced once, again when its level changes, and a phone-only notice
follows when it is cancelled. hazard_push_level (default yellow) is the
lowest level sent to the phone; hazard_speak_level (default orange) and
above is also spoken. What was announced survives a restart.

Region defaults (8.8.0): for an Irish home (Home Assistant's country IE, or
the home point on the island with no country set), the Met Éireann source
is on and the three legacy feeds below are off, unless the user has saved
a value. Everywhere else the defaults are as before.

The legacy feeds poll three free, no-key government/agency feeds on an
interval and speak/push only *new*, *nearby*, *significant* events through
Nova's existing alert path:

  - USGS earthquakes  — FDSN event query, bounding-box + min-magnitude scoped
  - NWS weather alerts — api.weather.gov active alerts for the home point
  - NASA EONET        — Earth Observatory natural events (wildfires, volcanoes,
                        severe storms), bbox-scoped, status=open

Location comes from the home coordinates HA already knows (zone.home /
hass.config), or a lat/long override the user sets in the panel. None of these
APIs take a ZIP directly, so a ZIP is converted to lat/long once (via a free
geocode) and stored as the override.

Design deliberately mirrors the rest of Nova and the lessons this codebase has
learned the hard way:
  - Each feed dedups on stable event IDs (a per-feed "seen" set) so a standing
    event never re-alerts — the same discipline package_monitor uses.
  - A feed that errors is logged and skipped; it never fabricates an alert. A
    transient fetch failure is not a hazard. (Same "don't cry wolf" principle as
    the service-health work.)
  - NWS *requires* a descriptive User-Agent or it rejects the request; we send
    one. A missing UA is a silent-failure trap, so it's not optional here.
  - Severity/magnitude thresholds keep it to genuinely notable events, not every
    micro-quake or minor advisory.

Nothing here runs unless the user turns the monitor on (hazard_monitor_enabled).
"""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

_LOGGER = logging.getLogger(__name__)

# ── endpoints (verified real, not the placeholder hosts some guides show) ────
_USGS_FDSN = "https://earthquake.usgs.gov/fdsnws/event/1/query"
_NWS_ACTIVE = "https://api.weather.gov/alerts/active"
_EONET_EVENTS = "https://eonet.gsfc.nasa.gov/api/v3/events"

# A descriptive User-Agent. NWS mandates one; EONET/USGS appreciate one. Generic
# on purpose — no personal data (contact is the project, not the user).
_USER_AGENT = "Nova-AIO Home Assistant hazard monitor (github.com/abz2much/nova)"

# ── defaults (all overridable via config) ────────────────────────────────────
_DEF_QUAKE_RADIUS_KM = 300.0     # earthquakes within this of home
_DEF_QUAKE_MIN_MAG = 2.5         # ignore micro-quakes
_DEF_DISASTER_RADIUS_KM = 300.0  # EONET events within this of home
_DEF_DISASTER_DAYS = 3           # EONET look-back window
# NWS severities we alert on (skip Minor/Unknown advisories by default)
_DEF_WX_SEVERITIES = ("Extreme", "Severe")

# Per-feed dedup sets + a cap so they don't grow without bound across a long
# uptime. Module-level so they persist across polls within a HA run.
_SEEN: dict = {"quake": set(), "wx": set(), "disaster": set()}
_SEEN_CAP = 500

# Titles for the push (reuses _notify_all_devices' action_type → title map where
# possible; these are new types so we pass a readable title via the message).
_ACTION = {
    "quake": "hazard_earthquake",
    "wx": "hazard_weather",
    "disaster": "hazard_disaster",
    "warning": "hazard_weather",
}

# Region-aware defaults (8.8.0) for settings the user has not saved:
# (Irish home, anywhere else).
_REGION_DEFAULTS = {
    "hazard_met_eireann_on": (True, False),
    "hazard_quakes_on": (False, True),
    "hazard_weather_on": (False, True),
    "hazard_disasters_on": (False, True),
}
_DEF_PUSH_LEVEL = "yellow"
_DEF_SPEAK_LEVEL = "orange"
_DEF_NIGHT_SPEAK_LEVEL = "red"
HAZARD_SOURCES = ("met_eireann", "us", "custom")

# Warnings memory ({source: {id: entry}}), loaded from disk once per run,
# and the warnings each source last reported after a successful poll (for
# the panel). See hazard_warnings.py.
_STORE: Optional[dict] = None
_LAST_WARNINGS: dict = {}


# ── config access ────────────────────────────────────────────────────────────

def _cfg(key: str, default=None):
    try:
        from . import nova_config
        v = nova_config.get(key, default)
        return v if v is not None else default
    except Exception:
        return default


def _enabled() -> bool:
    return bool(_cfg("hazard_monitor_enabled", False))


def _saved(key: str):
    """The user's saved value, or None when nothing is saved."""
    try:
        from . import nova_config
        return nova_config.get(key, None)
    except Exception:
        return None


def is_irish_home(hass) -> bool:
    from . import hazard_met_eireann
    try:
        country = getattr(hass.config, "country", None)
    except Exception:
        country = None
    return hazard_met_eireann.in_ireland(country, _home_latlon(hass))


def effective_flag(hass, key: str) -> bool:
    """A source on/off setting: the saved value, else the region default.
    A saved value is never overridden."""
    saved = _saved(key)
    if saved is not None:
        return bool(saved)
    irish, other = _REGION_DEFAULTS[key]
    return irish if is_irish_home(hass) else other


def default_source(hass) -> str:
    """The weather-warning source for a home with no saved choice."""
    try:
        country = str(getattr(hass.config, "country", "") or "").upper()
    except Exception:
        country = ""
    if country == "IE":
        return "met_eireann"
    if country == "US":
        return "us"
    if not country and is_irish_home(hass):
        return "met_eireann"
    return "custom"


def effective_source(hass) -> str:
    """Saved source, old-setting derivation, or the geographic default.

    The derived choice is deliberately read-only: upgrading never writes a new
    setting merely because old source flags exist.
    """
    saved = _saved("hazard_source")
    if saved in HAZARD_SOURCES:
        return saved
    if _saved("hazard_met_eireann_on") is True:
        return "met_eireann"
    if (_saved("hazard_cap_on") is True
            and str(_saved("hazard_cap_url") or "").strip()):
        return "custom"
    if _saved("hazard_weather_on") is True:
        return "us"
    return default_source(hass)


def _level_setting(key: str, default: str) -> str:
    value = str(_saved(key) or "").strip().lower()
    return value if value in ("yellow", "orange", "red") else default


def _night_level_setting() -> str:
    value = str(_saved("hazard_night_speak_level") or "").strip().lower()
    return value if value in ("yellow", "orange", "red", "off") else _DEF_NIGHT_SPEAK_LEVEL


def _runtime(hass, key: str, default):
    """Live panel config, matching package_monitor's event-loop read."""
    from .runtime import domain_runtime_config
    rc = domain_runtime_config(hass)
    return rc[key] if key in rc else default


def _in_quiet_hours(hass) -> bool:
    """The Observer quiet window; a read or parse failure is safely not quiet."""
    try:
        from . import sleep_detection
        return sleep_detection._in_quiet_hours(
            str(_runtime(hass, "observer_quiet_start", "22:00")),
            str(_runtime(hass, "observer_quiet_end", "07:00")),
        )
    except Exception:
        return False


def _json_list(key: str) -> list:
    import json
    value = _saved(key)
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except ValueError:
            return []
    return [str(v) for v in value] if isinstance(value, list) else []


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


def base_home_latlon(hass) -> Optional[tuple[float, float]]:
    """Home Assistant's home coordinates, without Nova's override."""
    try:
        z = hass.states.get("zone.home")
        if z is not None:
            la = z.attributes.get("latitude")
            lo = z.attributes.get("longitude")
            if la is not None and lo is not None:
                return float(la), float(lo)
    except Exception:
        pass
    try:
        return float(hass.config.latitude), float(hass.config.longitude)
    except Exception:
        return None


def _home_latlon(hass) -> Optional[tuple[float, float]]:
    """Resolve the monitoring center: an explicit override if set, else home."""
    ov_lat = _cfg("hazard_lat", None)
    ov_lon = _cfg("hazard_lon", None)
    try:
        if ov_lat not in (None, "") and ov_lon not in (None, ""):
            return float(ov_lat), float(ov_lon)
    except Exception:
        pass
    return base_home_latlon(hass)


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """A lat/long bounding box roughly `radius_km` around a point. Coarse (used
    to pre-filter the API query); exact distance is checked with haversine after."""
    dlat = radius_km / 111.0
    # guard against cos(lat)=0 near the poles
    coslat = max(0.01, math.cos(math.radians(lat)))
    dlon = radius_km / (111.0 * coslat)
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


def _remember(feed: str, event_id: str) -> bool:
    """Record an event id as seen. Returns True if it was NEW (not seen before)."""
    s = _SEEN.setdefault(feed, set())
    if event_id in s:
        return False
    s.add(event_id)
    if len(s) > _SEEN_CAP:
        # drop the oldest-ish half (sets are unordered; this just bounds memory)
        for _ in range(len(s) - _SEEN_CAP // 2):
            s.pop()
    return True


async def _get_json(hass, url: str, params: dict | None = None) -> Optional[dict]:
    """GET JSON with the required User-Agent. Returns parsed dict or None on any
    failure (never raises, never fabricates)."""
    try:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(hass)
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/geo+json"}
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug("hazard: %s → HTTP %s", url, resp.status)
                return None
            return await resp.json(content_type=None)
    except Exception as exc:
        _LOGGER.debug("hazard: fetch failed %s: %s", url, exc)
        return None


# ── individual feeds ─────────────────────────────────────────────────────────

async def _check_earthquakes(hass, lat: float, lon: float) -> list[dict]:
    """USGS FDSN, bounding-box + min-mag scoped. Returns NEW nearby quakes."""
    radius = float(_cfg("hazard_quake_radius_km", _DEF_QUAKE_RADIUS_KM))
    min_mag = float(_cfg("hazard_quake_min_mag", _DEF_QUAKE_MIN_MAG))
    min_lat, max_lat, min_lon, max_lon = _bbox(lat, lon, radius)
    params = {
        "format": "geojson",
        "starttime": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(time.time() - 3600)),  # last hour
        "minlatitude": f"{min_lat:.4f}", "maxlatitude": f"{max_lat:.4f}",
        "minlongitude": f"{min_lon:.4f}", "maxlongitude": f"{max_lon:.4f}",
        "minmagnitude": f"{min_mag:.1f}", "orderby": "time",
    }
    data = await _get_json(hass, _USGS_FDSN, params)
    if not data or "features" not in data:
        return []
    out = []
    for feat in data.get("features", []):
        try:
            eid = feat.get("id")
            props = feat.get("properties", {}) or {}
            coords = (feat.get("geometry", {}) or {}).get("coordinates", [])
            if not eid or len(coords) < 2:
                continue
            qlon, qlat = float(coords[0]), float(coords[1])
            dist = _haversine_km(lat, lon, qlat, qlon)
            if dist > radius:                     # exact radius (bbox is square)
                continue
            if not _remember("quake", eid):       # already alerted
                continue
            out.append({
                "id": eid,
                "mag": props.get("mag"),
                "place": props.get("place") or "unknown location",
                "dist_km": round(dist),
                "url": props.get("url"),
            })
        except Exception:
            continue
    return out


async def _check_weather(hass, lat: float, lon: float) -> list[dict]:
    """NWS active alerts for the home point, filtered to notable severities."""
    sevs = _cfg("hazard_wx_severities", None) or _DEF_WX_SEVERITIES
    sevs = tuple(sevs) if isinstance(sevs, (list, tuple)) else _DEF_WX_SEVERITIES
    params = {"point": f"{lat:.4f},{lon:.4f}"}
    data = await _get_json(hass, _NWS_ACTIVE, params)
    if not data or "features" not in data:
        return []
    out = []
    for feat in data.get("features", []):
        try:
            eid = feat.get("id")
            props = feat.get("properties", {}) or {}
            severity = props.get("severity") or "Unknown"
            if severity not in sevs:
                continue
            if not eid or not _remember("wx", eid):
                continue
            out.append({
                "id": eid,
                "event": props.get("event") or "Weather alert",
                "severity": severity,
                "headline": props.get("headline") or "",
                "area": props.get("areaDesc") or "",
                "instruction": props.get("instruction") or "",
            })
        except Exception:
            continue
    return out


async def _check_disasters(hass, lat: float, lon: float) -> list[dict]:
    """NASA EONET open natural events, bbox-scoped + exact-radius filtered."""
    radius = float(_cfg("hazard_disaster_radius_km", _DEF_DISASTER_RADIUS_KM))
    days = int(_cfg("hazard_disaster_days", _DEF_DISASTER_DAYS))
    min_lat, max_lat, min_lon, max_lon = _bbox(lat, lon, radius)
    # EONET bbox order is minlon,maxlat,maxlon,minlat (WWSE)
    params = {
        "status": "open", "days": str(days),
        "bbox": f"{min_lon:.4f},{max_lat:.4f},{max_lon:.4f},{min_lat:.4f}",
    }
    data = await _get_json(hass, _EONET_EVENTS, params)
    if not data or "events" not in data:
        return []
    out = []
    for ev in data.get("events", []):
        try:
            eid = ev.get("id")
            if not eid:
                continue
            # nearest geometry point to home
            best = None
            for g in ev.get("geometry", []) or []:
                c = g.get("coordinates")
                if not c or len(c) < 2:
                    continue
                try:
                    elon, elat = float(c[0]), float(c[1])
                except Exception:
                    continue
                d = _haversine_km(lat, lon, elat, elon)
                if best is None or d < best:
                    best = d
            if best is None or best > radius:
                continue
            if not _remember("disaster", eid):
                continue
            cats = ", ".join(c.get("title", "") for c in ev.get("categories", []) if c)
            out.append({
                "id": eid,
                "title": ev.get("title") or "Natural event",
                "category": cats or "event",
                "dist_km": round(best),
                "url": (ev.get("sources") or [{}])[0].get("url") if ev.get("sources") else None,
            })
        except Exception:
            continue
    return out


# ── message formatting ───────────────────────────────────────────────────────

def _fmt_quake(q: dict, honorific: str) -> str:
    # honorific may be "" once nobody specific is home to address (see
    # honorific.py) — addr collapses the trailing ", {honorific}" to
    # nothing rather than a dangling comma.
    addr = f", {honorific}" if honorific else ""
    mag = q.get("mag")
    magtxt = f"magnitude {mag:.1f}" if isinstance(mag, (int, float)) else "an earthquake"
    return (f"Seismic alert{addr}. A {magtxt} earthquake was just "
            f"recorded {q['dist_km']} km away — {q['place']}.")


def _fmt_weather(w: dict, honorific: str) -> str:
    addr = f", {honorific}" if honorific else ""
    base = f"{w['severity']} weather alert{addr}: {w['event']}"
    if w.get("area"):
        base += f" for {w['area']}"
    base += "."
    if w.get("instruction"):
        base += f" {w['instruction'][:200]}"
    return base


def _fmt_disaster(d: dict, honorific: str) -> str:
    addr = f", {honorific}" if honorific else ""
    return (f"Natural hazard nearby{addr}: {d['title']} ({d['category']}), "
            f"about {d['dist_km']} km away.")


# ── delivery ─────────────────────────────────────────────────────────────────

async def _deliver(hass, push_text: str, action_key: str, *, speak_text: str = "") -> None:
    """Push to the phones, and speak ``speak_text`` when given, through the
    same paths every other Nova alert uses. Whether it is spoken is the
    caller's level rule; alert_path.for_hazard turns it into the plan
    (8.22.0). Never raises."""
    from . import alert_path
    plan = alert_path.for_hazard(alert_path.situation(hass), speak_allowed=bool(speak_text))

    async def _push():
        try:
            from . import cognitive_core as cc
            config = getattr(cc, "_CORE", None)
            cfg_obj = getattr(config, "config", None) if config else None
            await cc._notify_all_devices(hass, cfg_obj, push_text, _ACTION[action_key])
        except Exception as exc:
            _LOGGER.debug("hazard: notify failed: %s", exc)

    async def _speak(_targets):
        # a whole-house broadcast (earthquakes/severe weather are relevant
        # everywhere), same resolution sentinel.py uses for its own alerts
        try:
            from . import tts_helper, audio_routing
            tts = tts_helper.find_best_tts_entity(hass)
            spk = audio_routing.broadcast_target(
                hass,
                broadcast_group=_cfg("broadcast_group", "") or None,
                announcement_speakers=_cfg("announcement_speakers", None),
            )
            if tts and spk:
                await tts_helper.async_announce(hass, speak_text, tts, spk, context="hazard")
        except Exception:
            pass

    try:
        await alert_path.deliver(plan, speak=_speak, push=_push)
    except Exception as exc:
        _LOGGER.debug("hazard: delivery failed: %s", exc)


# ── weather warnings (8.8.0) ─────────────────────────────────────────────────

def _cap_settings() -> dict:
    return {"url": str(_saved("hazard_cap_url") or "").strip(),
            "codes": _json_list("hazard_cap_area_codes"),
            "names": _json_list("hazard_cap_area_names")}


async def _fetch_warnings(hass) -> dict:
    """The chosen CAP warning source, if any; failures are incomplete lists."""
    from . import hazard_cap, hazard_met_eireann
    home = _home_latlon(hass)
    out: dict = {}
    source = effective_source(hass)
    if source == "met_eireann":
        counties = hazard_met_eireann.chosen_counties(_saved("hazard_counties"), home)
        if counties:
            complete, warnings, _via = await hazard_met_eireann.fetch(
                hass, counties, lang=_lang(hass), user_agent=_USER_AGENT)
            out[hazard_met_eireann.SOURCE] = (complete, warnings)
    cap = _cap_settings()
    if source == "custom" and cap["url"]:
        complete, alerts = await hazard_cap.collect(
            hass, cap["url"], lang=_lang(hass), user_agent=_USER_AGENT)
        out[hazard_cap.SOURCE] = (complete, hazard_cap.to_warnings(
            alerts, home=home, area_codes=cap["codes"], area_names=cap["names"]))
    return out


def _active(warnings: list[dict], now) -> list[dict]:
    """Warnings that are warnings now: not a Cancel, not expired."""
    from . import hazard_warnings as hw
    return [w for w in warnings if w.get("msg_type") != "Cancel"
            and w.get("level") in hw.LEVELS and not hw._expired(w.get("expiry", ""), now)]


async def _poll_warnings(hass, honorific: str, *, in_quiet: bool) -> dict:
    """One poll of the warnings sources: reconcile, deliver, remember."""
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
    changed = False
    for source, (complete, warnings) in fetched.items():
        before = _STORE.get(source, {})
        after, events = hw.reconcile(before, warnings, complete=complete, now=now,
                                     push_level=push_level)
        if after != before:
            _STORE[source] = after
            changed = True
        if complete or warnings:
            _LAST_WARNINGS[source] = _active(warnings, now)
        for event in events:
            kind = event[0]
            if kind == "cancelled":
                # Phone only, whatever the level.
                await _deliver(hass, hw.pushed("cancelled", event[1], honorific, tz), "warning")
                continue
            w, old = event[1], (event[2] if len(event) > 2 else "")
            # At or above the speak level (and the push level): spoken and
            # pushed. Below it, including a warning lowered under the push
            # level after it was announced: phone only.
            speak = hw.speak_allowed(
                w["level"], in_quiet, speak_level, push_level, night_level)
            await _deliver(hass, hw.pushed(kind, w, honorific, tz, old), "warning",
                           speak_text=hw.spoken(kind, w, honorific, tz, old) if speak else "")
        counts[source] = len(events)
    if changed:
        await hass.async_add_executor_job(hw.save_store, _STORE, now)
    return counts


# ── the periodic entry point (called on an interval from __init__) ───────────

async def periodic_check(hass, honorific: str = "sir") -> dict:
    """Poll all enabled feeds once; push/speak any NEW nearby significant events.
    Returns a small summary dict. Never raises."""
    if not _enabled():
        return {"skipped": "disabled"}

    in_quiet = _in_quiet_hours(hass)
    try:
        warnings = await _poll_warnings(hass, honorific, in_quiet=in_quiet)
    except Exception as exc:
        _LOGGER.debug("hazard: warnings poll failed: %s", type(exc).__name__)
        warnings = {}

    center = _home_latlon(hass)
    if center is None:
        _LOGGER.debug("hazard: no home coordinates available; skipping")
        return {"skipped": "no_location", "warnings": warnings}
    lat, lon = center

    fired = {"quake": 0, "wx": 0, "disaster": 0}

    async def _announce(action_key: str, message: str) -> None:
        # deliver via the same paths every other Nova alert uses: pushed
        # to the phones, and spoken only outside quiet hours.  These legacy
        # feeds have no shared severity scale for a safe night exception.
        await _deliver(hass, message, action_key,
                       speak_text="" if in_quiet else message)

    # Earthquakes
    if effective_flag(hass, "hazard_quakes_on"):
        for q in await _check_earthquakes(hass, lat, lon):
            await _announce("quake", _fmt_quake(q, honorific))
            fired["quake"] += 1

    # Weather
    if effective_source(hass) == "us":
        for w in await _check_weather(hass, lat, lon):
            await _announce("wx", _fmt_weather(w, honorific))
            fired["wx"] += 1

    # Disasters
    if effective_flag(hass, "hazard_disasters_on"):
        for d in await _check_disasters(hass, lat, lon):
            await _announce("disaster", _fmt_disaster(d, honorific))
            fired["disaster"] += 1

    total = sum(fired.values())
    if total:
        _LOGGER.info("hazard: %d new alert(s) — %s", total, fired)
    return {"checked": True, "fired": fired, "center": [round(lat, 3), round(lon, 3)],
            "warnings": warnings}


async def scan_now(hass, honorific: str = "sir") -> dict:
    """On-demand read-only scan for the agent tool / panel test. Queries all
    feeds and returns what's currently active near home WITHOUT touching the
    dedup sets or announcing — so asking 'any hazards?' never suppresses the
    background monitor's future alerts, and never double-speaks. Never raises."""
    from datetime import datetime, timezone
    from . import hazard_warnings as hw
    center = _home_latlon(hass)
    # Weather warnings (8.8.0): read-only, never reconciled or announced.
    now = datetime.now(timezone.utc)
    warnings: list = []
    try:
        for _source, (_complete, found) in (await _fetch_warnings(hass)).items():
            warnings.extend(hw.for_panel(w, _tz(hass)) for w in _active(found, now))
    except Exception as exc:
        _LOGGER.debug("hazard: warnings scan failed: %s", type(exc).__name__)
    if center is None:
        return {"ok": False, "error": "no home location configured",
                "warnings": warnings, "counts": {"warnings": len(warnings)}}
    lat, lon = center

    # Temporarily bypass dedup by scanning into a scratch: we re-run the feed
    # logic but read everything (not just new). Simplest: snapshot & restore the
    # seen sets around a scan so nothing is marked consumed. A legacy feed
    # that is off is not queried (8.8.0; it used to be scanned regardless).
    saved = {k: set(v) for k, v in _SEEN.items()}
    try:
        quakes = (await _check_earthquakes(hass, lat, lon)
                  if effective_flag(hass, "hazard_quakes_on") else [])
        wx = (await _check_weather(hass, lat, lon)
              if effective_source(hass) == "us" else [])
        disasters = (await _check_disasters(hass, lat, lon)
                     if effective_flag(hass, "hazard_disasters_on") else [])
    finally:
        _SEEN.clear()
        _SEEN.update(saved)

    return {
        "ok": True,
        "center": [round(lat, 3), round(lon, 3)],
        "earthquakes": quakes,
        "weather": wx,
        "disasters": disasters,
        "warnings": warnings,
        "counts": {"earthquakes": len(quakes), "weather": len(wx),
                   "disasters": len(disasters), "warnings": len(warnings)},
    }


async def status(hass) -> dict:
    """Panel status: config + what the monitor would watch, without alerting.
    Runs a *read-only* count so the user can confirm it's wired to their area."""
    from . import hazard_met_eireann, hazard_warnings as hw
    center = _home_latlon(hass)
    home = base_home_latlon(hass)
    source = effective_source(hass)
    nearest = hazard_met_eireann.nearest_county(*center) if center else None
    chosen = hazard_met_eireann.chosen_counties(_saved("hazard_counties"), center)
    tz = _tz(hass)
    cap = _cap_settings()
    out = {
        "enabled": _enabled(),
        "center": [round(center[0], 3), round(center[1], 3)] if center else None,
        "using_override": bool(_cfg("hazard_lat", None) and _cfg("hazard_lon", None)),
        "quake_radius_km": float(_cfg("hazard_quake_radius_km", _DEF_QUAKE_RADIUS_KM)),
        "quake_min_mag": float(_cfg("hazard_quake_min_mag", _DEF_QUAKE_MIN_MAG)),
        "disaster_radius_km": float(_cfg("hazard_disaster_radius_km", _DEF_DISASTER_RADIUS_KM)),
        "feeds": {
            "earthquakes": effective_flag(hass, "hazard_quakes_on"),
            "weather": source == "us",
            "disasters": effective_flag(hass, "hazard_disasters_on"),
        },
        # 8.8.0: weather warnings.
        "in_ireland": is_irish_home(hass),
        "source": source,
        "home_center": [round(home[0], 6), round(home[1], 6)] if home else None,
        "sources": {
            "met_eireann": source == "met_eireann",
            "cap": source == "custom",
            "cap_configured": bool(cap["url"]),
        },
        "detected_county": ({"code": nearest, "name": hazard_met_eireann.county_name(nearest)}
                            if nearest else None),
        "counties": [{"code": c, "name": hazard_met_eireann.county_name(c)} for c in chosen],
        "county_table": [{"code": c, "name": n}
                         for c, (n, _la, _lo) in sorted(hazard_met_eireann.COUNTIES.items(),
                                                       key=lambda kv: kv[1][0])],
        "push_level": _level_setting("hazard_push_level", _DEF_PUSH_LEVEL),
        "speak_level": _level_setting("hazard_speak_level", _DEF_SPEAK_LEVEL),
        "night_speak_level": _night_level_setting(),
        "warnings": [hw.for_panel(w, tz)
                     for w in _LAST_WARNINGS.get(
                         hazard_met_eireann.SOURCE if source == "met_eireann" else "cap", [])]
                    if source in ("met_eireann", "custom") else [],
    }
    return out
