"""
Nova Panel WebSocket API (v5.4.2).

Registers the `nova/get_panel_data` WebSocket command that the custom
panel calls (on mount + every 5s) to refresh live state.

The single command returns everything the panel needs in one round-trip:
status flags, area registry with capabilities and occupancy, dominant
room, satellite count, bedroom count, uptime.

Activity log is a separate endpoint (deferred to session 3, needs DB work).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import audio_routing, sleep_detection
from .safe_errors import safe_error_message
# The debug log lives in ws_log.py (it imports no Home Assistant code). Other
# modules and tests reach these names through this module, so they stay
# importable from here. _LOG_FILE is deliberately not re-exported: it is
# reassigned in tests, and a patch on a copy of it here would do nothing.
from .ws_log import (  # noqa: F401
    _DEBUG_LOG,
    _LOG_QUEUE,
    _ensure_writer,
    nova_log,
    recent_conversation_log,
    recent_debug_log,
)
# The panel helpers live in ws_area_helpers.py and ws_panel_stats.py. The
# handlers here, and tests that patch these names on this module, look them
# up in this module's namespace, so they are imported by name.
from .ws_area_helpers import (
    _all_areas_with_anything,
    _area_capabilities,
    _area_light_state,
    _area_live_readings,
    _area_name,
    _area_temp_humidity_entities,
    _dominant_area,
    _format_duration,
    _get_all_people,
    _get_camera_names,
    _get_camera_overrides,
    _get_cameras,
    _get_cast_devices,
    _get_onboarding_state,
    _get_satellites,
    _get_speaker_assignable_areas,
    _is_outdoor_area,
    _satellite_count,
)
from .ws_panel_stats import (
    _entity_names,
    _format_uptime,
    _get_alarm_panels,
    _get_announcements_today,
    _get_appliance_status,
    _get_area_sparklines,
    _get_doorbell_training,
    _get_filtered_suggestions,
    _get_goals,
    _get_intrusion_status,
    _get_knowledge_stats,
    _get_lockdown_status,
    _get_observer_stats,
    _get_person_routines,
    _get_sentinel_rules,
    _get_suggestions,
    _named_decision,
)
# The AI settings commands live in ws_ai.py. async_register registers the
# handlers by name; invalidate_model_cache is public and also used below.
from .ws_ai import (
    invalidate_model_cache,
    ws_apply_ai_config,
    ws_delete_credential,
    ws_get_credential_status,
    ws_list_models,
    ws_set_credential,
    ws_test_provider_endpoint,
)
from .const import (
    CONF_BEDROOM_AREAS,
    CONF_GROUND_FLOOR_AREAS,
    CONF_BROADCAST_GROUP,
    CONF_GEMINI_API_KEY,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_SERVICES,
    CONF_OBSERVER_ENABLED,
    CONF_OBSERVER_QUIET_END,
    CONF_OBSERVER_QUIET_START,
    DEFAULT_OBSERVER_QUIET_END,
    DEFAULT_OBSERVER_QUIET_START,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Startup wall-clock — for uptime computation
_STARTUP_TS: float = time.time()


# ─── Command registration ────────────────────────────────────────────────────

@callback
def async_register(hass: HomeAssistant) -> None:
    """Register all Nova panel WebSocket commands. Idempotent-ish."""
    try:
        websocket_api.async_register_command(hass, ws_get_panel_data)
        websocket_api.async_register_command(hass, ws_get_activity_log)
        websocket_api.async_register_command(hass, ws_update_config)
        websocket_api.async_register_command(hass, ws_set_lockdown)
        websocket_api.async_register_command(hass, ws_get_knowledge)
        websocket_api.async_register_command(hass, ws_add_knowledge)
        websocket_api.async_register_command(hass, ws_forget_knowledge)
        websocket_api.async_register_command(hass, ws_clear_scene_memory)
        websocket_api.async_register_command(hass, ws_pending_fact_action)
        websocket_api.async_register_command(hass, ws_edit_pending_fact)
        websocket_api.async_register_command(hass, ws_root_cause)
        websocket_api.async_register_command(hass, ws_compute_camera_coverage)
        websocket_api.async_register_command(hass, ws_reload_appliances)
        websocket_api.async_register_command(hass, ws_search_memory)
        websocket_api.async_register_command(hass, ws_get_debug_log)
        websocket_api.async_register_command(hass, ws_get_cognitive_status)
        websocket_api.async_register_command(hass, ws_run_analysis)
        websocket_api.async_register_command(hass, ws_get_calibration)
        websocket_api.async_register_command(hass, ws_list_decisions)
        websocket_api.async_register_command(hass, ws_get_decision)
        websocket_api.async_register_command(hass, ws_set_decision_outcome)
        websocket_api.async_register_command(hass, ws_replay_decision)
        websocket_api.async_register_command(hass, ws_list_models)
        websocket_api.async_register_command(hass, ws_test_provider_endpoint)
        websocket_api.async_register_command(hass, ws_apply_ai_config)
        websocket_api.async_register_command(hass, ws_get_credential_status)
        websocket_api.async_register_command(hass, ws_set_credential)
        websocket_api.async_register_command(hass, ws_delete_credential)
        websocket_api.async_register_command(hass, ws_suggestion_action)
        websocket_api.async_register_command(hass, ws_list_automation_inventory)
        websocket_api.async_register_command(hass, ws_list_automation_trials)
        websocket_api.async_register_command(hass, ws_automation_trial_feedback)
        websocket_api.async_register_command(hass, ws_goal_action)
        websocket_api.async_register_command(hass, ws_get_person_routines)
        websocket_api.async_register_command(hass, ws_get_area_sparklines)
        websocket_api.async_register_command(hass, ws_camera_snapshot)
        websocket_api.async_register_command(hass, ws_camera_diagnostics)
        websocket_api.async_register_command(hass, ws_rename_camera)
        websocket_api.async_register_command(hass, ws_camera_location)
        websocket_api.async_register_command(hass, ws_mmwave_overview)
        websocket_api.async_register_command(hass, ws_documents)
        websocket_api.async_register_command(hass, ws_semantic_search)
        websocket_api.async_register_command(hass, ws_diagnostics)
        websocket_api.async_register_command(hass, ws_get_setup_health)
        websocket_api.async_register_command(hass, ws_say_hello)
        websocket_api.async_register_command(hass, ws_get_provider_activity)
        websocket_api.async_register_command(hass, ws_get_spoken_history)
        websocket_api.async_register_command(hass, ws_list_actions)
        websocket_api.async_register_command(hass, ws_repeat_spoken)
        websocket_api.async_register_command(hass, ws_voice_confirm_test)
        websocket_api.async_register_command(hass, ws_intrusion)
        websocket_api.async_register_command(hass, ws_mode)
        websocket_api.async_register_command(hass, ws_energy)
        websocket_api.async_register_command(hass, ws_solar)
        websocket_api.async_register_command(hass, ws_hazard)
        websocket_api.async_register_command(hass, ws_biometrics)
    except Exception as exc:
        _LOGGER.debug("WS command register note: %s", exc)


# ─── Helpers ────────────────────────────────────────────────────────────────

def _get_entry(hass: HomeAssistant):
    """Return the first Nova config entry's ConfigEntry object, or None."""
    # The ConfigEntry carries options/data and, once loaded, its NovaRuntime
    # (entry.runtime_data), which owns the live panel runtime_config.
    for entry in hass.config_entries.async_entries(DOMAIN):
        return entry
    return None


def _entry_opt(entry, key: str, default=None):
    """Config read via the canonical resolver (no hass here, so runtime_config is
    skipped): config.json → options → data → default."""
    from . import nova_config
    return nova_config.runtime_get(None, entry, key, default)


def _live_runtime_config(entry) -> dict:
    """The entry's live NovaRuntime.runtime_config, for synchronous reads on
    the event loop. {} when there is no entry or it is not loaded (setup still
    running, failed, or unloaded), so callers keep their defaults. Raises
    NovaRuntimeUnavailable for a loaded entry without a runtime rather than
    showing made-up defaults. Never reads hass.data."""
    if entry is None:
        return {}
    from .runtime import lifecycle_runtime_config
    return lifecycle_runtime_config(entry)


def _executor_runtime_config(entry) -> dict:
    """A fresh runtime_config snapshot to hand to one executor job. Same
    ownership rules as _live_runtime_config. Take it right before the
    async_add_executor_job call and never keep it for a later command."""
    if entry is None:
        return {}
    from .runtime import runtime_config_snapshot
    return runtime_config_snapshot(entry)


def _runtime_opt(hass: HomeAssistant, entry, key: str, default=None):
    """Runtime-aware config read: live NovaRuntime.runtime_config first, then
    the canonical resolver for the rest (config.json → options → data →
    default). A None or blank runtime value falls through, as before."""
    from . import nova_config
    rc = _live_runtime_config(entry)
    if key in rc and rc[key] not in (None, ""):
        return rc[key]
    return nova_config.runtime_get(None, entry, key, default)


def _int_opt(hass: HomeAssistant, entry, key: str, default: int) -> int:
    """Read an int option, preserving 0 — `value or default` clobbers a valid 0."""
    v = _runtime_opt(hass, entry, key, default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ─── WebSocket command ───────────────────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_panel_data",
})
@websocket_api.async_response
async def ws_get_panel_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return all data the panel needs for one render."""
    try:
        entry = _get_entry(hass)

        # ── Status flags ────────────────────────────────────────────────────
        observer_running = False
        if entry is not None:
            from .runtime import observer_status
            observer_running = observer_status(entry)

        bedroom_areas = _entry_opt(entry, CONF_BEDROOM_AREAS, []) or []
        quiet_start = _entry_opt(entry, CONF_OBSERVER_QUIET_START, DEFAULT_OBSERVER_QUIET_START)
        quiet_end = _entry_opt(entry, CONF_OBSERVER_QUIET_END, DEFAULT_OBSERVER_QUIET_END)

        sleeping, sleep_reason = sleep_detection.is_sleeping(
            hass,
            bedroom_area_ids=bedroom_areas,
            quiet_start=quiet_start,
            quiet_end=quiet_end,
        )

        gemini_key = bool(_entry_opt(entry, CONF_GEMINI_API_KEY, ""))
        broadcast_group = _entry_opt(entry, CONF_BROADCAST_GROUP, "") or ""
        from .notify_targets import configured_notify_services
        notify_config = {CONF_NOTIFY_SERVICE: _runtime_opt(
            hass, entry, CONF_NOTIFY_SERVICE, "") or ""}
        entry_notify_services = _runtime_opt(
            hass, entry, CONF_NOTIFY_SERVICES, None)
        if entry_notify_services is not None:
            notify_config[CONF_NOTIFY_SERVICES] = entry_notify_services
        selected_notify_services = configured_notify_services(notify_config)
        observer_enabled_cfg = bool(_runtime_opt(hass, entry, CONF_OBSERVER_ENABLED, False))

        sat_avail, sat_total = _satellite_count(hass)

        # ── Areas grid ──────────────────────────────────────────────────────
        areas_list = []
        for aid in _all_areas_with_anything(hass):
            caps = _area_capabilities(hass, aid)
            active = audio_routing.is_area_occupied(hass, aid)
            l_on, l_total = _area_light_state(hass, aid)
            readings = _area_live_readings(hass, aid)
            temp_eid, humidity_eid = _area_temp_humidity_entities(hass, aid)
            areas_list.append({
                "id":       aid,
                "name":     _area_name(hass, aid),
                "caps":     caps,
                "active":   active,
                "bedroom":  aid in bedroom_areas,
                "lights_on":    l_on,
                "lights_total": l_total,
                "temp":         readings.get("temp"),
                "humidity":     readings.get("humidity"),
                "temp_entity":     temp_eid,
                "humidity_entity": humidity_eid,
                "last_motion":  _format_duration(readings.get("last_motion_seconds")),
            })
        # Sort: active first, then bedrooms, then alphabetical
        areas_list.sort(key=lambda a: (not a["active"], not a["bedroom"], a["name"].lower()))

        # ── Dominant room ───────────────────────────────────────────────────
        dominant_id = _dominant_area(hass)
        if dominant_id:
            readings = _area_live_readings(hass, dominant_id)
            dominant_satellites = audio_routing.satellites_in_area(hass, dominant_id)
            sat_id = dominant_satellites[0] if dominant_satellites else None
            dominant = {
                "area_id":    dominant_id,
                "name":       _area_name(hass, dominant_id),
                "subtitle":   f"Occupied · {_format_duration(readings.get('last_motion_seconds'))}" if readings.get('last_motion_seconds') is not None else "Occupied",
                "coord":      f"#{dominant_id[:8]}",
                "temp":       readings.get("temp") or "—",
                "humidity":   readings.get("humidity") or "—",
                "lights":     readings.get("lights") or "—",
                "satellite":  sat_id.split(".", 1)[-1][:20] if sat_id else "—",
                "last_motion": _format_duration(readings.get("last_motion_seconds")),
            }
        else:
            # No presence detected anywhere
            anyone = audio_routing.anyone_home(hass)
            dominant = {
                "area_id":    None,
                "name":       "AWAY" if not anyone else "AT HOME",
                "subtitle":   "no presence detected",
                "coord":      "—",
                "temp":       "—",
                "humidity":   "—",
                "lights":     "—",
                "satellite":  "—",
                "last_motion": "—",
            }

        # ── Status tiles ────────────────────────────────────────────────────
        status = {
            "observer": {
                "state": "RUNNING" if observer_running else ("READY" if observer_enabled_cfg else "DISABLED"),
                "level": "live" if observer_running else ("warn" if observer_enabled_cfg else "off"),
            },
            "sleep": {
                "state": "ASLEEP" if sleeping else "AWAKE",
                "level": "warn" if sleeping else "live",
            },
            "gemini": {
                "state": "READY" if gemini_key else "UNSET",
                "level": "live" if gemini_key else "warn",
            },
            "broadcast": {
                "state": "ONLINE" if broadcast_group else "UNSET",
                "level": "live" if broadcast_group else "warn",
            },
            "notify": {
                "state": "READY" if selected_notify_services else "UNSET",
                "level": "live" if selected_notify_services else "warn",
            },
            "satellites": {
                "state": f"{sat_avail} / {sat_total}" if sat_total > 0 else "NONE",
                "level": "live" if sat_avail == sat_total and sat_total > 0 else ("warn" if sat_total > 0 else "off"),
            },
        }

        uptime_seconds = time.time() - _STARTUP_TS
        uptime_str = _format_uptime(uptime_seconds)

        # ── Config flags for settings panel ─────────────────────────────
        announcements_on = bool(_runtime_opt(hass, entry, "announcements_enabled", False))
        sentinel_on = bool(_runtime_opt(hass, entry, "sentinel_enabled", True))

        # Available notify services for phone notification dropdown
        notify_services = []
        try:
            for svc in hass.services.async_services().get("notify", {}):
                notify_services.append(f"notify.{svc}")
        except Exception:
            pass
        runtime_notify_services = _runtime_opt(
            hass, entry, CONF_NOTIFY_SERVICES, None)
        notify_config = {CONF_NOTIFY_SERVICE: str(
            _runtime_opt(hass, entry, CONF_NOTIFY_SERVICE, "") or "")}
        if runtime_notify_services is not None:
            notify_config[CONF_NOTIFY_SERVICES] = runtime_notify_services
        current_notify_services = configured_notify_services(notify_config)
        current_notify = current_notify_services[0] if current_notify_services else ""

        # Reads the whole doorbell log file on every panel poll: off the loop.
        doorbell_training_data = await hass.async_add_executor_job(
            _get_doorbell_training, hass)

        result = {
            "status":         status,
            "version":        _INTEGRATION_VERSION,
            "meta": {
                "bedrooms":          len(bedroom_areas),
                "areas_monitored":   len(areas_list),
                "announcements_today": _get_announcements_today(),
                "est_cost":          "—",
                "uptime":            uptime_str,
            },
            "dominant":       dominant,
            "areas":          areas_list,
            "sleep_reason":   sleep_reason if sleeping else None,
            "doorbell_training": doorbell_training_data,
            "doors":          _get_door_states(hass),
            "lockdown":       _get_lockdown_status(),
            "intrusion":      _get_intrusion_status(),
            "knowledge":      _get_knowledge_stats(),
            # Reads patterns.db: run it off the event loop.
            "suggestions":    await hass.async_add_executor_job(
                _get_suggestions, _entity_names(hass)),
            "suggestions_filtered": await hass.async_add_executor_job(
                _get_filtered_suggestions, _entity_names(hass)),
            "goals":          _get_goals(),
            "config": {
                "announcements_enabled": announcements_on,
                "sentinel_enabled": sentinel_on,
                "observer_enabled": observer_enabled_cfg,
                "pattern_learn_doors":     bool(_runtime_opt(hass, entry, "pattern_learn_doors", False)),
                "pattern_learn_presence":  bool(_runtime_opt(hass, entry, "pattern_learn_presence", False)),
                "pattern_learn_buttons":   bool(_runtime_opt(hass, entry, "pattern_learn_buttons", False)),
                # Phase 4, v7.109.0 — defaults to True (opt-out, not opt-in),
                # unlike the doors/presence/buttons toggles above which
                # default off: camera detections already reach Nova through
                # an integration the administrator explicitly set up (Eufy/
                # Frigate/Nest), so there's no new noise source being turned
                # on by default the way "log every door" would be.
                "camera_event_learning": bool(_runtime_opt(hass, entry, "camera_event_learning", True)),
                "camera_historical_awareness": bool(_runtime_opt(
                    hass, entry, "camera_historical_awareness",
                    _runtime_opt(hass, entry, "camera_event_learning", True),
                )),
                "camera_awareness_min_observations": _runtime_opt(
                    hass, entry, "camera_awareness_min_observations", 3,
                ),
                "scene_memory_enabled": _runtime_opt(
                    hass, entry, "scene_memory_enabled", False) is True,
                "scene_memory_retention_days": _runtime_opt(
                    hass, entry, "scene_memory_retention_days", 14,
                ),
                # Phase 10 (v7.112.0) — off by default; auto-mapping only
                # ever suggests, never enables a disabled System Monitor
                # entity (see host_health.py).
                "host_health_enabled": bool(_runtime_opt(hass, entry, "host_health_enabled", False)),
                "host_health_alerts_enabled": bool(_runtime_opt(hass, entry, "host_health_alerts_enabled", False)),
                "host_health_recovery_announce": bool(_runtime_opt(hass, entry, "host_health_recovery_announce", True)),
                "host_health_persistence_minutes": _runtime_opt(hass, entry, "host_health_persistence_minutes", 10),
                "host_health_cooldown_minutes": _runtime_opt(hass, entry, "host_health_cooldown_minutes", 60),
                "host_health_mappings": _get_runtime_json(hass, entry, "host_health_mappings", {}),
                "host_health_thresholds": _get_runtime_json(hass, entry, "host_health_thresholds", {}),
                "host_health_status": _get_host_health_status(hass, entry),
                "pattern_include_entities": _get_runtime_json(hass, entry, "pattern_include_entities", []),
                "excluded_entities": _get_runtime_json(hass, entry, "excluded_entities", []),
                "excluded_domains": _get_runtime_json(hass, entry, "excluded_domains", []),
                "excluded_labels": _get_runtime_json(hass, entry, "excluded_labels", []),
                "available_labels": _available_labels(hass),
                "cognition_enabled": bool(_runtime_opt(hass, entry, "cognition_enabled", True)),
                "camera_auto_analyze": bool(_runtime_opt(hass, entry, "camera_auto_analyze", True)),
                "camera_auto_analyze_motion": bool(_runtime_opt(hass, entry, "camera_auto_analyze_motion", False)),
                "package_detection": bool(_runtime_opt(hass, entry, "package_detection", True)),
                "visitor_learning": bool(_runtime_opt(hass, entry, "visitor_learning", True)),
                "rich_reasoning": bool(_runtime_opt(hass, entry, "rich_reasoning", False)),
                "light_control_enabled": bool(_runtime_opt(hass, entry, "light_control_enabled", True)),
                "departure_alerts_enabled": bool(_runtime_opt(hass, entry, "departure_alerts_enabled", True)),
                "routine_alerts_enabled": bool(_runtime_opt(hass, entry, "routine_alerts_enabled", True)),
                "departure_lead_minutes": _runtime_opt(hass, entry, "departure_lead_minutes", 30),
                "routine_departure_lead_minutes": _runtime_opt(hass, entry, "routine_departure_lead_minutes", 15),
                "departure_origin_entity": str(_runtime_opt(hass, entry, "departure_origin_entity", "") or ""),
                "departure_osrm_url": str(_runtime_opt(hass, entry, "departure_osrm_url", "") or ""),
                "departure_travel_sensor": str(_runtime_opt(hass, entry, "departure_travel_sensor", "") or ""),
                "identity_min_confidence": _runtime_opt(hass, entry, "identity_min_confidence", 0.45),
                "ollama_num_ctx": _runtime_opt(hass, entry, "ollama_num_ctx", 8192),
                "memory_threading_enabled": bool(_runtime_opt(hass, entry, "memory_threading_enabled", True)),
                "memory_threading_hours": _runtime_opt(hass, entry, "memory_threading_hours", 48),
                "memory_threading_max": _runtime_opt(hass, entry, "memory_threading_max", 12),
                "continued_conversation_enabled": bool(_runtime_opt(hass, entry, "continued_conversation_enabled", False)),
                "continued_conversation_speaker_reopen": bool(_runtime_opt(hass, entry, "continued_conversation_speaker_reopen", True)),
                "continued_conversation_multi_satellite": bool(_runtime_opt(hass, entry, "continued_conversation_multi_satellite", False)),
                "observer_group_debounce": _runtime_opt(hass, entry, "observer_group_debounce", 90),
                "adaptive_interruption_budget": bool(_runtime_opt(hass, entry, "adaptive_interruption_budget", False)),
                "adaptive_suggestion_threshold": bool(_runtime_opt(hass, entry, "adaptive_suggestion_threshold", False)),
                "adaptive_awareness": bool(_runtime_opt(hass, entry, "adaptive_awareness", False)),
                "tts_use_ha_voice": bool(_runtime_opt(hass, entry, "tts_use_ha_voice", False)),
                "pattern_learn_motion": bool(_runtime_opt(hass, entry, "pattern_learn_motion", False)),
                "sleep_override": sleep_detection.current_override(),
                "sleep_prompt_enabled": bool(_runtime_opt(hass, entry, "sleep_prompt_enabled", True)),
                "sleep_prompt_time": str(_runtime_opt(hass, entry, "sleep_prompt_time", "23:00") or "23:00"),
                "ground_floor_areas": _runtime_opt(hass, entry, CONF_GROUND_FLOOR_AREAS, []) or [],
                "operational_mode_auto": bool(_runtime_opt(hass, entry, "operational_mode_auto", True)),
                "lab_areas": _runtime_opt(hass, entry, "lab_areas", []) or [],
                "movie_area": str(_runtime_opt(hass, entry, "movie_area", "") or ""),
                "infrastructure_audit_area": str(_runtime_opt(hass, entry, "infrastructure_audit_area", "") or ""),
                "movie_media_player": str(_runtime_opt(hass, entry, "movie_media_player", "") or ""),
                "movie_dim_pct": int(_runtime_opt(hass, entry, "movie_dim_pct", 15) or 15),
                "llm_base_url": str(_runtime_opt(hass, entry, "llm_base_url", "") or ""),
                "ollama_base_url": str(_runtime_opt(hass, entry, "ollama_base_url", "") or ""),
                "custom_base_url": str(_runtime_opt(hass, entry, "custom_base_url", "") or ""),
                "self_hosted_endpoints_migrated": bool(_runtime_opt(
                    hass, entry, "self_hosted_endpoints_migrated", False)),
                "notify_service": current_notify,
                "notify_services": current_notify_services,
                "notify_services_available": notify_services,
                "security_alarm_entity": str(_runtime_opt(
                    hass, entry, "security_alarm_entity", "") or ""),
                "lockdown_auto_on_arm": _runtime_opt(
                    hass, entry, "lockdown_auto_on_arm", False) is True,
                "intrusion_requires_confinement": _runtime_opt(
                    hass, entry, "intrusion_requires_confinement", False) is True,
                "alarm_panels": _get_alarm_panels(hass),
                "onboarding": _get_onboarding_state(hass, entry, current_notify),
                "sentinel_rules": _get_sentinel_rules(),
                "disabled_sentinel_rules": _get_disabled_rules(hass, entry),
                "observer_stats": _get_observer_stats(),
                "lockdown": _get_lockdown_status(),
                "appliances": _get_appliance_status(),
                "appliance_profile": _get_runtime_json(hass, entry, "appliance_profile", []),
                "energy_cost_today_entity": str(_runtime_opt(hass, entry, "energy_cost_today_entity", "") or ""),
                "energy_cost_net_entity": str(_runtime_opt(hass, entry, "energy_cost_net_entity", "") or ""),
                "memory_stats": _get_memory_stats(),
                "satellites": _get_satellites(hass),
                "cast_devices": _get_cast_devices(hass),
                "cameras": _get_cameras(hass),
                "camera_overrides": _get_camera_overrides(),
                "camera_names": _get_camera_names(),
                "satellite_pairings": _get_runtime_json(hass, entry, "satellite_pairings", {}),
                "announcement_speakers": _get_runtime_json(hass, entry, "announcement_speakers", []),
                "room_speakers": _get_runtime_json(hass, entry, "room_speakers", {}),
                "general_speaker": str(_runtime_opt(hass, entry, "general_speaker", "") or ""),
                "speaker_areas": _get_speaker_assignable_areas(hass),
                "person_honorifics": _get_runtime_json(hass, entry, "person_honorifics", {}),
                "all_people": _get_all_people(hass),
                "floor_plan_rooms": _get_runtime_json(hass, entry, "floor_plan_rooms", {}),
                "floor_plan_bg": _get_runtime_json(hass, entry, "floor_plan_bg", {}),
                "door_mapping": _get_runtime_json(hass, entry, "door_mapping", {}),
                "arrival_front_door_entity": str(_runtime_opt(hass, entry, "arrival_front_door_entity", "") or ""),
                # AI model selection (provider + model per role) — for the
                # Settings "AI Models" section's live-fetched dropdowns.
                "llm_provider":        str(_runtime_opt(hass, entry, "llm_provider", "groq") or "groq"),
                "model":               str(_runtime_opt(hass, entry, "model", "") or ""),
                "classifier_provider": str(_runtime_opt(hass, entry, "classifier_provider", "groq") or "groq"),
                "classifier_model":    str(_runtime_opt(hass, entry, "classifier_model", "") or ""),
                "reasoning_provider":  str(_runtime_opt(hass, entry, "reasoning_provider", "groq") or "groq"),
                "reasoning_model":     str(_runtime_opt(hass, entry, "reasoning_model", "") or ""),
                "review_provider":     str(_runtime_opt(hass, entry, "review_provider", "groq") or "groq"),
                "review_model":        str(_runtime_opt(hass, entry, "review_model", "") or ""),
                "vision_provider":     str(_runtime_opt(hass, entry, "vision_provider", "groq") or "groq"),
                "vision_model":        str(_runtime_opt(hass, entry, "vision_model", "") or ""),
                "camera_reasoning_provider": str(_runtime_opt(hass, entry, "camera_reasoning_provider", "groq") or "groq"),
                "camera_reasoning_model":    str(_runtime_opt(hass, entry, "camera_reasoning_model", "") or ""),
                "suggestion_review_enabled": bool(_runtime_opt(hass, entry, "suggestion_review_enabled", False)),
                "suggestion_review_provider": str(_runtime_opt(hass, entry, "suggestion_review_provider", "")
                                                  or _runtime_opt(hass, entry, "llm_provider", "") or ""),
                "suggestion_review_model":   str(_runtime_opt(hass, entry, "suggestion_review_model", "")
                                                 or _runtime_opt(hass, entry, "model", "") or ""),
                # Nova Character & Research — these must be surfaced here or
                # the panel's selects snap back to their defaults on every
                # re-render even though the value was saved (v6.64.1 fix).
                "banter_level":         _runtime_opt(hass, entry, "banter_level", 1),
                "search_backend":       str(_runtime_opt(hass, entry, "search_backend", "duckduckgo") or "duckduckgo"),
                "searxng_url":          str(_runtime_opt(hass, entry, "searxng_url", "") or ""),
                "calendar_tight_gap_min": _runtime_opt(hass, entry, "calendar_tight_gap_min", 15),
                "recognition_source":   str(_runtime_opt(hass, entry, "recognition_source", "both") or "both"),
                "voice_confirm_enabled": bool(_runtime_opt(hass, entry, "voice_confirm_enabled", False)),
                "voice_confirm_mode":   str(_runtime_opt(hass, entry, "voice_confirm_mode", "auto") or "auto"),
                "intrusion_response_timeout": _runtime_opt(hass, entry, "intrusion_response_timeout", 120),
                "intrusion_vision_confirm": bool(_runtime_opt(hass, entry, "intrusion_vision_confirm", True)),
                # Scheduled briefings (v6.78.0)
                "briefing_morning_enabled": bool(_runtime_opt(hass, entry, "briefing_morning_enabled", False)),
                "briefing_evening_enabled": bool(_runtime_opt(hass, entry, "briefing_evening_enabled", False)),
                "briefing_morning_time": _runtime_opt(hass, entry, "briefing_morning_time", "07:30"),
                "briefing_evening_time": _runtime_opt(hass, entry, "briefing_evening_time", "19:30"),
                "briefing_require_home": bool(_runtime_opt(hass, entry, "briefing_require_home", True)),
                "briefing_include_weather": bool(_runtime_opt(hass, entry, "briefing_include_weather", True)),
                "briefing_include_calendar": bool(_runtime_opt(hass, entry, "briefing_include_calendar", True)),
                "briefing_include_events": bool(_runtime_opt(hass, entry, "briefing_include_events", True)),
                "briefing_include_energy": bool(_runtime_opt(hass, entry, "briefing_include_energy", True)),
                "briefing_include_hazards": bool(_runtime_opt(hass, entry, "briefing_include_hazards", True)),
                # Hazard monitor controls — same read-back requirement (v6.71.0)
                "hazard_monitor_enabled": bool(_runtime_opt(hass, entry, "hazard_monitor_enabled", False)),
                "hazard_lat":             _runtime_opt(hass, entry, "hazard_lat", ""),
                "hazard_lon":             _runtime_opt(hass, entry, "hazard_lon", ""),
                "hazard_quakes_on":       bool(_runtime_opt(hass, entry, "hazard_quakes_on", True)),
                "hazard_weather_on":      bool(_runtime_opt(hass, entry, "hazard_weather_on", True)),
                "hazard_disasters_on":    bool(_runtime_opt(hass, entry, "hazard_disasters_on", True)),
                "hazard_quake_radius_km": _runtime_opt(hass, entry, "hazard_quake_radius_km", 300),
                "hazard_quake_min_mag":   _runtime_opt(hass, entry, "hazard_quake_min_mag", 2.5),
                # Residence model detail controls — same read-back requirement:
                # these save fine but reset on re-render unless surfaced here.
                "residence_style":      str(_runtime_opt(hass, entry, "residence_style", "cape_cod") or "cape_cod"),
                "floor_plan_sqft":      _runtime_opt(hass, entry, "floor_plan_sqft", ""),
                "floor_plan_units":     str(_runtime_opt(hass, entry, "floor_plan_units", "imperial") or "imperial"),
                "floor_plan_elements":  _get_runtime_json(hass, entry, "floor_plan_elements", {}),
                "floor_plan_cameras":   _get_runtime_json(hass, entry, "floor_plan_cameras", {}),
                "floor_plan_property":  _get_runtime_json(hass, entry, "floor_plan_property", {}),
                "floor_plan_entities":  _get_runtime_json(hass, entry, "floor_plan_entities", {}),
                "floor_plan_bg_opacity": _runtime_opt(hass, entry, "floor_plan_bg_opacity", "0.2"),
                "home_context_max_entities": _int_opt(hass, entry, "home_context_max_entities", 15),
                "ui_language": _runtime_opt(hass, entry, "ui_language", "auto"),
                "disabled_cameras":     _get_runtime_json(hass, entry, "disabled_cameras", []),
                "home_stories":         _runtime_opt(hass, entry, "home_stories", "1.5"),
                "has_basement":         _runtime_opt(hass, entry, "has_basement", True),
                "dormers_front":        _runtime_opt(hass, entry, "dormers_front", 2),
                "dormers_rear":         _runtime_opt(hass, entry, "dormers_rear", 1),
                "garage_bays":          _runtime_opt(hass, entry, "garage_bays", 3),
                "chimney_side":         str(_runtime_opt(hass, entry, "chimney_side", "right") or "right"),
                "home_bedrooms":        _runtime_opt(hass, entry, "home_bedrooms", ""),
                "home_bathrooms":       _runtime_opt(hass, entry, "home_bathrooms", ""),
            },
        }
        connection.send_result(msg["id"], result)
    except Exception as exc:
        _LOGGER.exception("ws_get_panel_data failed: %s", exc)
        connection.send_error(msg["id"], "panel_data_failed", safe_error_message(exc))


def _door_entity_open(state_obj) -> bool:
    """Back-compat shim — door open logic now lives in door_state.py."""
    from . import door_state
    return door_state.entity_is_open(state_obj)


def _get_door_states(hass: HomeAssistant) -> dict:
    """
    Open/closed state of the home's doors for the Residence 3D model. Reads the
    explicit ``door_mapping`` (slot -> entity_id) the user set on the Residence
    tab, then delegates to door_state.get_door_states which honours it and
    auto-detects the rest. Never raises.
    """
    try:
        from . import door_state
        entry = _get_entry(hass)
        mapping = _get_runtime_json(hass, entry, "door_mapping", {}) or {}
        return door_state.get_door_states(hass, mapping)
    except Exception:
        return {}


def _get_disabled_rules(hass: HomeAssistant, entry) -> list[str]:
    """Return list of disabled sentinel rule IDs from runtime config."""
    if entry is None:
        return []
    rc = _live_runtime_config(entry)
    raw = rc.get("disabled_sentinel_rules", _entry_opt(entry, "disabled_sentinel_rules", "[]"))
    if isinstance(raw, list):
        return raw
    try:
        import json
        return json.loads(raw) if isinstance(raw, str) else []
    except Exception:
        return []


def _get_host_health_status(hass: HomeAssistant, entry) -> dict:
    """Live discovery + mapping + snapshot for the Host Health settings card
    (Phase 10) — candidate entities per metric, current mapping status
    (mapped/ambiguous/missing/disabled), and the last tick's readings.
    Computed fresh on every panel load; never mutates host_health's own
    persistence state machine (that only advances on the periodic tick)."""
    try:
        from . import host_health
        config = {
            "host_health_enabled": bool(_runtime_opt(hass, entry, "host_health_enabled", False)),
            "host_health_mappings": _get_runtime_json(hass, entry, "host_health_mappings", {}),
            "host_health_thresholds": _get_runtime_json(hass, entry, "host_health_thresholds", {}),
        }
        candidates = host_health.discover_candidates(hass)
        mappings = host_health.resolve_mappings(hass, config, candidates)
        metrics = []
        for metric in host_health.METRICS:
            mapping = mappings[metric.key]
            metrics.append({
                "key": metric.key,
                "label": metric.label,
                "recommended": metric.recommended,
                "status": mapping.status,
                "source": mapping.source,
                "entity_id": mapping.entity_id,
                "candidates": [
                    {"entity_id": c.entity_id, "friendly_name": c.friendly_name,
                     "disabled": c.disabled}
                    for c in mapping.candidates
                ],
            })
        return {"metrics": metrics, "snapshot": host_health.snapshot(hass, config)}
    except Exception as exc:
        return {"error": safe_error_message(exc, where="host health read", log=True)}


# ─── Activity log WebSocket command ──────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_activity_log",
    vol.Optional("hours", default=24): int,
    vol.Optional("limit", default=50): int,
})
@websocket_api.async_response
async def ws_get_activity_log(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return recent activity log entries for the panel."""
    try:
        from .database import get_recent_activity
        entries = await hass.async_add_executor_job(
            lambda: get_recent_activity(hours=msg["hours"], limit=msg["limit"])
        )
        # Format for the panel
        from .cognitive.naming import humanize_text
        names = _entity_names(hass)
        result = []
        for e in entries:
            ts_str = e.get("timestamp", "")
            # Parse "2026-04-23T05:30:00" → "05:30"
            try:
                from datetime import datetime as _dt, timezone as _tz
                from homeassistant.util import dt as dt_util
                # Parse UTC timestamp and convert to local
                dt = _dt.fromisoformat(ts_str).replace(tzinfo=_tz.utc)
                local_dt = dt_util.as_local(dt)
                hhmm = local_dt.strftime("%H:%M")
            except Exception:
                hhmm = ts_str[:5] if len(ts_str) >= 5 else ts_str
            result.append({
                "ts": hhmm,
                "urgency": e.get("urgency", "low"),
                "tag": ((names.get(e.get("entity_id", "")) or
                         e.get("entity_id", "").split(".", 1)[-1])[:20]
                        or e.get("source", "")).upper(),
                "msg": humanize_text(e.get("message", ""), names),
                "source": e.get("source", "observer"),
            })
        connection.send_result(msg["id"], {"entries": result})
    except Exception as exc:
        _LOGGER.warning("ws_get_activity_log failed: %s", exc)
        connection.send_error(msg["id"], "activity_log_failed", safe_error_message(exc))

# ─── Config update WebSocket command ─────────────────────────────────────────

# Only these keys can be toggled from the panel. Prevents arbitrary writes.
PANEL_WRITABLE_KEYS = {
    "ui_language",
    "announcements_enabled",
    "sentinel_enabled",
    "observer_enabled",
    "pattern_learn_doors",         # learn door/window activity for routines
    "pattern_learn_presence",      # learn presence/arrivals for routines
    "pattern_learn_buttons",       # learn button/remote presses ("press -> scene")
    "pattern_include_entities",    # JSON list: specific entities to always learn
    "excluded_entities",           # JSON list: entity_ids removed from Nova's awareness
    "excluded_domains",            # JSON list: whole domains removed from awareness
    "excluded_labels",             # JSON list: HA labels whose entities are removed from awareness
    "notify_service",
    "notify_services",             # JSON list: normal alert push targets
    "security_alarm_entity",       # str: authoritative household alarm panel
    "lockdown_auto_on_arm",        # bool: explicit opt in for automatic lockdown
    "intrusion_requires_confinement",  # bool: intrusion monitoring only while locked down or alarm armed
    "departure_alerts_enabled",
    "routine_alerts_enabled",
    "departure_lead_minutes",
    "routine_departure_lead_minutes",  # int: remind this long before a usual departure
    "departure_origin_entity",
    "departure_osrm_url",
    "departure_travel_sensor",
    "memory_threading_enabled",
    "memory_threading_hours",
    "memory_threading_max",
    "continued_conversation_enabled",
    "continued_conversation_speaker_reopen",   # bool: Nova times the follow-up mic reopen to the reply speaker finishing
    "continued_conversation_multi_satellite",  # bool: follow-up follows the person to another room's satellite when the start room empties
    "disabled_sentinel_rules",   # JSON list of disabled rule IDs
    "satellite_pairings",        # JSON dict: {satellite_entity_id: cast_entity_id}
    "announcement_speakers",     # JSON list of cast entity IDs for announcements
    "room_speakers",             # JSON dict: {area_id: media_player_entity_id} (v7.92.0)
    "general_speaker",           # str: fallback speaker for rooms with no assignment (v7.92.0)
    "person_honorifics",         # JSON dict: {person_entity_id: honorific} — used only when
                                  # that person is home alone; see honorific.py (v7.99.0)
    "floor_plan_rooms",          # JSON: floor plan room positions per floor
    "floor_plan_bg",             # JSON: base64 background images per floor
    # Residence model (the 3D house on the Residence tab)
    "residence_style",           # str: home style template (cape_cod, ranch, …)
    "floor_plan_sqft",           # str/int: estimated square footage
    "floor_plan_units",          # "imperial" | "metric" for room dimensions
    "floor_plan_elements",       # JSON: placed windows/doors per floor (+ sensor map)
    "floor_plan_cameras",        # JSON: placed cameras per floor (pos/angle/fov/range) (v7.17.0)
    "floor_plan_property",       # JSON: property boundary polygon {points:[[x,y],...]} (v7.22.0)
    "floor_plan_entities",       # JSON: devices pinned on the plan per floor [{e: entity_id, x, y}] (v7.101.18)
    "floor_plan_bg_opacity",     # str/float 0-1: opacity of the imported floor-plan background image (v7.101.18)
    "home_context_max_entities", # int: entity names per domain in the system prompt (0=counts only) (v7.23.1)
    "disabled_cameras",          # JSON list: camera entity_ids Nova must not use
    "home_stories",              # str: number of stories (controls floor tabs)
    "has_basement",              # bool: whether to show the basement floor
    "dormers_front",             # int: front dormer count override
    "dormers_rear",              # int: rear dormer count override
    "garage_bays",               # int: garage bay count
    "chimney_side",              # str: chimney placement (left/right)
    "home_bedrooms",             # int: bedroom count (Residence stats)
    "home_bathrooms",            # int: bathroom count (Residence stats)
    "door_mapping",              # JSON: {model door slot -> entity_id}
    "arrival_front_door_entity", # str: binary_sensor gating the arrival-briefing trigger
    # Outdoor classification (feeds the intrusion false-alarm guards)
    "outdoor_areas",             # JSON list: extra area names treated as outdoor
    "outdoor_entities",          # JSON list: entity globs forced outdoor
    "indoor_entities",           # JSON list: entity globs forced indoor (wins)
    # Web Research + Communication agents (v6.51.0)
    "search_backend",            # str: "duckduckgo" (default) | "searxng"
    "searxng_url",               # str: SearXNG base URL when backend=searxng
    "calendar_tight_gap_min",    # int: back-to-back gap flagged as "tight"
    # Persona (v6.51.0)
    "banter_level",              # int: 0 plain · 1 dry (default) · 2 full wit
    # Local semantic search via Ollama embeddings (v6.57.0)
    "semantic_search",           # bool: use Ollama embeddings for doc retrieval
    "embed_model",               # str: Ollama embed model (default nomic-embed-text)
    "embed_base_url",            # str: override Ollama host for embeddings
    "custom_modes",              # dict: user-defined operational modes (v6.61.0)
    "operational_mode_auto",     # bool: auto away/normal by occupancy (v7.14.0)
    "lab_areas",                 # list: rooms Lab mode is scoped to (v7.15.0)
    "movie_area",                # str: room Movie mode is bound to (v7.15.0)
    "movie_media_player",        # str: Movie media_player binding (v7.15.0)
    "movie_dim_pct",             # int: Movie mood dim level 0-100 (v7.15.0)
    "energy_agency",             # str: advisory | opt_in | autonomous (v6.62.0)
    "energy_peak_watts",         # float: whole-home peak threshold in watts
    "energy_mode_bump",          # list: modes that raise energy agency one step
    "biometrics_enabled",        # bool: read wearable context (opt-in) (v6.63.0)
    "biometric_entities",        # dict: explicit kind→entity_id overrides
    "recognition_source",        # str: both | doubletake | frigate (v6.64.1)
    "voice_confirm_enabled",      # bool: voice-confirm sensitive actions (v6.67.0)
    "voice_confirm_mode",         # str: native | gated | auto
    "voice_confirm_entities",     # list: extra entities to confirm / !exempt
    "satellite_audio_out",        # dict/bool: satellites whose audio routes out
    "satellite_start_action",     # dict: satellite → esphome start action
    "intrusion_response_timeout", # float: secs before unanswered alert escalates
    "onboarding_dismissed",       # bool: user dismissed the first-run welcome card
    # Multi-hazard monitor (v6.71.0)
    "hazard_monitor_enabled",     # bool: master on/off for hazard polling
    "hazard_lat",                 # float|"": location override latitude
    "hazard_lon",                 # float|"": location override longitude
    "hazard_quakes_on",           # bool: earthquake feed
    "hazard_weather_on",          # bool: NWS severe-weather feed
    "hazard_disasters_on",        # bool: NASA EONET disaster feed
    "hazard_quake_radius_km",     # float: earthquake radius
    "hazard_quake_min_mag",       # float: min magnitude to alert
    "hazard_disaster_radius_km",  # float: disaster radius
    "intrusion_vision_confirm",   # bool: verify Frigate person detection with Nova vision before escalating
    "intrusion_inward_depth",     # int: rooms deep from breach motion must reach to confirm
    # Scheduled briefings (v6.78.0)
    "briefing_morning_enabled",   # bool: deliver a morning briefing
    "briefing_evening_enabled",   # bool: deliver an evening briefing
    "briefing_morning_time",      # str "HH:MM"
    "briefing_evening_time",      # str "HH:MM"
    "briefing_require_home",      # bool: skip when nobody is home
    "briefing_include_weather",
    "briefing_include_calendar",
    "briefing_include_presence",
    "briefing_include_events",
    "briefing_include_energy",
    "briefing_include_hazards",
    "document_watch_folders",    # str/list: extra folders to auto-ingest new docs from
    # AI model selection (Settings → AI Models live-fetched dropdowns)
    "llm_provider",
    "model",
    "llm_base_url",
    "ollama_base_url",
    "custom_base_url",
    "classifier_provider",
    "classifier_model",
    "reasoning_provider",
    "reasoning_model",
    "review_provider",
    "review_model",
    "vision_provider",
    "vision_model",
    "camera_reasoning_provider",
    "camera_reasoning_model",
    "suggestion_review_enabled",   # bool: AI review of learned suggestions (opt in)
    "suggestion_review_provider",
    "suggestion_review_model",
    "classifier_rate_limit",
    "intrusion_notify_image_ttl_minutes",  # minutes: signed notification-image copy lifetime (v7.102.0)
    "cognition_enabled",
    "cognition_threshold",
    "observer_group_debounce",      # seconds: coalesce a burst of numbered sibling entities (0 = off)
    "adaptive_interruption_budget",  # bool: scale the announcement cap down when recent proactive decisions were unwelcome
    "adaptive_suggestion_threshold", # bool: tune the suggestion confidence bar from how welcome recent suggestions were
    "adaptive_awareness",            # bool: anticipation alerts learn from alerts the user mutes
    "tts_use_ha_voice",              # bool: prefer the TTS entity of the preferred Assist pipeline
    "infrastructure_audit_area",     # area id the infrastructure audit speaks in ("" = log only)
    "pattern_learn_motion",          # bool: learn motion/occupancy triggers for "when X, do Y" suggestions (rate-limited)
    "camera_event_learning",        # bool: feed Eufy/Frigate/Nest/vision detections into pattern learning (Phase 4, v7.109.0)
    "camera_event_confidence_floor",  # float 0-100: minimum source-supplied confidence to record a camera event (0 = off)
    "camera_event_dedup_window",    # float seconds: window collapsing duplicate camera events across sources
    "camera_historical_awareness",  # bool: add repeated camera history to interactive prompts (Phase 5)
    "camera_awareness_min_observations",  # int 3-12: historical evidence floor
    "scene_memory_enabled",         # bool: keep camera descriptions so Nova can answer "where did I last see X"
    "scene_memory_retention_days",  # int 1-90: how long scene memory keeps a description
    "host_health_enabled",           # bool: master on/off for Home Assistant host telemetry (Phase 10, v7.112.0)
    "host_health_alerts_enabled",    # bool: separate opt-in for spoken/pushed host-health alerts
    "host_health_recovery_announce", # bool: announce a stable recovery, bounded and optional
    "host_health_persistence_minutes",  # float 2-120: sustained-breach window before the first alert
    "host_health_cooldown_minutes",     # float 5-720: minimum gap between repeat alerts on an unresolved problem
    "host_health_mappings",          # dict: metric_key -> sensor.* entity_id (manual System Monitor mapping)
    "host_health_thresholds",        # dict: metric_key -> float (per-metric alert threshold override)
    "appliance_profile",            # JSON list of declared appliances (name/type/entity/watts)
    "camera_auto_analyze",          # bool: auto-inspect doorbell/person camera events
    "camera_auto_analyze_motion",   # bool: also auto-inspect motion events (noisier)
    "package_detection",            # bool: watch porch cameras for packages & mail
    "visitor_learning",             # bool: silent vision learning from person events
    "rich_reasoning",               # bool: cloud-first reasoning for medium+ events
    "llm_base_url",                 # legacy shared self-hosted endpoint
    "ollama_base_url",              # str: dedicated Ollama endpoint
    "custom_base_url",              # str: dedicated OpenAI-compatible endpoint
    "pattern_min_occurrences",      # int: pattern engine repeat threshold
    "pattern_confidence",           # float: pattern engine confidence threshold
    "light_control_enabled",        # bool: allow toggling lights from the dashboard
    "energy_cost_today_entity",     # str: entity_id of an install's own "cost today" sensor,
                                     # preferred over solar.py's generic price x kWh estimate
    "energy_cost_net_entity",       # str: entity_id of an install's own "net cost today" sensor
                                     # (post export-credit); optional, paired with the above
    "identity_min_confidence",      # float: face-match threshold below which a person is 'unknown'
    "ollama_num_ctx",               # int: Ollama context window for local models
    # Sleep state (v7.86.0)
    "sleep_override",               # str: "auto" | "awake" | "asleep" — panel dropdown
    "sleep_prompt_enabled",         # bool: send the nightly "Heading to bed?" prompt
    "sleep_prompt_time",            # str: HH:MM — earliest the prompt may fire
}

# ── Integration version ──────────────────────────────────────────────────────
from pathlib import Path as _Path
import json as _json_mod


def _read_integration_version() -> str:
    """
    Read the integration version from manifest.json — the single source of
    truth. Done once at import (not per-request) so the panel can display the
    actually-running version. This fixes the banner drifting out of sync: the
    version was hardcoded in the panel JS, so a browser-cached panel showed a
    stale number after an addon update. Now the panel fetches this live.
    """
    try:
        mf = _Path(__file__).parent / "manifest.json"
        return _json_mod.loads(mf.read_text()).get("version", "?")
    except Exception:
        return "?"


_INTEGRATION_VERSION = _read_integration_version()


def _named_log_entries(entries: list, names: dict) -> list:
    """Display copies of log entries with known entity_ids named."""
    if not names:
        return entries
    try:
        from .cognitive.naming import humanize_text
        return [dict(e, msg=humanize_text(e.get("msg", ""), names))
                if isinstance(e, dict) else e for e in entries]
    except Exception:
        return entries


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/reload_appliances",
})
@websocket_api.async_response
async def ws_reload_appliances(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Restart the appliance monitor so profile edits take effect immediately
    (no Home Assistant restart needed)."""
    try:
        from . import appliance_monitor, nova_config
        entry = _get_entry(hass)
        cfg: dict = {}
        if entry:
            cfg = await hass.async_add_executor_job(
                nova_config.effective_config_with_runtime, entry,
                _executor_runtime_config(entry))
        await appliance_monitor.start(hass, cfg, entry=entry)
        connection.send_result(msg["id"], {
            "ok": True, "appliances": _get_appliance_status(),
        })
    except Exception as exc:
        connection.send_error(msg["id"], "reload_failed", safe_error_message(exc, where="reload_appliances", log=True))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/set_lockdown",
    vol.Required("on"): bool,
})
@websocket_api.async_response
async def ws_set_lockdown(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Engage or lift the formal lockdown from the panel."""
    try:
        from . import cognitive_core
        ok = await cognitive_core.request_lockdown(
            bool(msg["on"]), reason="requested from panel", hass=hass)
        status = cognitive_core.lockdown_status()
        if not ok:
            _LOGGER.warning("Panel lockdown request returned not-ok (on=%s); status=%s",
                            bool(msg["on"]), status)
        connection.send_result(msg["id"], {"ok": ok, "lockdown": status})
    except Exception as exc:
        _LOGGER.exception("Panel lockdown request failed: %s", exc)
        connection.send_error(msg["id"], "lockdown_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_knowledge",
    vol.Optional("subject"): str,
})
@websocket_api.async_response
async def ws_get_knowledge(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return the curated facts Nova knows, for the Memory panel.

    `facts` is confirmed-only (v7.88.0) -- a fact agent.py's `remember` tool
    staged as pending must not appear here as if it were already established;
    `pending` carries those separately so the panel can show a distinct
    review queue (confirm / reject / edit) instead of silently merging them
    into the trusted list.
    """
    try:
        from . import knowledge
        subject = msg.get("subject")
        facts = await hass.async_add_executor_job(
            lambda: knowledge.all_facts(subject=subject, status="confirmed"))
        pending = await hass.async_add_executor_job(
            lambda: knowledge.pending_facts(subject=subject))
        kstats = await hass.async_add_executor_job(knowledge.stats)
        connection.send_result(msg["id"], {"facts": facts, "pending": pending, "stats": kstats})
    except Exception as exc:
        _LOGGER.exception("get_knowledge failed: %s", exc)
        connection.send_error(msg["id"], "knowledge_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/add_knowledge",
    vol.Required("key"): str,
    vol.Required("value"): str,
    vol.Optional("subject"): str,
    vol.Optional("kind"): str,
})
@websocket_api.async_response
async def ws_add_knowledge(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Teach Nova a fact from the Memory panel."""
    try:
        from . import knowledge
        f = await hass.async_add_executor_job(
            lambda: knowledge.remember(
                msg["key"], msg["value"],
                subject=msg.get("subject", knowledge.DEFAULT_SUBJECT),
                kind=msg.get("kind", "fact"), source="stated"))
        facts = await hass.async_add_executor_job(knowledge.all_facts)
        connection.send_result(msg["id"], {"ok": bool(f), "facts": facts})
    except Exception as exc:
        _LOGGER.exception("add_knowledge failed: %s", exc)
        connection.send_error(msg["id"], "add_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/clear_scene_memory",
})
@websocket_api.async_response
async def ws_clear_scene_memory(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Forget every camera description scene memory has kept."""
    try:
        from . import scene_memory
        removed = await hass.async_add_executor_job(scene_memory.forget_all)
        stats = await hass.async_add_executor_job(scene_memory.stats)
        connection.send_result(msg["id"], {"removed": removed, "stats": stats})
    except Exception as exc:
        _LOGGER.exception("clear_scene_memory failed: %s", exc)
        connection.send_error(msg["id"], "clear_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/forget_knowledge",
    vol.Optional("fact_id"): int,
    vol.Optional("subject"): str,
    vol.Optional("key"): str,
})
@websocket_api.async_response
async def ws_forget_knowledge(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Forget a fact (by fact_id, or subject+key) from the Memory panel.

    NOTE: the fact id is carried as ``fact_id``, not ``id`` — ``id`` is reserved
    by the HA WebSocket protocol for the message sequence number (the frontend
    overwrites any ``id`` we send), so using it here silently deleted nothing.
    """
    try:
        from . import knowledge
        fid = msg.get("fact_id")
        removed = await hass.async_add_executor_job(
            lambda: knowledge.forget(fact_id=fid, subject=msg.get("subject"), key=msg.get("key")))
        facts = await hass.async_add_executor_job(knowledge.all_facts)
        connection.send_result(msg["id"], {"removed": removed, "facts": facts})
    except Exception as exc:
        _LOGGER.exception("forget_knowledge failed: %s", exc)
        connection.send_error(msg["id"], "forget_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/pending_fact_action",
    vol.Required("fact_id"): int,
    vol.Required("action"): vol.In(["confirm", "reject"]),
})
@websocket_api.async_response
async def ws_pending_fact_action(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Confirm or reject a fact agent.py's `remember` tool staged as pending
    (v7.88.0), from the Memory panel's review queue -- the fallback for when
    the user didn't (or couldn't) confirm it inline in the conversation that
    proposed it."""
    try:
        from . import knowledge
        fid = msg["fact_id"]
        if msg["action"] == "confirm":
            ok = await hass.async_add_executor_job(knowledge.confirm_fact, fid)
        else:
            ok = bool(await hass.async_add_executor_job(lambda: knowledge.forget(fact_id=fid)))
        facts = await hass.async_add_executor_job(lambda: knowledge.all_facts(status="confirmed"))
        pending = await hass.async_add_executor_job(knowledge.pending_facts)
        connection.send_result(msg["id"], {"ok": ok, "facts": facts, "pending": pending})
    except Exception as exc:
        _LOGGER.exception("pending_fact_action failed: %s", exc)
        connection.send_error(msg["id"], "pending_fact_action_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/edit_pending_fact",
    vol.Required("fact_id"): int,
    vol.Required("value"): str,
})
@websocket_api.async_response
async def ws_edit_pending_fact(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Correct a pending fact's value before confirming it (v7.88.0) — the
    one capability the Memory panel didn't have for any fact before this."""
    try:
        from . import knowledge
        updated = await hass.async_add_executor_job(
            lambda: knowledge.edit_fact(msg["fact_id"], msg["value"]))
        pending = await hass.async_add_executor_job(knowledge.pending_facts)
        connection.send_result(msg["id"], {"ok": bool(updated), "pending": pending})
    except Exception as exc:
        _LOGGER.exception("edit_pending_fact failed: %s", exc)
        connection.send_error(msg["id"], "edit_pending_fact_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/root_cause",
    vol.Required("entity_id"): str,
    vol.Optional("event_time"): str,
    vol.Optional("window_secs"): int,
})
@websocket_api.async_response
async def ws_root_cause(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Root cause analysis for an entity's (latest or specified) change —
    the same engine the conversational 'why did …' tool uses, structured for
    the panel."""
    try:
        from . import rca
        names = rca.entity_names(hass)
        result = await hass.async_add_executor_job(
            lambda: rca.analyze(
                msg["entity_id"],
                msg.get("event_time"),
                int(msg.get("window_secs") or rca.DEFAULT_WINDOW_SECS),
                names=names))
        connection.send_result(msg["id"], result)
    except Exception as exc:
        _LOGGER.exception("root_cause failed: %s", exc)
        connection.send_error(msg["id"], "root_cause_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/compute_camera_coverage",
    vol.Required("camera"): dict,
})
@websocket_api.async_response
async def ws_compute_camera_coverage(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Judge one camera's coverage (which rooms it can confirm + a human reason)
    from the geometric candidates the panel supplies. Uses the reasoning LLM,
    falling back to a geometry-only summary."""
    try:
        from . import camera_coverage, nova_config
        entry = _get_entry(hass)
        try:
            config = nova_config.effective_config(entry) if entry else {}
        except Exception:
            config = {}
        result = await camera_coverage.infer_coverage(hass, config, msg["camera"])
        connection.send_result(msg["id"], result)
    except Exception as exc:
        _LOGGER.exception("compute_camera_coverage failed: %s", exc)
        connection.send_error(msg["id"], "coverage_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/update_config",
    vol.Required("key"): str,
    vol.Required("value"): vol.Any(bool, str, int, float, None),
})
@websocket_api.async_response
async def ws_update_config(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """
    Update a config toggle from the panel.

    Stores in NovaRuntime.runtime_config (NOT entry.options) to avoid
    triggering an entry reload which would navigate the browser away
    from the panel. Sentinel and observer check runtime_config first,
    then fall back to entry.options.

    The runtime is resolved before anything is written or applied, so an
    entry without one fails with update_failed and nothing changes.
    observer_enabled is transactional: the observer is started or stopped
    first, and only a successful transition updates the observer state,
    runtime_config and config.json.
    """
    key = msg["key"]
    value = msg["value"]

    # Defense in depth (Phase 2, v7.107.0): no credential key is in
    # PANEL_WRITABLE_KEYS today, so this is already unreachable in practice —
    # but a credential must never be writable, loggable, or land in panel
    # runtime_config through this generic command, even if that allowlist is
    # ever edited by mistake. Credentials go only through the dedicated
    # nova/set_credential and nova/delete_credential commands.
    from . import ha_secrets
    if key in ha_secrets.CREDENTIAL_KEYS:
        connection.send_error(
            msg["id"], "invalid_key",
            "Credentials are set via nova/set_credential, not nova/update_config",
        )
        return

    if key not in PANEL_WRITABLE_KEYS:
        connection.send_error(
            msg["id"], "invalid_key",
            f"Key '{key}' is not writable from the panel",
        )
        return

    from . import safety_config
    if not safety_config.valid_panel_value(key, value):
        connection.send_error(
            msg["id"], "invalid_value",
            f"Key '{key}' requires a boolean value",
        )
        return

    entry = _get_entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "no_entry", "No Nova config entry found")
        return

    try:
        from .runtime import get_runtime
        # Ownership first: resolve the runtime before any write, save, cache
        # invalidation or side effect. Raises (→ update_failed) without one.
        runtime = get_runtime(entry)
        rc = runtime.runtime_config
        if not isinstance(rc, dict):
            connection.send_error(msg["id"], "no_data", "Nova runtime data not found")
            return

        # observer_enabled: change the observer first and confirm the result
        # with is_running(). A failed or unconfirmed start or stop raises
        # here, before runtime_config, observer state or config.json are
        # touched. No rollback afterwards, so there is never a second
        # observer transition.
        if key == "observer_enabled":
            from . import observer as observer_mod
            from .runtime import set_observer_running
            # Never start or stop an unowned observer.
            get_runtime(entry)
            if value:
                # Already running: never restart it.
                if not observer_mod.is_running():
                    from . import nova_config
                    # The candidate is this operation's own snapshot plus the
                    # requested value; the executor never sees the live dict.
                    from .runtime import runtime_config_snapshot
                    candidate = runtime_config_snapshot(entry, strict=True)
                    candidate[key] = value
                    observer_config = await hass.async_add_executor_job(
                        nova_config.effective_config_with_runtime, entry, candidate)
                    await observer_mod.start(hass, observer_config, entry=entry)
                # start() can return without the observer running; confirm
                # the requested state before anything is recorded.
                if not observer_mod.is_running():
                    raise RuntimeError("Observer did not start")
                set_observer_running(entry, True)
            else:
                # Already stopped: never stop it again.
                if observer_mod.is_running():
                    await observer_mod.stop()
                if observer_mod.is_running():
                    raise RuntimeError("Observer did not stop")
                set_observer_running(entry, False)

        # Store in runtime_config — does NOT trigger entry reload
        rc[key] = value
        _LOGGER.info("Nova panel: set %s = %s", key, str(value)[:80])

        # Disabling cognition opens an observation gap now, not at the next
        # state change: "none yet today" must never span it (v7.120.2).
        if key == "cognition_enabled" and not value:
            from . import cognition
            cognition.mark_unobserved()

        # llm_base_url is the saved endpoint identity custom/ollama discovery
        # is cached under (Phase 3, v7.108.0) — a stale cached list for the
        # old endpoint must not survive the endpoint changing.
        if key in ("llm_base_url", "ollama_base_url", "custom_base_url"):
            if key != "ollama_base_url":
                invalidate_model_cache("custom")
            if key != "custom_base_url":
                invalidate_model_cache("ollama")

        # Persist via centralized config module (survives restarts). The
        # in-memory runtime_config above is already set either way, so this
        # session keeps working even on a save failure — but the panel is
        # told, since otherwise the setting silently reverts on next restart
        # with no visible sign anything went wrong.
        persisted = True
        try:
            from . import nova_config
            persisted = await hass.async_add_executor_job(nova_config.set, key, value)
        except Exception as exc:
            _LOGGER.debug("Config persist note: %s", exc)
            persisted = False
        if not persisted:
            _LOGGER.warning("Nova panel: %s = %s applied for this session but "
                            "FAILED to persist to disk — it will revert on restart",
                            key, str(value)[:80])

        # sleep_override needs its expiry computed too (a plain nova_config.set
        # above would otherwise leave the override permanently inert — see
        # sleep_detection.set_override, which is what actually schedules the
        # revert to Auto at the next quiet-hours end).
        if key == "sleep_override":
            try:
                from . import nova_config
                quiet_end = await hass.async_add_executor_job(
                    nova_config.get, "observer_quiet_end", "07:00")
                await hass.async_add_executor_job(
                    sleep_detection.set_override, value, quiet_end)
            except Exception as exc:
                _LOGGER.warning("sleep_override apply failed: %s", exc)

        if key in ("security_alarm_entity", "lockdown_auto_on_arm",
                   "intrusion_requires_confinement"):
            from . import cognitive_core
            await cognitive_core.apply_runtime_config(key, value)

        # Classifier/reasoning provider or model changed while Observer is
        # already running — refresh those two tier providers live, the same
        # way vision/camera-reasoning already apply on their very next
        # analysis (Phase 3, v7.108.0). A no-op if Observer isn't running or
        # this key isn't one of the four that matter.
        if key in ("classifier_provider", "classifier_model",
                   "reasoning_provider", "reasoning_model"):
            try:
                from . import observer as observer_mod
                if observer_mod.is_running():
                    await observer_mod.refresh_tier_providers(hass, {key: value})
            except Exception as exc:
                _LOGGER.debug("Observer tier refresh note: %s", exc)

        connection.send_result(msg["id"], {"key": key, "value": value, "persisted": persisted})
    except Exception as exc:
        _LOGGER.warning("ws_update_config failed: %s", exc)
        connection.send_error(msg["id"], "update_failed", safe_error_message(exc))


def _get_memory_stats() -> dict:
    """Return memory system stats for the panel."""
    try:
        from .memory import get_memory_stats
        return get_memory_stats()
    except Exception:
        return {"backend": "unavailable", "total_memories": 0}


def _available_labels(hass: HomeAssistant) -> list:
    """[{id, name}] of Home Assistant labels, for the exclusion label picker.
    Empty list if the label registry isn't available on this HA version."""
    try:
        from homeassistant.helpers import label_registry as _lr
        reg = _lr.async_get(hass)
        out = []
        for lbl in reg.labels.values():
            out.append({"id": lbl.label_id, "name": lbl.name})
        out.sort(key=lambda x: (x.get("name") or "").lower())
        return out
    except Exception:
        return []


def _get_runtime_json(hass: HomeAssistant, entry, key: str, default):
    """Read a JSON-encoded value from runtime_config → nova_config → entry options."""
    import json as _json
    # 1. Live NovaRuntime.runtime_config (fastest)
    if entry is not None:
        rc = _live_runtime_config(entry)
        raw = rc.get(key)
        if raw is not None:
            if isinstance(raw, (dict, list)):
                return raw
            try:
                return _json.loads(raw)
            except Exception:
                pass

    # 2. Persistent config file (survives restarts)
    try:
        from . import nova_config
        val = nova_config.get(key)
        if val is not None:
            if isinstance(val, (dict, list)):
                return val
            try:
                return _json.loads(val)
            except Exception:
                return val
    except Exception:
        pass

    # 3. Entry options (bootstrap defaults)
    if entry is not None:
        raw = _entry_opt(entry, key, None)
        if raw is not None:
            if isinstance(raw, (dict, list)):
                return raw
            try:
                return _json.loads(raw)
            except Exception:
                pass

    return default


def _get_runtime_str(hass: HomeAssistant, entry, key: str, default: str) -> str:
    """Read a plain string from runtime_config → nova_config → entry options."""
    # 1. Live NovaRuntime.runtime_config
    if entry is not None:
        rc = _live_runtime_config(entry)
        raw = rc.get(key)
        if raw is not None:
            return str(raw)

    # 2. Persistent config file
    try:
        from . import nova_config
        val = nova_config.get(key)
        if val is not None:
            return str(val)
    except Exception:
        pass

    # 3. Entry options
    if entry is not None:
        raw = _entry_opt(entry, key, None)
        if raw is not None:
            return str(raw)

    return default# ─── Memory search WebSocket command ─────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/search_memory",
    vol.Required("query"): str,
    vol.Optional("k", default=5): int,
})
@websocket_api.async_response
async def ws_search_memory(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Search long-term memory for relevant past conversations."""
    try:
        from .memory import search_memory
        results = await hass.async_add_executor_job(
            lambda: search_memory(msg["query"], k=msg["k"])
        )
        connection.send_result(msg["id"], {"results": results})
    except Exception as exc:
        _LOGGER.warning("ws_search_memory failed: %s", exc)
        connection.send_error(msg["id"], "search_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_debug_log",
})
@websocket_api.async_response
async def ws_get_debug_log(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return Nova internal debug log entries, with entities named. The
    persisted log file and the diagnostics export keep the entity_ids."""
    connection.send_result(msg["id"], {"entries": _named_log_entries(
        list(_DEBUG_LOG), _entity_names(hass))})


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_calibration",
})
@websocket_api.async_response
async def ws_get_calibration(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Confidence calibration + interruption-budget health for the dashboard."""
    try:
        from . import decision_record
        payload = {
            "calibration": decision_record.calibration(),
            "interruption_budget": decision_record.interruption_budget(),
            "stats": decision_record.stats(),
            "suggestion": decision_record.outcome_rate("suggestion"),
            "anticipation": decision_record.outcome_rate(
                "anticipation", None, None, True),
        }
        try:
            from . import adaptive_awareness
            payload["adaptive_awareness"] = adaptive_awareness.status()
        except Exception:
            pass
        try:
            from .automation import patterns as pattern_analyzer
            payload["suggestion_threshold"] = {
                "base": round(pattern_analyzer.CONFIDENCE_THRESHOLD, 3),
                "effective": round(pattern_analyzer._effective_threshold(), 3),
                "learned_delta": round(pattern_analyzer._learned_threshold_delta(), 3),
            }
        except Exception:
            pass
        connection.send_result(msg["id"], payload)
    except Exception as exc:
        connection.send_result(msg["id"], {
            "calibration": {"n": 0}, "interruption_budget": {"judged": 0},
            "error": safe_error_message(exc, where="get_calibration", log=True),
        })


# ─── Decision Record browser (Phase 1: decision explanations + feedback) ────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_decisions",
    vol.Optional("kind"): str,
    vol.Optional("only_unjudged", default=False): bool,
    vol.Optional("limit", default=50): int,
    vol.Optional("cursor_ts"): vol.Coerce(float),
    vol.Optional("cursor_id"): int,
})
@websocket_api.async_response
async def ws_list_decisions(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Bounded, cursor-paginated Decision Record browser for the Logs tab.
    Summary rows only (no observation/interpretation/evidence) — full detail
    is a separate nova/get_decision call."""
    try:
        from . import decision_record
        limit = max(1, min(int(msg.get("limit", 50)), 200))
        result = await hass.async_add_executor_job(
            lambda: decision_record.page(
                limit=limit,
                kind=msg.get("kind"),
                only_unjudged=bool(msg.get("only_unjudged", False)),
                cursor_ts=msg.get("cursor_ts"),
                cursor_id=msg.get("cursor_id"),
            )
        )
        names = _entity_names(hass)
        connection.send_result(msg["id"], {
            "decisions": [_named_decision(d, names) for d in result["items"]],
            "next_cursor": result["next_cursor"],
        })
    except Exception as exc:
        _LOGGER.exception("ws_list_decisions failed: %s", exc)
        connection.send_error(msg["id"], "list_decisions_failed", safe_error_message(exc))


_DECISION_FIELD_MAX_CHARS = 500


def _bound_decision_strings(obj):
    """Recursively cap every string at _DECISION_FIELD_MAX_CHARS. The
    observation/interpretation/evidence blobs can carry free text (a calendar
    event title, a routine description) with no length limit enforced at
    write time (decision_record._js() has none) — this bounds it before it
    ever reaches the panel. Never raises."""
    try:
        if isinstance(obj, str):
            return obj if len(obj) <= _DECISION_FIELD_MAX_CHARS else (
                obj[:_DECISION_FIELD_MAX_CHARS] + "…")
        if isinstance(obj, dict):
            return {k: _bound_decision_strings(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_bound_decision_strings(v) for v in obj]
        return obj
    except Exception:
        return obj


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_decision",
    vol.Required("decision_id"): int,
})
@websocket_api.async_response
async def ws_get_decision(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Full detail for one Decision Record — the drawer behind nova/list_decisions.
    Defence in depth (today's writers put nothing sensitive here — verified):
    redacted the same way the config-entry diagnostics dump already is, then
    string-bounded, before this ever reaches the panel."""
    try:
        from . import decision_record
        from .diagnostics import _redact
        rec = await hass.async_add_executor_job(decision_record.get, msg["decision_id"])
        if rec is None:
            connection.send_error(msg["id"], "not_found", "decision not found")
            return
        connection.send_result(
            msg["id"], {"decision": _bound_decision_strings(
                _named_decision(_redact(rec), _entity_names(hass)))})
    except Exception as exc:
        _LOGGER.exception("ws_get_decision failed: %s", exc)
        connection.send_error(msg["id"], "get_decision_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/set_decision_outcome",
    vol.Required("decision_id"): int,
    vol.Required("verdict"): vol.In(["good", "unnecessary", "wrong"]),
})
@websocket_api.async_response
async def ws_set_decision_outcome(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Record Helpful/Unnecessary/Wrong feedback on a Decision Record.
    Set-once: an already-judged record reports "already_judged", not a silent
    no-op, so the panel can tell the two apart from "not_found"."""
    try:
        from . import decision_record
        status = await hass.async_add_executor_job(
            decision_record.set_outcome_checked, msg["decision_id"], msg["verdict"], "panel")
        connection.send_result(msg["id"], {"status": status})
    except Exception as exc:
        _LOGGER.exception("ws_set_decision_outcome failed: %s", exc)
        connection.send_error(msg["id"], "set_decision_outcome_failed", safe_error_message(exc))


# ─── Decision Lab (Phase 4: current-policy replay) ──────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/replay_decision",
    vol.Required("decision_id"): int,
})
@websocket_api.async_response
async def ws_replay_decision(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Read-only Decision Lab replay — current policy only, never a
    historical reconstruction (see replay.replay_one's docstring). No writes,
    no service calls, no LLM/cloud calls: replay_one takes a plain record
    dict, not hass, so it has no way to perform any of those even by
    accident."""
    try:
        from . import decision_record, replay
        rec = await hass.async_add_executor_job(decision_record.get, msg["decision_id"])
        if rec is None:
            connection.send_error(msg["id"], "not_found", "decision not found")
            return
        connection.send_result(msg["id"], replay.replay_one(rec))
    except Exception as exc:
        _LOGGER.exception("ws_replay_decision failed: %s", exc)
        connection.send_error(msg["id"], "replay_decision_failed", safe_error_message(exc))


def _name_diagnostic(res, names: dict) -> None:
    """Add a `name` beside each entity_id in the analysis diagnostic, in
    place. Never raises."""
    try:
        dg = res.get("diagnostic") if isinstance(res, dict) else None
        if not isinstance(dg, dict):
            return
        from .cognitive.naming import name_for
        for key in ("candidates", "top_sources"):
            for row in dg.get(key) or []:
                if isinstance(row, dict) and row.get("entity_id"):
                    row["name"] = name_for(row["entity_id"], names)
    except Exception:
        pass


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/run_analysis",
})
@websocket_api.async_response
async def ws_run_analysis(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Force a pattern-analysis pass now (manual 'Analyze Now')."""
    try:
        from . import cognitive_core
        res = await cognitive_core.run_analysis_now(hass)
        _name_diagnostic(res, _entity_names(hass))
        connection.send_result(msg["id"], res)
    except Exception as exc:
        connection.send_result(msg["id"], {"ran": False, "error": safe_error_message(exc, where="analyze_now", log=True)})


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_cognitive_status",
})
@websocket_api.async_response
async def ws_get_cognitive_status(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return Nova cognitive core status for the dashboard."""
    try:
        from . import cognitive_core
        status = cognitive_core.status()
        connection.send_result(msg["id"], status)
    except Exception as exc:
        connection.send_result(msg["id"], {
            "running": False,
            "error": safe_error_message(exc, where="get_cognitive_status", log=True),
            "learning": {},
        })


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/suggestion_action",
    vol.Required("suggestion_id"): int,
    vol.Required("action"): vol.In(["approve", "dismiss", "restore"]),
})
@websocket_api.async_response
async def ws_suggestion_action(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Approve or dismiss a pattern-engine automation suggestion. Approval now
    installs the automation into HA, not just flags it (v6.52.0). "restore"
    brings back a suggestion the AI review rejected (v7.126.0)."""
    try:
        from .automation.installation import install_approved_suggestion
        from .automation.patterns import get_analyzer
        analyzer = get_analyzer()
        sid = int(msg["suggestion_id"])
        if msg["action"] == "approve":
            res = await install_approved_suggestion(
                hass, sid,
                requested_by_user_id=getattr(connection.user, "id", None),
                requested_by_name=getattr(connection.user, "name", None),
            )
            if res.get("installed"):
                nova_log("LEARN", f"Suggestion #{sid} approved & installed "
                                    f"as '{res.get('alias')}'")
            elif res.get("ok"):
                nova_log("LEARN", f"Suggestion #{sid} approved "
                                    f"(advisory — {res.get('reason')})")
            connection.send_result(msg["id"], {
                "ok": bool(res.get("ok")),
                "installed": bool(res.get("installed")),
                "reason": res.get("reason"),
                "alias": res.get("alias"),
            })
            return
        if msg["action"] == "restore":
            ok = await hass.async_add_executor_job(analyzer.restore_suggestion, sid)
            nova_log("LEARN", f"Suggestion #{sid} restored after AI review (ok={ok})")
            connection.send_result(msg["id"], {"ok": bool(ok)})
            return
        ok = await hass.async_add_executor_job(analyzer.dismiss_suggestion, sid)
        nova_log("LEARN", f"Suggestion #{sid} dismissed (ok={ok})")
        connection.send_result(msg["id"], {"ok": bool(ok)})
    except Exception as exc:
        _LOGGER.exception("ws_suggestion_action failed: %s", exc)
        connection.send_error(msg["id"], "suggestion_action_failed", safe_error_message(exc))


# ─── Automation probation (Phase 3) ──────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_automation_inventory",
})
@websocket_api.async_response
async def ws_list_automation_inventory(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return Nova's cached, read-only Home Assistant automation inventory.

    Raw automation configuration never leaves the backend. This endpoint
    reads the startup/reload cache, so opening Suggestions adds no inventory
    scan to Home Assistant's normal dashboard polling.
    """
    try:
        from .automation.inventory import get_inventory
        inventory = get_inventory(hass)
        connection.send_result(msg["id"], {
            "available": inventory is not None,
            "refreshed_at": inventory.refreshed_at if inventory else None,
            "automations": inventory.public_items() if inventory else [],
        })
    except Exception as exc:
        _LOGGER.exception("ws_list_automation_inventory failed: %s", exc)
        connection.send_error(
            msg["id"], "list_automation_inventory_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_automation_trials",
})
@websocket_api.async_response
async def ws_list_automation_trials(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Installed-automation run counts + manual feedback for the Suggestions
    tab. Installation only means the suggestion was accepted — this reports
    what's actually observed running, never a claim that it works."""
    try:
        from .automation import trials as automation_trials
        trials = await hass.async_add_executor_job(automation_trials.list_trials)
        connection.send_result(msg["id"], {"trials": trials})
    except Exception as exc:
        _LOGGER.exception("ws_list_automation_trials failed: %s", exc)
        connection.send_error(msg["id"], "list_automation_trials_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/automation_trial_feedback",
    vol.Required("trial_id"): int,
    vol.Required("verdict"): vol.In(["working", "needs_adjustment"]),
})
@websocket_api.async_response
async def ws_automation_trial_feedback(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Manual Working / Needs adjustment feedback — never inferred, only ever
    what the household actually reports."""
    try:
        from .automation import trials as automation_trials
        ok = await hass.async_add_executor_job(
            automation_trials.set_manual_outcome, msg["trial_id"], msg["verdict"])
        connection.send_result(msg["id"], {"ok": bool(ok)})
    except Exception as exc:
        _LOGGER.exception("ws_automation_trial_feedback failed: %s", exc)
        connection.send_error(msg["id"], "automation_trial_feedback_failed", safe_error_message(exc))


_SNAP_LOG_TS: dict[str, float] = {}


def _snap_log(entity_id: str, msg: str) -> None:
    """CAMERA-log a snapshot failure at most once per 5 min per entity —
    the panel polls this tier every 6s, and a broken camera shouldn't
    flood the log while still leaving a visible trail."""
    now = time.time()
    if now - _SNAP_LOG_TS.get(entity_id, 0) < 300:
        return
    _SNAP_LOG_TS[entity_id] = now
    nova_log("CAMERA", f"{entity_id} snapshot: {msg}")


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_snapshot",
    vol.Required("entity_id"): str,
})
@websocket_api.async_response
async def ws_camera_snapshot(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """A frame via Nova's camera backend registry (Nest event media,
    Frigate snapshot, stream-wake). The panel's last-resort tile source for
    cameras where /api/camera_proxy* fails — WebRTC-only Nest cams have no
    MJPEG stream and can't produce stills while idle, so both proxy tiers
    404 and the tile went permanently blank (v6.46.0)."""
    import base64
    entity_id = str(msg["entity_id"])
    try:
        if not hass.states.get(entity_id) or not entity_id.startswith("camera."):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import camera as cam
        img = await cam._get_best_image(hass, entity_id)
        if not img:
            _snap_log(entity_id,
                      "no frame — backend and proxy paths all empty "
                      "(Nest: check integration is loaded and events enabled)")
            connection.send_result(msg["id"], {"image": None})
            return
        img = cam._downscale_jpeg(img, 960)
        connection.send_result(
            msg["id"], {"image": base64.b64encode(img).decode()})
    except Exception as exc:
        _LOGGER.debug("camera_snapshot failed for %s: %s", entity_id, exc)
        _snap_log(entity_id, f"error — {exc}")
        connection.send_error(msg["id"], "snapshot_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/rename_camera",
    vol.Required("entity_id"): str,
    vol.Required("name"): vol.Any(str, None),
})
@websocket_api.async_response
async def ws_rename_camera(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Set a Nova-only display name for a camera (v6.48.0) — chips, strip,
    and pickers use it; HA's entity name is untouched. Blank name reverts."""
    entity_id = str(msg["entity_id"])
    new_name = msg.get("name")
    try:
        if not entity_id.startswith("camera.") or not hass.states.get(entity_id):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import nova_config
        from .camera import merge_camera_name
        names = merge_camera_name(_get_camera_names(), entity_id, new_name)
        await hass.async_add_executor_job(nova_config.set, "camera_names", names)
        shown = names.get(entity_id)
        nova_log("CONFIG", f"camera {entity_id} "
                             + (f"renamed to '{shown}'" if shown else "name reverted")
                             + " (Nova only)")
        connection.send_result(msg["id"], {
            "ok": True, "camera_names": names, "cameras": _get_cameras(hass),
        })
    except Exception as exc:
        _LOGGER.exception("rename_camera failed: %s", exc)
        connection.send_error(msg["id"], "rename_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/biometrics",
    vol.Required("action"): vol.In(["status", "enable", "disable"]),
})
@websocket_api.async_response
async def ws_biometrics(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Wellbeing/biometric context for the panel (v6.63.0) — discover connected
    wearable entities and toggle the feature. Context only; never medical."""
    try:
        from . import biometrics, nova_config
        if msg["action"] == "enable":
            nova_config.set("biometrics_enabled", True)
            nova_log("BIO", "biometric context enabled")
        elif msg["action"] == "disable":
            nova_config.set("biometrics_enabled", False)
            nova_log("BIO", "biometric context disabled")
        enabled = bool(nova_config.get("biometrics_enabled", False))
        found = await hass.async_add_executor_job(biometrics.discover, hass)
        # flatten discovered entities for the panel
        entities = []
        for kind, ents in found.items():
            for e in ents:
                entities.append({"kind": kind, **e})
        connection.send_result(msg["id"], {
            "enabled": enabled,
            "found": len(entities),
            "entities": entities,
        })
    except Exception as exc:
        _LOGGER.exception("ws_biometrics failed: %s", exc)
        connection.send_error(msg["id"], "biometrics_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/energy",
    vol.Required("action"): vol.In(["status", "set_agency"]),
    vol.Optional("agency"): str,
})
@websocket_api.async_response
async def ws_energy(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Energy management for the panel (v6.62.0): report the current power
    picture + advice, or set the agency level (advisory/opt_in/autonomous)."""
    try:
        from . import energy, nova_config
        if msg["action"] == "set_agency":
            level = str(msg.get("agency", "") or "").lower()
            if level not in (energy.AGENCY_ADVISORY, energy.AGENCY_OPT_IN,
                             energy.AGENCY_AUTONOMOUS):
                connection.send_error(msg["id"], "bad_agency",
                                      f"unknown agency '{level}'")
                return
            nova_config.set("energy_agency", level)
            nova_log("ENERGY", f"agency → {level}")
            res = await hass.async_add_executor_job(energy.power_status, hass)
            connection.send_result(msg["id"], res)
        else:
            res = await hass.async_add_executor_job(energy.power_status, hass)
            connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_energy failed: %s", exc)
        connection.send_error(msg["id"], "energy_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/solar",
    vol.Required("action"): vol.In(["status"]),
})
@websocket_api.async_response
async def ws_solar(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Solar/battery/grid picture for the panel (v7.91.0), read straight from
    Home Assistant's own Energy dashboard config. Pure read, no mutating
    action — left open like other read-only panel data (get_panel_data,
    get_activity_log, etc.), not admin-gated."""
    try:
        from . import solar
        res = await solar.solar_status(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_solar failed: %s", exc)
        connection.send_error(msg["id"], "solar_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/hazard",
    vol.Required("action"): vol.In(["status", "scan"]),
})
@websocket_api.async_response
async def ws_hazard(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Multi-hazard monitor for the panel (v6.71.0): 'status' returns config +
    the resolved monitoring center; 'scan' runs a live read-only check of all
    feeds so the user can confirm it's wired to their area (does not alert or
    consume dedup)."""
    try:
        from . import hazard_monitor
        if msg["action"] == "scan":
            res = await hazard_monitor.scan_now(hass)
        else:
            res = await hazard_monitor.status(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_hazard failed: %s", exc)
        connection.send_error(msg["id"], "hazard_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/mode",
    vol.Required("action"): vol.In(["status", "set"]),
    vol.Optional("mode"): str,
    vol.Optional("reason"): str,
})
@websocket_api.async_response
async def ws_mode(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Operational mode control for the panel (Directive Layer, v6.61.0):
    report the active mode + available modes, or switch modes. Modes shift the
    whole behavior profile (proactivity, tone, event scope) but never disable
    safety."""
    try:
        from . import modes
        if msg["action"] == "set":
            res = await hass.async_add_executor_job(
                modes.set_mode, msg.get("mode", ""), msg.get("reason", ""))
            if res.get("ok"):
                nova_log("MODE", f"mode → {res['mode']} (panel)")
                try:
                    from . import mode_scene
                    await mode_scene.apply_mode_entry(
                        hass, res["mode"], source="panel",
                        requested_by_user_id=getattr(connection.user, "id", None),
                        requested_by_name=getattr(connection.user, "name", None),
                    )
                except Exception:
                    pass
            connection.send_result(msg["id"], {**res, **modes.mode_info()})
        else:
            connection.send_result(msg["id"], modes.mode_info())
    except Exception as exc:
        _LOGGER.exception("ws_mode failed: %s", exc)
        connection.send_error(msg["id"], "mode_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/intrusion",
    vol.Required("action"): vol.In(["status", "dismiss", "acknowledge",
                                    "log", "label", "learning"]),
    vol.Optional("reason"): str,
    vol.Optional("event_id"): str,
    vol.Optional("label"): vol.Any(str, None),
    vol.Optional("limit"): int,
})
@websocket_api.async_response
async def ws_intrusion(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Intrusion snapshot + call-off for the panel (v6.68.0): report the last
    snapshot and call-off state, dismiss an active alert as a false alarm, or
    acknowledge it (hold auto-escalation without cancelling) (v6.69.0).

    Snapshot images are never served over HTTP (v7.102.0) — they live in a
    private directory and are read back and base64-inlined here, so they can
    only ever reach the panel through this already-@require_admin command.
    """
    try:
        from . import intrusion

        async def _status_with_image() -> dict:
            s = intrusion.status()
            snap = s.get("last_snapshot")
            if snap and snap.get("path"):
                snap["image_b64"] = await intrusion.get_snapshot_b64(hass, snap["path"])
            return s

        if msg["action"] == "dismiss":
            res = intrusion.dismiss_intrusion(msg.get("reason", "panel"))
            try:
                from . import cognitive_core
                core = getattr(cognitive_core, "_CORE", None)
                if core and getattr(core, "safety_mgr", None):
                    core.safety_mgr._investigation = None
            except Exception:
                pass
            nova_log("SAFETY", "Intrusion called off from panel (false alarm)")
            connection.send_result(msg["id"], {**res, **await _status_with_image()})
        elif msg["action"] == "acknowledge":
            res = intrusion.acknowledge(msg.get("reason", "panel"))
            nova_log("SAFETY", "Intrusion acknowledged from panel (holding escalation)")
            connection.send_result(msg["id"], {**res, **await _status_with_image()})
        elif msg["action"] == "log":
            # Reviewable event history with snapshots (v6.76.0)
            events = intrusion.get_log(msg.get("limit", 50))
            for ev in events:
                p = ev.get("snapshot_path")
                if p:
                    ev["image_b64"] = await intrusion.get_snapshot_b64(hass, p)
            connection.send_result(msg["id"], {
                "events": events,
                "learning": intrusion.learning_summary(),
            })
        elif msg["action"] == "label":
            # The training signal: mark an event real or false
            res = intrusion.label_event(msg.get("event_id", ""),
                                        msg.get("label"))
            nova_log("SAFETY", f"Intrusion event labelled: "
                                 f"{msg.get('event_id')} = {msg.get('label')}")
            connection.send_result(msg["id"], {
                **res, "learning": intrusion.learning_summary()})
        elif msg["action"] == "learning":
            connection.send_result(msg["id"], intrusion.learning_summary())
        else:
            connection.send_result(msg["id"], await _status_with_image())
    except Exception as exc:
        _LOGGER.exception("ws_intrusion failed: %s", exc)
        connection.send_error(msg["id"], "intrusion_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/voice_confirm_test",
})
@websocket_api.async_response
async def ws_voice_confirm_test(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Fire the assist_satellite.announce test (v6.67.0) — the 'does it come out
    the Nest?' check that decides whether native voice-confirm works. The user
    listens and picks native vs gated based on whether they heard it."""
    try:
        from . import voice_confirm
        res = await voice_confirm.announce_test(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_voice_confirm_test failed: %s", exc)
        connection.send_error(msg["id"], "test_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/diagnostics",
})
@websocket_api.async_response
async def ws_diagnostics(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Core dependency health for the panel (v6.60.0): LLM, embeddings, TTS,
    STT. Returns per-service status so the user can see at a glance what's up
    and get a specific reason for anything down."""
    try:
        from . import diagnostics
        res = await diagnostics.run_service_health(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_diagnostics failed: %s", exc)
        connection.send_error(msg["id"], "diagnostics_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_setup_health",
})
@websocket_api.async_response
async def ws_get_setup_health(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Setup Doctor (Phase 2): read-only configuration health — stale entity
    references, room speakers, camera overrides, notification service,
    Assist pipeline wiring, person entities, required integrations, and
    persistence — plus the existing service-health checks folded in unchanged.
    Admin-only: the response can contain entity_ids. Never makes a
    configuration change, offers no automatic fix, and never creates a
    Repair issue — this is a panel-only report."""
    try:
        from . import setup_health
        res = await setup_health.run_setup_health(hass)
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_get_setup_health failed: %s", exc)
        connection.send_error(msg["id"], "get_setup_health_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/say_hello",
})
@websocket_api.async_response
async def ws_say_hello(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Welcome card "Say hello" test: sends the fixed text "Hello" through
    Nova's own conversation agent and returns {ok, reply|error}. Admin-only;
    takes no text from the caller, so it can't drive device actions."""
    from . import welcome
    res = await welcome.async_say_hello(hass, connection.context(msg))
    connection.send_result(msg["id"], res)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_provider_activity",
    vol.Optional("days", default=7): int,
})
@websocket_api.async_response
async def ws_get_provider_activity(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Bounded daily LLM provider/model/role activity (Phase 5) — call
    counts, success/failure, average tokens and latency. Never prompts,
    responses, tool arguments, images, or credentials; admin-only regardless,
    since it still reveals which providers/models are configured."""
    try:
        from . import provider_activity
        days = max(1, min(int(msg.get("days", 7)), 90))
        db_path = provider_activity.db_path_for(hass)
        result = await hass.async_add_executor_job(
            lambda: provider_activity.list_days(days, db_path=db_path))
        connection.send_result(msg["id"], {"days": result})
    except Exception as exc:
        _LOGGER.exception("ws_get_provider_activity failed: %s", exc)
        connection.send_error(msg["id"], "get_provider_activity_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_spoken_history",
})
@websocket_api.async_response
async def ws_get_spoken_history(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Spoken History (v7.104.0): the last things Nova actually sent to a
    speaker — welcome-home, reminders, alerts, briefings, manual tests,
    confirmed Assist replies, and repeats. Text only, newest first, bounded
    to the last 100. Admin-only: this reveals what was actually said in the
    house, unlike the panel's other pure-read commands."""
    try:
        from . import spoken_history
        entries = await hass.async_add_executor_job(spoken_history.list_recent)
        connection.send_result(msg["id"], {"entries": entries})
    except Exception as exc:
        _LOGGER.exception("ws_get_spoken_history failed: %s", exc)
        connection.send_error(msg["id"], "get_spoken_history_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/list_actions",
    vol.Optional("limit", default=20): int,
    vol.Optional("cursor_ts"): vol.Coerce(float),
    vol.Optional("cursor_request_id"): str,
})
@websocket_api.async_response
async def ws_list_actions(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Action Audit Log: actions Nova genuinely attempted or performed on
    the user's behalf — device controls, bulk controls, scene/script/
    automation execution, safety routines, suggested-automation
    installation, notifications. Request-level, keyset-paginated: one page
    is a set of COMPLETE request groups (never a request split across two
    pages), newest first. Admin-only: reveals what Nova actually did in
    the house, same tier as Spoken History.

    Each returned request carries its own aggregate `status` (success/
    partial/failed/blocked/awaiting) separate from each target row's own
    approval_result/execution_result, and — when a matching Spoken History
    entry exists for that request_id — a `spoken_history_id` reference
    (never the spoken text itself; the panel fetches that separately via
    the existing nova/get_spoken_history command if it wants to show it)."""
    try:
        from . import action_log
        limit = max(1, min(int(msg.get("limit", 20)), 100))
        result = await hass.async_add_executor_job(
            lambda: action_log.page_requests(
                limit=limit,
                cursor_ts=msg.get("cursor_ts"),
                cursor_request_id=msg.get("cursor_request_id"),
            )
        )
        request_ids = [r["request_id"] for r in result["requests"]]
        spoken_links: dict[str, int] = {}
        if request_ids:
            try:
                from . import spoken_history
                spoken_links = await hass.async_add_executor_job(
                    spoken_history.find_by_action_request_id, request_ids
                )
            except Exception:
                pass  # a Spoken History lookup failure must never break the actions list
        for r in result["requests"]:
            r["spoken_history_id"] = spoken_links.get(r["request_id"])
        connection.send_result(msg["id"], result)
    except Exception as exc:
        _LOGGER.exception("ws_list_actions failed: %s", exc)
        connection.send_error(msg["id"], "list_actions_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/repeat_spoken",
    vol.Required("spoken_id"): int,
})
@websocket_api.async_response
async def ws_repeat_spoken(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Re-announce one Spoken History entry (the panel's Repeat button).
    `spoken_id` (never `id` — that field is reserved for websocket message
    correlation) names which row to repeat.

    Sends to the original speaker(s) if they are still available;
    otherwise falls back to Nova's configured default speakers
    (the same broadcast_target() the manual TTS test already uses).
    Delivery and recording both happen inside async_announce — this
    handler never calls spoken_history.record itself, so a repeat is
    recorded exactly once, by the same single recorder as every other
    path that goes through async_announce."""
    try:
        from . import spoken_history, nova_config
        from .tts_helper import async_announce, resolve_tts_entity
        from .audio_routing import broadcast_target

        row = await hass.async_add_executor_job(spoken_history.get, msg["spoken_id"])
        if row is None:
            connection.send_error(msg["id"], "not_found", "No spoken history entry with that id")
            return

        speakers = [
            s for s in row["speakers"]
            if (st := hass.states.get(s)) is not None
            and st.state not in ("unavailable", "unknown")
        ]
        entry = _get_entry(hass)
        cfg = await hass.async_add_executor_job(nova_config.effective_config, entry)
        if not speakers:
            speakers = broadcast_target(
                hass,
                broadcast_group=(cfg.get("broadcast_group") or None),
                announcement_speakers=cfg.get("announcement_speakers"),
            )
        if not speakers:
            connection.send_error(msg["id"], "no_speaker", "No speaker available to repeat through")
            return

        tts_entity = resolve_tts_entity(hass, cfg.get("tts_engine", "auto"))
        if not tts_entity:
            connection.send_error(msg["id"], "no_tts_entity", "No TTS entity available")
            return

        ok = await async_announce(
            hass, row["text"], tts_entity, speakers,
            context="repeat", repeat_of_id=row["id"],
        )
        connection.send_result(msg["id"], {"ok": ok, "spoken": row["text"] if ok else ""})
    except Exception as exc:
        _LOGGER.exception("ws_repeat_spoken failed: %s", exc)
        connection.send_error(msg["id"], "repeat_spoken_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/semantic_search",
    vol.Required("action"): vol.In(["status", "enable", "disable", "test"]),
})
@websocket_api.async_response
async def ws_semantic_search(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Local semantic search control (v6.57.0). Instead of ChromaDB (whose
    onnxruntime dep has no Python-3.14 wheel), Nova embeds via the Ollama
    server it already uses and stores vectors in its own SQLite DB. This
    reports status, toggles it on/off, and runs a live embedding health check."""
    action = msg["action"]
    try:
        from . import embeddings, nova_config
        if action == "enable":
            await hass.async_add_executor_job(nova_config.set, "semantic_search", True)
            await hass.async_add_executor_job(embeddings.init_store)
            res = await embeddings.probe(hass)
            res["enabled"] = True
            if res.get("ok"):
                nova_log("AGENT", f"semantic search enabled "
                                    f"(Ollama {res.get('model')}, dim "
                                    f"{res.get('dim')}) — re-ingest to embed docs")
            else:
                nova_log("AGENT", f"semantic search enabled but Ollama not "
                                    f"ready: {res.get('error')}")
            connection.send_result(msg["id"], res)
        elif action == "disable":
            await hass.async_add_executor_job(nova_config.set, "semantic_search", False)
            nova_log("AGENT", "semantic search disabled — keyword (FTS) active")
            connection.send_result(msg["id"], {"enabled": False, "ok": True})
        elif action == "test":
            res = await embeddings.probe(hass)
            connection.send_result(msg["id"], res)
        else:  # status
            enabled = bool(nova_config.get("semantic_search", False))
            base = embeddings._ollama_base()
            vcount = await hass.async_add_executor_job(embeddings.vector_count)
            connection.send_result(msg["id"], {
                "enabled": enabled,
                "ollama_configured": bool(base),
                "base": base or "",
                "model": embeddings._model(),
                "vector_count": vcount,
            })
    except Exception as exc:
        _LOGGER.exception("ws_semantic_search failed: %s", exc)
        connection.send_error(msg["id"], "semantic_search_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/documents",
    vol.Required("action"): vol.In(["status", "ingest", "search", "upload",
                                    "scan_watch", "delete"]),
    vol.Optional("query"): str,
    vol.Optional("filename"): str,
    vol.Optional("content"): str,        # base64 for upload
})
@websocket_api.async_response
async def ws_documents(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Document library control for the panel. status / ingest (rescan) /
    search / upload (base64 file → save + ingest) / scan_watch (pull new files
    from configured watch folders) / delete (remove a source). The retrieval
    Nova uses in conversation is the search_documents agent tool; this exposes
    the same store to the UI (v6.55.0; upload+watch v6.59.0)."""
    action = msg["action"]
    try:
        from . import documents
        if action == "status":
            res = await hass.async_add_executor_job(documents.library_status)
        elif action == "ingest":
            res = await documents.ingest_directory_async(hass)
            extra = (f", {res.get('embedded_chunks',0)} embedded"
                     if res.get("semantic") else "")
            nova_log("AGENT", f"documents ingested via panel: "
                                f"{res.get('files_ingested',0)} files, "
                                f"{res.get('total_chunks',0)} chunks{extra}")
        elif action == "upload":
            res = await documents.save_and_ingest_upload(
                hass, msg.get("filename", ""), msg.get("content", ""))
            if res.get("ok"):
                nova_log("AGENT", f"document uploaded: {res.get('filename')} "
                                    f"({res.get('chunks',0)} chunks)")
        elif action == "scan_watch":
            res = await documents.scan_watch_folders(hass)
            if res.get("new_files"):
                nova_log("AGENT", f"watch-folder scan: {res['new_files']} "
                                    f"new document(s) ingested")
        elif action == "delete":
            res = await hass.async_add_executor_job(
                documents.delete_source, msg.get("filename", ""))
            if res.get("ok"):
                nova_log("AGENT", f"document removed: {msg.get('filename')}")
        else:  # search
            hits = await documents.search_documents_async(
                hass, msg.get("query", ""), 5)
            res = {"results": hits}
        connection.send_result(msg["id"], res)
    except Exception as exc:
        _LOGGER.exception("ws_documents failed: %s", exc)
        connection.send_error(msg["id"], "documents_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/mmwave_overview",
})
@websocket_api.async_response
async def ws_mmwave_overview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Per-area mmWave presence overview for the residence tab (v6.53.0).

    Distinct from the generic area grid: this reports *only* rooms with
    presence/occupancy/motion sensors, and for each the live sensor breakdown —
    how many sensors, how many currently detecting, the freshest detection age —
    so the panel can show genuine mmWave coverage and live state rather than a
    binary 'occupied' flag that could come from a door contact."""
    import time as _t
    try:
        from . import audio_routing
        rooms = []
        total_sensors = 0
        rooms_detecting = 0
        for aid in _all_areas_with_anything(hass):
            sensors = audio_routing.presence_entities_in_area(hass, aid)
            if not sensors:
                continue
            detecting = 0
            freshest = None            # seconds since most-recent change
            sensor_rows = []
            for eid in sensors:
                st = hass.states.get(eid)
                if st is None:
                    continue
                on = st.state == "on"
                if on:
                    detecting += 1
                age = None
                try:
                    age = _t.time() - st.last_changed.timestamp()
                    if freshest is None or age < freshest:
                        freshest = age
                except Exception:
                    pass
                sensor_rows.append({
                    "entity_id": eid,
                    "name": (st.attributes.get("friendly_name") or eid),
                    "detecting": on,
                    "age": _format_duration(age),
                })
            total_sensors += len(sensor_rows)
            if detecting:
                rooms_detecting += 1
            rooms.append({
                "area_id": aid,
                "name": _area_name(hass, aid),
                "outdoor": _is_outdoor_area(hass, aid),
                "sensor_count": len(sensor_rows),
                "detecting_count": detecting,
                "state": ("detecting" if detecting else "clear"),
                "freshest": _format_duration(freshest),
                "sensors": sensor_rows,
            })
        # Detecting rooms first, then most-recently-active, then name
        rooms.sort(key=lambda r: (r["detecting_count"] == 0, r["name"].lower()))
        connection.send_result(msg["id"], {
            "rooms": rooms,
            "summary": {
                "rooms_with_mmwave": len(rooms),
                "rooms_detecting": rooms_detecting,
                "total_sensors": total_sensors,
            },
        })
    except Exception as exc:
        _LOGGER.exception("mmwave_overview failed: %s", exc)
        connection.send_error(msg["id"], "mmwave_overview_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_location",
    vol.Required("entity_id"): str,
    vol.Required("mode"): vol.In(["auto", "indoor", "outdoor"]),
})
@websocket_api.async_response
async def ws_camera_location(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Designate a camera indoor/outdoor (or auto = heuristics), v6.49.0.
    Pins the exact entity id into the existing indoor_entities /
    outdoor_entities lists — outdoor.py's most-authoritative layer — so the
    designation immediately governs the intrusion investigator, the
    notable-outdoor-event filter, and the motion scan alike."""
    entity_id = str(msg["entity_id"])
    mode = str(msg["mode"])
    try:
        if not entity_id.startswith("camera.") or not hass.states.get(entity_id):
            connection.send_error(msg["id"], "unknown_camera", entity_id)
            return
        from . import nova_config, outdoor
        new_in, new_out = outdoor.set_entity_location(
            outdoor._cfg_list("indoor_entities"),
            outdoor._cfg_list("outdoor_entities"),
            entity_id, mode,
        )
        await hass.async_add_executor_job(
            nova_config.set_many,
            {"indoor_entities": new_in, "outdoor_entities": new_out},
        )
        nova_log("CONFIG", f"camera {entity_id} location → {mode.upper()}"
                             + ("" if mode != "auto" else " (heuristics)"))
        connection.send_result(msg["id"], {
            "ok": True, "cameras": _get_cameras(hass),
        })
    except Exception as exc:
        _LOGGER.exception("camera_location failed: %s", exc)
        connection.send_error(msg["id"], "camera_location_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/camera_diagnostics",
    vol.Optional("entity_id"): str,
})
@websocket_api.async_response
async def ws_camera_diagnostics(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """End-to-end probe of one camera's frame sources, plus a platform
    summary of every camera entity HA has — answers both "why is this tile
    blank" and "do my Nest entities even exist" in one call (v6.46.2)."""
    import asyncio as _aio
    try:
        summary = []
        platforms: dict[str, int] = {}
        try:
            from homeassistant.helpers import entity_registry as er
            reg = er.async_get(hass)
        except Exception:
            reg = None
        for st in hass.states.async_all("camera"):
            plat = None
            if reg:
                try:
                    e = reg.async_get(st.entity_id)
                    plat = e.platform if e else None
                except Exception:
                    plat = None
            platforms[plat or "?"] = platforms.get(plat or "?", 0) + 1
            summary.append({"entity_id": st.entity_id,
                            "state": st.state, "platform": plat})

        probe = None
        entity_id = msg.get("entity_id")
        if entity_id:
            from . import camera as cam
            try:
                probe = await _aio.wait_for(
                    cam.probe_camera(hass, str(entity_id)), timeout=30)
            except _aio.TimeoutError:
                probe = {"entity_id": entity_id, "tiers": [],
                         "verdict": "probe timed out after 30s "
                                    "(stream wake hanging?)"}
            nova_log("CAMERA", f"diag {entity_id}: {probe.get('verdict', '?')}")

        connection.send_result(msg["id"], {
            "summary": summary, "platforms": platforms, "probe": probe,
        })
    except Exception as exc:
        _LOGGER.exception("camera_diagnostics failed: %s", exc)
        connection.send_error(msg["id"], "camera_diag_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_area_sparklines",
})
@websocket_api.async_response
async def ws_get_area_sparklines(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Recent temp/humidity history per area, for dashboard sparklines.
    Deliberately a separate, slow-polled command — recorder history queries
    are heavier than the rest of the panel payload and shouldn't ride along
    on the fast real-time-triggered refresh."""
    try:
        entity_map: dict[str, dict[str, Optional[str]]] = {}
        for aid in _all_areas_with_anything(hass):
            t_eid, h_eid = _area_temp_humidity_entities(hass, aid)
            if t_eid or h_eid:
                entity_map[aid] = {"temp": t_eid, "humidity": h_eid}
        sparklines = await _get_area_sparklines(hass, entity_map)
        connection.send_result(msg["id"], {"sparklines": sparklines})
    except Exception as exc:
        _LOGGER.exception("get_area_sparklines failed: %s", exc)
        connection.send_error(msg["id"], "sparklines_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/goal_action",
    vol.Required("action"): vol.In(["cancel", "delete", "create"]),
    vol.Optional("goal_id"): int,
    vol.Optional("title"): str,
    vol.Optional("outcome"): str,
    vol.Optional("interval_min"): vol.Coerce(float),
})
@websocket_api.async_response
async def ws_goal_action(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Manage goals from the panel: create a new one, cancel an active one
    (keeps it in history), or delete one entirely (tidies the list). Goals also
    close themselves via the headless runner as before."""
    try:
        from . import goals
        action = msg["action"]
        if action == "create":
            outcome = str(msg.get("outcome", "") or "").strip()
            if not outcome:
                connection.send_error(msg["id"], "empty_outcome",
                                      "a goal needs an outcome to work toward")
                return
            title = str(msg.get("title", "") or "").strip()
            kwargs = {}
            if msg.get("interval_min") is not None:
                kwargs["check_interval_min"] = float(msg["interval_min"])
            res = await hass.async_add_executor_job(
                lambda: goals.create(title, outcome, **kwargs))
            if res.get("error"):
                connection.send_error(msg["id"], "create_failed", res["error"])
                return
            nova_log("LEARN", f"Goal created from panel: {title or outcome[:50]}")
            connection.send_result(msg["id"], {"ok": True, "goal": res,
                                               "goals": _get_goals()})
            return

        # cancel / delete both need a goal_id
        gid = msg.get("goal_id")
        if gid is None:
            connection.send_error(msg["id"], "missing_goal_id",
                                  f"{action} needs a goal_id")
            return
        gid = int(gid)
        if action == "delete":
            ok = await hass.async_add_executor_job(goals.delete, gid)
            nova_log("LEARN", f"Goal #{gid} deleted from panel (ok={ok})")
        else:  # cancel
            ok = await hass.async_add_executor_job(goals.cancel, gid)
            nova_log("LEARN", f"Goal #{gid} cancelled from panel (ok={ok})")
        connection.send_result(msg["id"], {"ok": bool(ok), "goals": _get_goals()})
    except Exception as exc:
        _LOGGER.exception("ws_goal_action failed: %s", exc)
        connection.send_error(msg["id"], "goal_action_failed", safe_error_message(exc))


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_person_routines",
})
@websocket_api.async_response
async def ws_get_person_routines(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Per-person learned routines, grouped by person, for the Memory panel."""
    try:
        routines = await hass.async_add_executor_job(
            _get_person_routines, _entity_names(hass))
        connection.send_result(msg["id"], {"routines": routines})
    except Exception as exc:
        _LOGGER.exception("get_person_routines failed: %s", exc)
        connection.send_error(msg["id"], "person_routines_failed", safe_error_message(exc))
