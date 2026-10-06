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
    _get_output_mutes,
    _get_sentinel_rules,
    _get_suggestions,
)
# The AI settings commands live in ws_ai.py. async_register registers the
# handlers by name; invalidate_model_cache is public and stays importable
# from here. nova/update_config uses the panel AI key checks below.
from .ws_ai import (  # noqa: F401
    HIDDEN_PASSWORD_MESSAGE,
    PANEL_AI_ENDPOINT_KEYS,
    PANEL_AI_VALUE_KEYS,
    invalidate_model_cache,
    panel_ai_value,
    shows_hidden_password,
    ws_apply_ai_config,
    ws_delete_credential,
    ws_get_credential_status,
    ws_list_models,
    ws_set_credential,
    ws_test_provider_endpoint,
)
# The Faces tab commands live in ws_faces.py.
from .ws_faces import ws_add_resident, ws_list_faces, ws_remove_resident
# The knowledge, decision and automation commands live in ws_knowledge.py,
# ws_decisions.py and ws_automation.py. async_register registers the handlers
# by name.
from .ws_automation import (
    ws_automation_trial_feedback,
    ws_get_person_routines,
    ws_goal_action,
    ws_list_automation_inventory,
    ws_list_automation_trials,
    ws_suggestion_action,
)
from .ws_decisions import (
    ws_get_calibration,
    ws_get_cognitive_status,
    ws_get_decision,
    ws_list_decisions,
    ws_replay_decision,
    ws_root_cause,
    ws_run_analysis,
    ws_set_decision_outcome,
)
from .ws_knowledge import (
    ws_add_knowledge,
    ws_clear_scene_memory,
    ws_edit_pending_fact,
    ws_edit_relation,
    ws_forget_knowledge,
    ws_get_knowledge,
    ws_list_relations,
    ws_pending_fact_action,
    ws_relation_action,
    ws_search_memory,
    ws_set_lockdown,
)
# The camera commands live in ws_cameras.py. async_register registers them by name.
from .ws_cameras import (
    ws_camera_diagnostics,
    ws_camera_location,
    ws_camera_snapshot,
    ws_compute_camera_coverage,
    ws_mmwave_overview,
    ws_rename_camera,
)
# The mode, intrusion, hazard, energy, solar and biometrics
# commands live in ws_modes.py. async_register registers them by name.
from .ws_modes import (
    ws_biometrics,
    ws_energy,
    ws_hazard,
    ws_intrusion,
    ws_mode,
    ws_solar,
)
# The voice and history commands live in ws_voice.py. async_register registers them by name.
from .ws_voice import (
    ws_get_spoken_history,
    ws_list_actions,
    ws_repeat_spoken,
    ws_say_hello,
    ws_voice_confirm_test,
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
        websocket_api.async_register_command(hass, ws_list_faces)
        websocket_api.async_register_command(hass, ws_add_resident)
        websocket_api.async_register_command(hass, ws_remove_resident)
        websocket_api.async_register_command(hass, ws_list_relations)
        websocket_api.async_register_command(hass, ws_relation_action)
        websocket_api.async_register_command(hass, ws_edit_relation)
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
                "announce_notify_only": bool(_runtime_opt(hass, entry, "announce_notify_only", False)),
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
                "departure_osrm_url": _masked_url(_runtime_opt(hass, entry, "departure_osrm_url", "")),
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
                "llm_base_url": _masked_url(_runtime_opt(hass, entry, "llm_base_url", "")),
                "ollama_base_url": _masked_url(_runtime_opt(hass, entry, "ollama_base_url", "")),
                "custom_base_url": _masked_url(_runtime_opt(hass, entry, "custom_base_url", "")),
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
                "face_stand_down": _runtime_opt(
                    hass, entry, "face_stand_down", False) is True,
                "alarm_panels": _get_alarm_panels(hass),
                "onboarding": _get_onboarding_state(hass, entry, current_notify),
                "sentinel_rules": _get_sentinel_rules(),
                "disabled_sentinel_rules": _get_disabled_rules(hass, entry),
                "observer_stats": _get_observer_stats(),
                "output_mutes": _get_output_mutes(),
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
                "searxng_url":          _masked_url(_runtime_opt(hass, entry, "searxng_url", "")),
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
                # 8.8.0: region-aware (off by default for an Irish home
                # unless saved); a saved value is shown as saved.
                "hazard_quakes_on":       _hazard_flag(hass, "hazard_quakes_on"),
                "hazard_weather_on":      _hazard_flag(hass, "hazard_weather_on"),
                "hazard_disasters_on":    _hazard_flag(hass, "hazard_disasters_on"),
                # Weather warnings (8.8.0)
                "hazard_met_eireann_on":  _hazard_flag(hass, "hazard_met_eireann_on"),
                "hazard_cap_on":          bool(_runtime_opt(hass, entry, "hazard_cap_on", False)),
                "hazard_counties":        _get_runtime_json(hass, entry, "hazard_counties", []),
                "hazard_push_level":      str(_runtime_opt(hass, entry, "hazard_push_level", "yellow") or "yellow"),
                "hazard_speak_level":     str(_runtime_opt(hass, entry, "hazard_speak_level", "orange") or "orange"),
                "hazard_cap_url":         _masked_url(_runtime_opt(hass, entry, "hazard_cap_url", "")),
                "hazard_cap_area_codes":  _get_runtime_json(hass, entry, "hazard_cap_area_codes", []),
                "hazard_cap_area_names":  _get_runtime_json(hass, entry, "hazard_cap_area_names", []),
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
                "output_language": str(_runtime_opt(hass, entry, "output_language", "") or ""),
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


def _hazard_flag(hass: HomeAssistant, key: str) -> bool:
    """A hazard source switch as the monitor sees it: saved, else the region
    default (8.8.0). Falls back to the pre-8.8.0 default on any error."""
    try:
        from . import hazard_monitor
        return hazard_monitor.effective_flag(hass, key)
    except Exception:
        return key != "hazard_met_eireann_on"


def _masked_url(value) -> str:
    """A saved URL for display, as a string, with any user:pass@ (and any
    credential-like query value) masked by the diagnostics scrubber
    (8.7.23). Only what is sent to the panel changes; the saved value is
    untouched. Never raises."""
    text = str(value or "")
    if not text:
        return text
    try:
        from .diagnostics import _scrub_text
        return _scrub_text(text)
    except Exception:
        from .safe_errors import REDACTED
        return REDACTED if "@" in text else text


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
    "output_language",             # "" or "auto" follows Home Assistant, else a language code
    "announcements_enabled",
    "announce_notify_only",        # bool: proactive announcements go to the phone, not the speakers (critical still speaks)
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
    "face_stand_down",             # bool, off by default: a recognised resident can stop a NEW intrusion investigation opening
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
    # Hazard Monitor weather warnings (8.8.0)
    "hazard_met_eireann_on",      # bool: Met Éireann warnings (on by default for an Irish home)
    "hazard_cap_on",              # bool: a custom CAP feed (hazard_cap_url)
    "hazard_counties",            # JSON list: Met Éireann county codes (empty = nearest to home)
    "hazard_push_level",          # str: yellow | orange | red, lowest level sent to the phone
    "hazard_speak_level",         # str: yellow | orange | red, lowest level also spoken
    "hazard_cap_url",             # str: https CAP document, or Atom/RSS index of them
    "hazard_cap_area_codes",      # JSON list: CAP geocode values that mean "here"
    "hazard_cap_area_names",      # JSON list: CAP areaDesc names that mean "here"
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

    # The panel shows saved URLs with any password masked. A value still
    # holding the mask is that shown text sent back, so it is refused and the
    # saved value is kept (8.7.24). A plain URL, or a real new one with a
    # password, is not affected.
    if shows_hidden_password(value):
        connection.send_error(msg["id"], "invalid_value", HIDDEN_PASSWORD_MESSAGE)
        return

    # AI endpoints are written only by nova/apply_ai_config, which normalises
    # them, checks the destination and tests the connection (8.7.23). The
    # panel never sends them here. Already saved values still load.
    if key in PANEL_AI_ENDPOINT_KEYS:
        connection.send_error(
            msg["id"], "invalid_key",
            f"Key '{key}' is set with nova/apply_ai_config (Settings, AI Models), "
            "not nova/update_config",
        )
        return

    # Provider and model keys get nova/apply_ai_config's own checks.
    if key in PANEL_AI_VALUE_KEYS:
        from .safe_errors import NovaValidationError
        try:
            value = panel_ai_value(key, value)
        except NovaValidationError as exc:
            connection.send_error(msg["id"], "invalid_value", safe_error_message(exc))
            return

    from . import safety_config
    if not safety_config.valid_panel_value(key, value):
        connection.send_error(
            msg["id"], "invalid_value",
            safety_config.invalid_panel_value_message(key),
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
                   "intrusion_requires_confinement", "face_stand_down"):
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
    persisted log file and the diagnostics export keep the entity_ids.

    Never raises (8.7.23). If naming fails, the entries go back with their
    entity_ids, and the failure goes to the Home Assistant log. The pinned
    contract has no error code for this command, so it answers with the
    same {"entries": [...]} shape rather than a new error."""
    entries = list(_DEBUG_LOG)
    try:
        names = _entity_names(hass)
    except Exception as exc:
        safe_error_message(exc, where="get_debug_log naming", log=True)
        names = {}
    connection.send_result(msg["id"], {"entries": _named_log_entries(entries, names)})


# ─── Decision Record browser (Phase 1: decision explanations + feedback) ────


# ─── Decision Lab (Phase 4: current-policy replay) ──────────────────────────


# ─── Automation probation (Phase 3) ──────────────────────────────────────────


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
                "base": _masked_url(base),
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


