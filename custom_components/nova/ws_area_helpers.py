"""The area, satellite and camera helpers behind nova/get_panel_data.

Moved verbatim out of websocket.py. They read the area, entity and device
registries and the state machine to build the room cards and the setup lists
(satellites, cast speakers, cameras, onboarding). They import nothing from
Home Assistant at import time.

websocket.py imports these names, so they stay reachable through it.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from . import audio_routing

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _area_name(hass: HomeAssistant, area_id: str) -> str:
    """Friendly name for an area_id."""
    try:
        from homeassistant.helpers import area_registry as ar
        reg = ar.async_get(hass)
        area = reg.async_get_area(area_id)
        if area:
            return area.name or area_id
    except Exception:
        pass
    return area_id


def _entities_in_area(hass: HomeAssistant, area_id: str) -> list[str]:
    """All entity_ids whose (entity area) or (device area) matches.

    Skips user-excluded entities so the room card (light count, capabilities,
    last motion) doesn't show or count entities the user has excluded.
    """
    from homeassistant.helpers import entity_registry as er, device_registry as dr
    try:
        from .entity_filter import is_excluded
    except Exception:
        is_excluded = lambda _h, _e: False
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    out = []
    for ent in ent_reg.entities.values():
        ent_area = ent.area_id
        if not ent_area and ent.device_id:
            dev = dev_reg.async_get(ent.device_id)
            if dev:
                ent_area = dev.area_id
        if ent_area == area_id:
            if is_excluded(hass, ent.entity_id):
                continue
            out.append(ent.entity_id)
    return out


def _area_capabilities(hass: HomeAssistant, area_id: str) -> list[str]:
    """
    Return a sorted list of capability codes present in this area.
    Each code is what the panel will render as icon + label.
    """
    caps: set[str] = set()
    for eid in _entities_in_area(hass, area_id):
        domain = eid.split(".", 1)[0]
        state = hass.states.get(eid)
        dclass = state.attributes.get("device_class") if state else None

        if domain == "assist_satellite":
            caps.add("sat")
        elif domain == "media_player":
            caps.add("spkr")
        elif domain == "camera":
            caps.add("cam")
        elif domain == "binary_sensor":
            if dclass in ("occupancy", "motion", "presence"):
                caps.add("mmwave")
            elif dclass in ("door", "window", "garage_door", "opening"):
                caps.add("door")
            elif dclass in ("moisture",):
                caps.add("leak")
            elif dclass in ("smoke", "gas", "carbon_monoxide"):
                caps.add("alarm")
            elif dclass in ("safety", "tamper", "problem"):
                caps.add("alarm")
        elif domain == "light":
            caps.add("light")
        elif domain == "switch":
            caps.add("switch")
        elif domain == "lock":
            caps.add("lock")
        elif domain == "climate":
            caps.add("climate")

    # Ordering: sat, spkr, mmwave, cam, light, switch, lock, climate, door, leak, alarm
    order = ["sat", "spkr", "mmwave", "cam", "light", "switch", "lock", "climate", "door", "leak", "alarm"]
    return [c for c in order if c in caps]


def _is_outdoor_area(hass: HomeAssistant, area_id: str) -> bool:
    """Heuristic: does the area name look outdoor?"""
    name = (_area_name(hass, area_id) or "").lower()
    outdoor_keywords = (
        "yard", "garden", "driveway", "patio", "deck", "porch",
        "pool", "outdoor", "outside", "exterior", "lawn",
    )
    return any(kw in name for kw in outdoor_keywords)


def _dominant_area(hass: HomeAssistant) -> str | None:
    """
    Pick the 'most alive' area — currently occupied, with most-recent motion.
    Prefers indoor areas over outdoor ones (you don't live in the yard).
    Returns area_id or None.
    """
    occupied = audio_routing.currently_occupied_areas(hass)
    if not occupied:
        return None

    # Split into indoor vs outdoor
    indoor = [a for a in occupied if not _is_outdoor_area(hass, a)]
    outdoor = [a for a in occupied if _is_outdoor_area(hass, a)]
    # Strongly prefer indoor; only use outdoor if that's all we have
    candidates = indoor or outdoor

    # Rank by most recent occupancy sensor change
    best_area = None
    best_ts = 0.0
    for area_id in candidates:
        for eid in audio_routing.presence_entities_in_area(hass, area_id):
            state = hass.states.get(eid)
            if state is None:
                continue
            # last_changed is a datetime
            try:
                ts = state.last_changed.timestamp()
            except Exception:
                continue
            if ts > best_ts:
                best_ts = ts
                best_area = area_id

    return best_area or candidates[0]


def _area_light_state(hass: HomeAssistant, area_id: str) -> tuple[int, int]:
    """Count (lights_on, lights_total) for an area — cheap, light-domain only.
    Used to drive the per-room light indicator + toggle in the 3D house."""
    on = total = 0
    for eid in _entities_in_area(hass, area_id):
        if not eid.startswith("light."):
            continue
        st = hass.states.get(eid)
        if st is None:
            continue
        total += 1
        if st.state == "on":
            on += 1
    return on, total


def _area_temp_humidity_entities(hass: HomeAssistant, area_id: str) -> tuple[Optional[str], Optional[str]]:
    """The first temperature/humidity sensor entity_id found in an area, or
    None. Same resolution order _area_live_readings uses, factored out so
    the areas grid and the sparkline history fetch use one source of truth."""
    temp_eid = None
    humidity_eid = None
    for eid in _entities_in_area(hass, area_id):
        if temp_eid and humidity_eid:
            break
        state = hass.states.get(eid)
        if state is None or eid.split(".", 1)[0] != "sensor":
            continue
        dclass = state.attributes.get("device_class")
        if dclass == "temperature" and temp_eid is None:
            temp_eid = eid
        elif dclass == "humidity" and humidity_eid is None:
            humidity_eid = eid
    return temp_eid, humidity_eid


def _area_live_readings(hass: HomeAssistant, area_id: str) -> dict:
    """Pull temperature, humidity, any lights-on count in the area."""
    temp = None
    humidity = None
    lights_on = 0
    lights_total = 0
    last_motion_seconds = None

    for eid in _entities_in_area(hass, area_id):
        state = hass.states.get(eid)
        if state is None:
            continue
        domain = eid.split(".", 1)[0]
        dclass = state.attributes.get("device_class")

        if domain == "sensor":
            if dclass == "temperature" and temp is None:
                try:
                    val = float(state.state)
                    unit = state.attributes.get("unit_of_measurement", "")
                    temp = f"{int(round(val))}°{unit.replace('°', '')[:1] or 'F'}"
                except (ValueError, TypeError):
                    pass
            elif dclass == "humidity" and humidity is None:
                try:
                    humidity = f"{int(round(float(state.state)))}%"
                except (ValueError, TypeError):
                    pass
        elif domain == "light":
            lights_total += 1
            if state.state == "on":
                lights_on += 1
        elif domain == "binary_sensor" and dclass in ("occupancy", "motion", "presence"):
            try:
                age = (time.time() - state.last_changed.timestamp())
                if last_motion_seconds is None or age < last_motion_seconds:
                    last_motion_seconds = age
            except Exception:
                pass

    lights_display = None
    if lights_total > 0:
        lights_display = f"{lights_on}/{lights_total}" if lights_total > 1 else ("ON" if lights_on else "OFF")

    return {
        "temp": temp,
        "humidity": humidity,
        "lights": lights_display,
        "last_motion_seconds": last_motion_seconds,
    }


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


def _satellite_count(hass: HomeAssistant) -> tuple[int, int]:
    """Return (available, total) satellite count."""
    total = 0
    avail = 0
    for state in hass.states.async_all("assist_satellite"):
        total += 1
        if state.state not in ("unavailable", "unknown"):
            avail += 1
    return avail, total


def _get_satellites(hass: HomeAssistant) -> list[dict]:
    """Return list of satellites with entity_id, name, and area."""
    satellites = []
    try:
        from homeassistant.helpers import (
            entity_registry as er,
            device_registry as dr,
            area_registry as areg,
        )
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)
        area_reg = areg.async_get(hass)

        for state in hass.states.async_all("assist_satellite"):
            entry = ent_reg.async_get(state.entity_id)
            area_name = ""
            if entry and entry.device_id:
                device = dev_reg.async_get(entry.device_id)
                if device and device.area_id:
                    area = area_reg.async_get_area(device.area_id)
                    area_name = area.name if area else device.area_id
            name = state.attributes.get("friendly_name", state.entity_id)
            satellites.append({
                "entity_id": state.entity_id,
                "name": name,
                "area": area_name,
            })
    except Exception:
        for state in hass.states.async_all("assist_satellite"):
            satellites.append({
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name", state.entity_id),
                "area": "",
            })
    return satellites


def _get_camera_overrides() -> dict:
    """The camera_overrides runtime map (original → frame source), for the
    panel to mirror server-side source resolution (v6.47.0). Never raises."""
    try:
        from . import nova_config
        ov = nova_config.get("camera_overrides", {}) or {}
        return {str(k): str(v) for k, v in ov.items()} if isinstance(ov, dict) else {}
    except Exception:
        return {}


def _get_camera_names() -> dict:
    """The camera_names runtime map (entity_id → Nova-only display name),
    v6.48.0. Never raises."""
    try:
        from . import nova_config
        nm = nova_config.get("camera_names", {}) or {}
        return {str(k): str(v) for k, v in nm.items()} if isinstance(nm, dict) else {}
    except Exception:
        return {}


def _get_onboarding_state(hass: HomeAssistant, entry, current_notify: str) -> dict:
    """Compute the first-run onboarding checklist (v6.70.0). Reports which
    high-value setup steps are done so the panel can show a welcome card to new
    users and hide it once they're set up or dismiss it. Guidance, not config —
    the LLM key is already collected by the config flow before the panel loads;
    this covers the 'what now?' gap after install."""
    try:
        from . import nova_config
        dismissed = bool(nova_config.get("onboarding_dismissed", False))
    except Exception:
        dismissed = False
    has_notify = bool(current_notify)
    try:
        has_cameras = len(_get_cameras(hass)) > 0
    except Exception:
        has_cameras = False
    try:
        from . import nova_config
        banter_set = nova_config.get("banter_level", None) is not None
    except Exception:
        banter_set = False
    # Voice counts as set up only when Setup Doctor's own Assist pipeline
    # check passes (a Nova pipeline that really uses Nova's agent), not
    # merely when some satellite exists. That check is cheap and sync: no
    # LLM probe, so it is safe on the 20s panel poll.
    try:
        from . import setup_health
        has_voice = setup_health._check_assist_pipeline(hass).get("status") == "ok"
    except Exception:
        has_voice = False
    try:
        from . import nova_config
        fresh = bool(nova_config.get("welcome_pending", False))
    except Exception:
        fresh = False
    try:
        from . import nova_config
        briefings_on = (bool(nova_config.get("briefing_morning_enabled", False))
                        or bool(nova_config.get("briefing_evening_enabled", False)))
    except Exception:
        briefings_on = False
    steps = [
        {"id": "notify", "label": "Set an alert destination",
         "hint": "Where Nova sends security alerts and notifications (your phone).",
         "jump": "Notifications", "done": has_notify},
        {"id": "cameras", "label": "Connect cameras (optional)",
         "hint": "Nest/Frigate cameras enable doorbell analysis, package detection, the live floor plan.",
         "jump": "Cameras", "done": has_cameras},
        {"id": "voice", "label": "Set up voice (optional)",
         "hint": "On HA OS/Supervised Nova installs the voice stack for you. Done when "
                 "an Assist pipeline uses Nova as its conversation agent "
                 "(Settings \u2192 Voice assistants).",
         "done": has_voice},
        {"id": "banter", "label": "Pick a personality level",
         "hint": "Plain, dry, or full — how much of Nova's quiet wit comes through. Settings \u2192 Character.",
         "jump": "Nova Character", "done": banter_set},
        {"id": "briefings", "label": "Turn on daily briefings",
         "hint": "Morning and evening summaries of weather, calendar, overnight "
                 "events, and energy, in Settings under Briefings. Anticipation "
                 "and cross-session memory live under Anticipation and Memory.",
         "jump": "Briefings", "done": briefings_on},
    ]
    done_count = sum(1 for s in steps if s["done"])
    return {
        "dismissed": dismissed,
        "show": (not dismissed) and (not has_notify or done_count < 2),
        # Fresh install (setup screens): the panel also keeps the card up
        # while Setup Doctor reports problems, until dismissed.
        "fresh": fresh,
        "steps": steps,
        "done_count": done_count,
        "total": len(steps),
    }


def _get_cameras(hass: HomeAssistant) -> list[dict]:
    """Camera entities for the picker/chips. `name` honours the Nova-only
    camera_names map (v6.48.0); `raw_name` keeps the HA friendly name so the
    rename UI can show what blank reverts to."""
    cams = []
    names = _get_camera_names()
    try:
        from . import outdoor
        from .camera import display_name, _disabled_cameras
        indoor_list = outdoor._cfg_list("indoor_entities")
        outdoor_list = outdoor._cfg_list("outdoor_entities")
        disabled = _disabled_cameras()
        for state in hass.states.async_all("camera"):
            friendly = state.attributes.get("friendly_name", state.entity_id)
            cams.append({
                "entity_id": state.entity_id,
                "name": display_name(state.entity_id, friendly, names),
                "raw_name": friendly,
                # v6.49.0: location designation for the whole cognitive stack
                # (intrusion filter, notable-events, motion scan all consult
                # outdoor.is_outdoor).
                "enabled": state.entity_id not in disabled,
                "outdoor": outdoor.is_outdoor(hass, state.entity_id, friendly),
                "location_mode": outdoor.location_mode(
                    state.entity_id, indoor_list, outdoor_list),
            })
    except Exception:
        pass
    return sorted(cams, key=lambda c: c["name"])


def _get_cast_devices(hass: HomeAssistant) -> list[dict]:
    """Return list of Cast/Google media_player entities."""
    devices = []
    for state in hass.states.async_all("media_player"):
        # Include cast, Google, Sonos, Lenovo, and group players
        eid = state.entity_id
        name = state.attributes.get("friendly_name", eid)
        platform = state.attributes.get("platform", "")
        # Cast devices typically have these attributes
        is_cast = (
            "cast" in platform.lower()
            or "google" in name.lower()
            or "nest" in name.lower()
            or "lenovo" in name.lower()
            or "sonos" in name.lower()
            or "home_group" in eid
            or "group" in eid
            or state.attributes.get("supported_features", 0) & 16384  # PLAY_MEDIA
        )
        if is_cast and state.state not in ("unavailable",):
            devices.append({
                "entity_id": eid,
                "name": name,
            })
    return devices


def _all_areas_with_anything(hass: HomeAssistant) -> list[str]:
    """Areas that have at least one satellite, speaker, or presence sensor."""
    try:
        from homeassistant.helpers import area_registry as ar
        reg = ar.async_get(hass)
        all_ids = [a.id for a in reg.async_list_areas()]
    except Exception:
        return []

    interesting = []
    for aid in all_ids:
        if _area_capabilities(hass, aid):
            interesting.append(aid)
    return interesting


def _get_all_people(hass: HomeAssistant) -> list[dict]:
    """Every known person.* entity, home or not — for the Person Honorifics
    card to list every household member, not just whoever's home right now
    when the panel happens to load (v7.99.0)."""
    try:
        from . import honorific
        return honorific.all_people(hass)
    except Exception:
        return []


def _get_speaker_assignable_areas(hass: HomeAssistant) -> list[dict]:
    """Areas the Room Speakers card can assign a speaker to (v7.92.0) —
    every area with a satellite, a speaker, or a presence sensor, same set
    as _all_areas_with_anything, with display names attached."""
    try:
        from homeassistant.helpers import area_registry as ar
        reg = ar.async_get(hass)
        out = []
        for area_id in _all_areas_with_anything(hass):
            area = reg.async_get_area(area_id)
            out.append({"area_id": area_id, "name": area.name if area else area_id})
        return out
    except Exception:
        return []
