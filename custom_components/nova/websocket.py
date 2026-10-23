"""Nova's panel websocket layer.

async_register puts every nova/* command on Home Assistant's websocket API.
Most handlers live in the ws_*.py modules and are imported here by name; this
module keeps the config readers they share (reached through ws_bridge) and
the commands that are still its own: the panel data poll, the activity and
debug logs, the generic config write, the health reports, provider activity,
semantic search, the document library, area sparklines and the appliance
monitor reload.
"""
from __future__ import annotations

import json
import logging
import math
import time
from functools import partial
from pathlib import Path
from typing import Any, Optional

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import audio_routing, optional_features, settings_schema, sleep_detection
from .const import (
    CONF_BEDROOM_AREAS, CONF_BROADCAST_GROUP, CONF_GEMINI_API_KEY, CONF_GROUND_FLOOR_AREAS,
    CONF_NOTIFY_SERVICE, CONF_NOTIFY_SERVICES, CONF_OBSERVER_ENABLED, CONF_OBSERVER_QUIET_END,
    CONF_OBSERVER_QUIET_START, DEFAULT_OBSERVER_QUIET_END, DEFAULT_OBSERVER_QUIET_START, DOMAIN,
)
from .safe_errors import safe_error_message

# The debug log is ws_log's. Other modules and tests still reach these names
# through here, so they are imported under their own names. _LOG_FILE stays
# out on purpose: tests reassign it, and a copy held here would go stale.
from .ws_log import (  # noqa: F401
    _DEBUG_LOG, _LOG_QUEUE, _ensure_writer, nova_log, recent_conversation_log, recent_debug_log,
)

# Panel helpers. Handlers look them up in this module's namespace at call
# time, which is what lets a test (or a slow tile) be patched here.
from .ws_area_helpers import (
    _all_areas_with_anything, _area_capabilities, _area_light_state, _area_live_readings,
    _area_name, _area_temp_humidity_entities, _dominant_area, _format_duration,
    _get_all_people, _get_camera_names, _get_camera_overrides, _get_cameras, _get_cast_devices,
    _get_onboarding_state, _get_satellites, _get_speaker_assignable_areas, _is_outdoor_area,
    _satellite_count, area_scan,
)
from .ws_panel_stats import (
    _entity_names, _format_uptime, _get_activity_counts, _get_alarm_panels,
    _get_announcements_today,
    _get_appliance_status, _get_area_sparklines, _get_doorbell_training,
    _get_filtered_suggestions, _get_goals, _get_intrusion_status, _get_knowledge_stats,
    _get_lockdown_status, _get_observer_stats, _get_output_mutes, _get_sentinel_rules,
    _get_stt_entities, _get_suggestions, _get_voice_id_status,
)

# The AI settings commands, plus the checks nova/update_config borrows from
# them. invalidate_model_cache is public and stays importable from here.
from .ws_ai import (  # noqa: F401
    HIDDEN_PASSWORD_MESSAGE, PANEL_AI_ENDPOINT_KEYS, PANEL_AI_VALUE_KEYS,
    invalidate_model_cache, panel_ai_value, shows_hidden_password,
    ws_apply_ai_config, ws_delete_credential, ws_get_credential_status, ws_list_models,
    ws_set_credential, ws_test_provider_endpoint,
)

# Every other command handler, by the module that owns it.
from .ws_automation import (
    ws_automation_trial_feedback, ws_get_person_routines, ws_goal_action,
    ws_list_automation_inventory, ws_list_automation_trials, ws_suggestion_action,
)
from .ws_cameras import (
    ws_camera_diagnostics, ws_camera_location, ws_camera_snapshot, ws_compute_camera_coverage,
    ws_mmwave_overview, ws_rename_camera,
)
from .ws_chat import ws_chat, ws_chat_new
from .ws_convenience import ws_convenience_choice, ws_convenience_status
from .ws_decisions import (
    ws_get_calibration, ws_get_cognitive_status, ws_get_decision, ws_list_decisions,
    ws_root_cause, ws_run_analysis, ws_set_decision_outcome,
)
from .ws_faces import ws_add_resident, ws_list_faces, ws_remove_resident
from .ws_knowledge import (
    ws_add_knowledge, ws_clear_scene_memory, ws_edit_pending_fact, ws_edit_relation,
    ws_forget_knowledge, ws_get_knowledge, ws_list_relations, ws_pending_fact_action,
    ws_relation_action, ws_search_memory, ws_set_lockdown,
)
from .ws_modes import (
    ws_biometrics, ws_energy, ws_energy_flow, ws_energy_outlook, ws_get_reminders, ws_hazard,
    ws_intrusion, ws_mode, ws_solar, ws_subscribe_presentation,
)
from .ws_voice import (
    ws_get_spoken_history, ws_list_actions, ws_repeat_spoken, ws_say_hello, ws_voice_confirm_test,
)
from .ws_voice_id import (
    ws_voice_id_delete, ws_voice_id_profiles, ws_voice_id_record, ws_voice_id_record_cancel,
    ws_voice_id_record_status, ws_voice_id_token_get, ws_voice_id_token_set,
)
from .ws_voice_phrases import (
    ws_voice_phrases_delete, ws_voice_phrases_list, ws_voice_phrases_save,
    ws_voice_phrases_set_enabled, ws_voice_phrases_test,
)

_LOGGER = logging.getLogger(__name__)

# When this module was imported; the panel's uptime counts from here.
_STARTUP_TS: float = time.time()

# The only keys nova/update_config will write. Each one's kind and default
# live in settings_schema.SETTINGS (8.42.0).
PANEL_WRITABLE_KEYS = set(settings_schema.PANEL_KEYS)

# What get_panel_data shows when a reading is missing.
_DASH = "—"

# The hazard location override is saved as one pair.
_HAZARD_PAIR = ("hazard_lat", "hazard_lon")
# Settings a running observer re-reads when one of them changes.
_OBSERVER_TIER_KEYS = ("classifier_provider", "classifier_model",
                       "reasoning_provider", "reasoning_model")


def _manifest_version() -> str:
    """The version in manifest.json, or "?" when it cannot be read. Read
    once at import, so the panel shows the running version rather than one
    a cached panel script remembers."""
    try:
        manifest = json.loads((Path(__file__).parent / "manifest.json").read_text())
        return manifest.get("version", "?")
    except Exception:
        return "?"


_INTEGRATION_VERSION = _manifest_version()


@callback
def async_register(hass: HomeAssistant) -> None:
    """Put every Nova panel command on the websocket API. A failure part way
    through is only noted at debug level (a second call after a reload finds
    the commands already there)."""
    try:
        # This module's own commands.
        websocket_api.async_register_command(hass, ws_documents)
        websocket_api.async_register_command(hass, ws_diagnostics)
        websocket_api.async_register_command(hass, ws_get_activity_log)
        websocket_api.async_register_command(hass, ws_get_area_sparklines)
        websocket_api.async_register_command(hass, ws_get_debug_log)
        websocket_api.async_register_command(hass, ws_get_panel_data)
        websocket_api.async_register_command(hass, ws_get_provider_activity)
        websocket_api.async_register_command(hass, ws_get_setup_health)
        websocket_api.async_register_command(hass, ws_reload_appliances)
        websocket_api.async_register_command(hass, ws_semantic_search)
        websocket_api.async_register_command(hass, ws_update_config)
        # AI models and credentials.
        websocket_api.async_register_command(hass, ws_apply_ai_config)
        websocket_api.async_register_command(hass, ws_delete_credential)
        websocket_api.async_register_command(hass, ws_get_credential_status)
        websocket_api.async_register_command(hass, ws_list_models)
        websocket_api.async_register_command(hass, ws_set_credential)
        websocket_api.async_register_command(hass, ws_test_provider_endpoint)
        # Suggestions, goals, routines and automation trials.
        websocket_api.async_register_command(hass, ws_automation_trial_feedback)
        websocket_api.async_register_command(hass, ws_get_person_routines)
        websocket_api.async_register_command(hass, ws_goal_action)
        websocket_api.async_register_command(hass, ws_list_automation_inventory)
        websocket_api.async_register_command(hass, ws_list_automation_trials)
        websocket_api.async_register_command(hass, ws_suggestion_action)
        # Cameras.
        websocket_api.async_register_command(hass, ws_camera_diagnostics)
        websocket_api.async_register_command(hass, ws_camera_location)
        websocket_api.async_register_command(hass, ws_camera_snapshot)
        websocket_api.async_register_command(hass, ws_compute_camera_coverage)
        websocket_api.async_register_command(hass, ws_mmwave_overview)
        websocket_api.async_register_command(hass, ws_rename_camera)
        # Chat and the convenience path.
        websocket_api.async_register_command(hass, ws_chat)
        websocket_api.async_register_command(hass, ws_chat_new)
        websocket_api.async_register_command(hass, ws_convenience_choice)
        websocket_api.async_register_command(hass, ws_convenience_status)
        # Decisions and cognition.
        websocket_api.async_register_command(hass, ws_get_calibration)
        websocket_api.async_register_command(hass, ws_get_cognitive_status)
        websocket_api.async_register_command(hass, ws_get_decision)
        websocket_api.async_register_command(hass, ws_list_decisions)
        websocket_api.async_register_command(hass, ws_root_cause)
        websocket_api.async_register_command(hass, ws_run_analysis)
        websocket_api.async_register_command(hass, ws_set_decision_outcome)
        # Faces.
        websocket_api.async_register_command(hass, ws_list_faces)
        websocket_api.async_register_command(hass, ws_add_resident)
        websocket_api.async_register_command(hass, ws_remove_resident)
        # Knowledge, relations and lockdown.
        websocket_api.async_register_command(hass, ws_add_knowledge)
        websocket_api.async_register_command(hass, ws_clear_scene_memory)
        websocket_api.async_register_command(hass, ws_edit_pending_fact)
        websocket_api.async_register_command(hass, ws_forget_knowledge)
        websocket_api.async_register_command(hass, ws_get_knowledge)
        websocket_api.async_register_command(hass, ws_pending_fact_action)
        websocket_api.async_register_command(hass, ws_search_memory)
        websocket_api.async_register_command(hass, ws_set_lockdown)
        websocket_api.async_register_command(hass, ws_list_relations)
        websocket_api.async_register_command(hass, ws_relation_action)
        websocket_api.async_register_command(hass, ws_edit_relation)
        # Modes, safety and energy.
        websocket_api.async_register_command(hass, ws_biometrics)
        websocket_api.async_register_command(hass, ws_energy)
        websocket_api.async_register_command(hass, ws_energy_flow)
        websocket_api.async_register_command(hass, ws_energy_outlook)
        websocket_api.async_register_command(hass, ws_hazard)
        websocket_api.async_register_command(hass, ws_intrusion)
        websocket_api.async_register_command(hass, ws_mode)
        websocket_api.async_register_command(hass, ws_solar)
        websocket_api.async_register_command(hass, ws_subscribe_presentation)
        websocket_api.async_register_command(hass, ws_get_reminders)
        # Speech history and voice confirmation.
        websocket_api.async_register_command(hass, ws_get_spoken_history)
        websocket_api.async_register_command(hass, ws_list_actions)
        websocket_api.async_register_command(hass, ws_repeat_spoken)
        websocket_api.async_register_command(hass, ws_say_hello)
        websocket_api.async_register_command(hass, ws_voice_confirm_test)
        # Voice phrases.
        websocket_api.async_register_command(hass, ws_voice_phrases_delete)
        websocket_api.async_register_command(hass, ws_voice_phrases_list)
        websocket_api.async_register_command(hass, ws_voice_phrases_save)
        websocket_api.async_register_command(hass, ws_voice_phrases_set_enabled)
        websocket_api.async_register_command(hass, ws_voice_phrases_test)
        # Nova Voice ID.
        websocket_api.async_register_command(hass, ws_voice_id_delete)
        websocket_api.async_register_command(hass, ws_voice_id_profiles)
        websocket_api.async_register_command(hass, ws_voice_id_record)
        websocket_api.async_register_command(hass, ws_voice_id_record_cancel)
        websocket_api.async_register_command(hass, ws_voice_id_record_status)
        websocket_api.async_register_command(hass, ws_voice_id_token_get)
        websocket_api.async_register_command(hass, ws_voice_id_token_set)
    except Exception as exc:
        _LOGGER.debug("WS command register note: %s", exc)


# ─── Config readers ──────────────────────────────────────────────────────────
#
# Precedence everywhere: the loaded entry's live NovaRuntime.runtime_config,
# then config.json, then the entry's options, then its data, then the default.
# hass.data is never consulted, which keeps the compatibility bridge out.

def _get_entry(hass: HomeAssistant):
    """Nova's config entry (the first one), or None."""
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


def _live_runtime_config(entry) -> dict:
    """The live runtime_config dict, for reads on the event loop. {} without
    an entry or before it has loaded; a loaded entry with no runtime raises
    NovaRuntimeUnavailable rather than showing invented defaults."""
    if entry is None:
        return {}
    from .runtime import lifecycle_runtime_config
    return lifecycle_runtime_config(entry)


def _executor_runtime_config(entry) -> dict:
    """A private copy of runtime_config for one executor job. Same rules as
    _live_runtime_config; take it just before the job and do not keep it."""
    if entry is None:
        return {}
    from .runtime import runtime_config_snapshot
    return runtime_config_snapshot(entry)


def _entry_opt(entry, key: str, default=None):
    """Every layer below runtime_config (config.json, options, data)."""
    from . import nova_config
    return nova_config.runtime_get(None, entry, key, default)


def _runtime_opt(hass: HomeAssistant, entry, key: str, default=None):
    """The live runtime value, unless it is None or blank, else _entry_opt."""
    live = _live_runtime_config(entry).get(key)
    if live not in (None, ""):
        return live
    return _entry_opt(entry, key, default)


def _int_opt(hass: HomeAssistant, entry, key: str, default: int) -> int:
    """_runtime_opt as an int. A stored 0 is kept; junk gives the default."""
    try:
        return int(_runtime_opt(hass, entry, key, default))
    except (TypeError, ValueError):
        return default


_UNREADABLE = object()


def _from_json(raw, keep_bad_text: bool = False):
    """raw when it is already a dict or list, else raw parsed as JSON. Text
    that does not parse is _UNREADABLE, or raw itself with keep_bad_text."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw if keep_bad_text else _UNREADABLE


def _get_runtime_json(hass: HomeAssistant, entry, key: str, default):
    """A JSON valued setting, decoded. A layer whose value does not parse is
    skipped, config.json included."""
    if entry is not None:
        live = _live_runtime_config(entry).get(key)
        if live is not None:
            value = _from_json(live)
            if value is not _UNREADABLE:
                return value
    try:
        from . import nova_config
        saved = nova_config.get(key)
        if saved is not None:
            value = _from_json(saved)
            if value is not _UNREADABLE:
                return value
    except Exception:
        pass
    if entry is not None:
        bootstrap = _entry_opt(entry, key, None)
        if bootstrap is not None:
            value = _from_json(bootstrap)
            if value is not _UNREADABLE:
                return value
    return default


def _movie_dim_pct(hass: HomeAssistant, entry) -> int:
    """The saved movie dim level; a saved 0 stays 0, only a missing or blank
    value reads as the default 15."""
    value = _runtime_opt(hass, entry, "movie_dim_pct", 15)
    return 15 if value in (None, "") else int(value)


def _get_disabled_rules(hass: HomeAssistant, entry) -> list[str]:
    """The sentinel rule ids the user switched off, as a list."""
    if entry is None:
        return []
    live = _live_runtime_config(entry)
    raw = live.get("disabled_sentinel_rules", _entry_opt(entry, "disabled_sentinel_rules", "[]"))
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


# ─── Panel display helpers ───────────────────────────────────────────────────

def _masked_url(value) -> str:
    """A saved URL as text for the panel, with any user:pass@ or credential
    looking query value hidden by the diagnostics scrubber (8.7.23). The
    saved value itself is untouched. Never raises: without the scrubber, any
    text holding an "@" is hidden whole."""
    text = str(value or "")
    if not text:
        return text
    try:
        from .diagnostics import _scrub_text
        return _scrub_text(text)
    except Exception:
        from .safe_errors import REDACTED
        return text if "@" not in text else REDACTED


def _hazard_flag(hass: HomeAssistant, key: str) -> bool:
    """A hazard source switch as the monitor reads it (saved, else the region
    default, 8.8.0). If the monitor cannot answer, every source but Met
    Éireann reads as on, as before 8.8.0."""
    try:
        from . import hazard_monitor
        return hazard_monitor.effective_flag(hass, key)
    except Exception:
        return key != "hazard_met_eireann_on"


def _hazard_source(hass: HomeAssistant, entry) -> str:
    """The one weather warning source: saved, else derived, else custom."""
    saved = _runtime_opt(hass, entry, "hazard_source", None)
    if saved in ("met_eireann", "us", "custom"):
        return saved
    try:
        from . import hazard_monitor
        return hazard_monitor.effective_source(hass)
    except Exception:
        return "custom"


def _hazard_coordinate(hass: HomeAssistant, entry, key: str, index: int):
    """A saved coordinate override, else Home Assistant's own home one, else ""."""
    saved = _runtime_opt(hass, entry, key, "")
    if saved not in (None, ""):
        return saved
    try:
        from . import hazard_monitor
        home = hazard_monitor.base_home_latlon(hass)
        return home[index] if home else ""
    except Exception:
        return ""


def _get_lockdown_exempt_locks() -> list:
    """The locks lockdown leaves alone right now, sorted. Read from the
    running manager, the same source as the sweep (8.29.0)."""
    try:
        from .core_lockdown_sync import _lockdown_exempt_locks
        return sorted(_lockdown_exempt_locks())
    except Exception:
        return []


def _get_home_doors(hass: HomeAssistant, entry) -> dict:
    """The garage, exit door (8.26.0) and other room (8.27.0) details the
    panel shows. Display only: no safety check reads this. Worked out on
    every load, so a garage added or removed shows straight away."""
    from . import home_doors
    layout = {
        "garage_mode": _runtime_opt(hass, entry, "garage_mode", home_doors.DEFAULT_GARAGE_MODE),
        "door_mapping": _get_runtime_json(hass, entry, "door_mapping", {}),
        "exit_doors": _get_runtime_json(hass, entry, "exit_doors", []),
        "basement_mode": _runtime_opt(hass, entry, "basement_mode", None),
        "has_basement": _runtime_opt(hass, entry, "has_basement", None),
    }
    try:
        from .core_lockdown_sync import _lockdown_exempt_locks
        exempt = _lockdown_exempt_locks()
    except Exception:
        exempt = set()
    try:
        return {
            "garage_mode": home_doors.garage_mode(layout),
            "has_garage": home_doors.has_garage(hass, layout),
            "exit_doors": home_doors.exit_doors(layout),
            "exit_door_status": home_doors.exit_door_status(hass, layout, exempt),
            "exit_door_candidates": home_doors.exit_door_candidates(hass, layout),
            "basement_mode": home_doors.basement_mode(layout),
            "home_features": home_doors.home_features(hass, layout),
        }
    except Exception as exc:
        _LOGGER.debug("home doors read failed: %s", exc)
    return {
        "garage_mode": home_doors.garage_mode(layout),
        "has_garage": False,
        "exit_doors": [],
        "exit_door_status": [],
        "exit_door_candidates": [],
        "basement_mode": home_doors.basement_mode(layout),
        "home_features": {"basement": False, "utility": False},
    }


def _get_host_health_status(hass: HomeAssistant, entry) -> dict:
    """What the Host Health card shows (Phase 10): for each metric its
    candidate sensors and mapping status, plus the last tick's readings.
    Worked out fresh each load; host_health's own alert state only moves
    on its periodic tick."""
    try:
        from . import host_health
        settings = {
            "host_health_enabled": bool(_runtime_opt(hass, entry, "host_health_enabled", False)),
            "host_health_mappings": _get_runtime_json(hass, entry, "host_health_mappings", {}),
            "host_health_thresholds": _get_runtime_json(hass, entry, "host_health_thresholds", {}),
        }
        found = host_health.discover_candidates(hass)
        mapped = host_health.resolve_mappings(hass, settings, found)
        rows = []
        for metric in host_health.METRICS:
            result = mapped[metric.key]
            rows.append({
                "key": metric.key,
                "label": metric.label,
                "recommended": metric.recommended,
                "status": result.status,
                "source": result.source,
                "entity_id": result.entity_id,
                "candidates": [{"entity_id": option.entity_id,
                                "friendly_name": option.friendly_name,
                                "disabled": option.disabled}
                               for option in result.candidates],
            })
        return {"metrics": rows, "snapshot": host_health.snapshot(hass, settings)}
    except Exception as exc:
        return {"error": safe_error_message(exc, where="host health read", log=True)}


def _available_labels(hass: HomeAssistant) -> list:
    """Home Assistant's labels as [{id, name}] by name, for the exclusion
    picker. [] when the label registry is not there."""
    try:
        from homeassistant.helpers import label_registry
        registry = label_registry.async_get(hass)
        found = [{"id": label.label_id, "name": label.name}
                 for label in registry.labels.values()]
        return sorted(found, key=lambda item: (item.get("name") or "").lower())
    except Exception:
        return []


def _get_memory_stats() -> dict:
    """The memory store's counts, or a placeholder when it cannot say."""
    try:
        from .memory import get_memory_stats
        return get_memory_stats()
    except Exception:
        return {"backend": "unavailable", "total_memories": 0}


async def _voice_id_token(hass: HomeAssistant) -> str:
    """The saved Voice ID token, "" when unset or unreadable. The panel only
    learns whether one is set; the value goes out through
    nova/voice_id_token_get alone."""
    try:
        from . import voice_id_store
        return await voice_id_store.async_get_token(hass)
    except Exception:
        return ""


def _get_voice_id_enrol() -> dict:
    """Where a Voice ID recording stands, idle when that cannot be read."""
    try:
        from . import voice_enrol
        return voice_enrol.status()
    except Exception:
        return {"state": "idle"}


def _named_log_entries(entries: list, names: dict) -> list:
    """Copies of the log entries for display, known entity_ids replaced by
    their names. The entries themselves are left alone; with no names, or
    if naming fails, they are returned as they are."""
    if not names:
        return entries
    try:
        from .cognitive.naming import humanize_text
        shown = []
        for item in entries:
            if isinstance(item, dict):
                item = {**item, "msg": humanize_text(item.get("msg", ""), names)}
            shown.append(item)
        return shown
    except Exception:
        return entries


def _chosen_notify_services(hass: HomeAssistant, entry, as_text: bool) -> list:
    """The notify services the user picked. The settings card (as_text)
    sends the single service through str(); the status tile does not."""
    from .notify_targets import configured_notify_services
    single = _runtime_opt(hass, entry, CONF_NOTIFY_SERVICE, "") or ""
    picked = {CONF_NOTIFY_SERVICE: str(single) if as_text else single}
    several = _runtime_opt(hass, entry, CONF_NOTIFY_SERVICES, None)
    if several is not None:
        picked[CONF_NOTIFY_SERVICES] = several
    return configured_notify_services(picked)


def _known_notify_services(hass: HomeAssistant) -> list:
    """Every notify.* service Home Assistant has, for the phone dropdown."""
    known: list = []
    try:
        for service in hass.services.async_services().get("notify", {}):
            known.append(f"notify.{service}")
    except Exception:
        pass
    return known


def _area_rows(hass: HomeAssistant, bedrooms) -> list:
    """One card per area with anything in it: occupied rooms first, then
    bedrooms, then by name."""
    rows = []
    for area_id in _all_areas_with_anything(hass):
        capabilities = _area_capabilities(hass, area_id)
        occupied = audio_routing.is_area_occupied(hass, area_id)
        lit, lights = _area_light_state(hass, area_id)
        live = _area_live_readings(hass, area_id)
        temp_sensor, humidity_sensor = _area_temp_humidity_entities(hass, area_id)
        rows.append({
            "id": area_id,
            "name": _area_name(hass, area_id),
            "caps": capabilities,
            "active": occupied,
            "bedroom": area_id in bedrooms,
            "lights_on": lit,
            "lights_total": lights,
            "temp": live.get("temp"),
            "humidity": live.get("humidity"),
            "temp_entity": temp_sensor,
            "humidity_entity": humidity_sensor,
            "last_motion": _format_duration(live.get("last_motion_seconds")),
        })
    rows.sort(key=lambda row: (not row["active"], not row["bedroom"], row["name"].lower()))
    return rows


def _dominant_card(hass: HomeAssistant) -> dict:
    """The big room card: where people are, or home / away when nowhere."""
    area_id = _dominant_area(hass)
    if not area_id:
        return {
            "area_id": None,
            "name": "AT HOME" if audio_routing.anyone_home(hass) else "AWAY",
            "subtitle": "no presence detected",
            "coord": _DASH, "temp": _DASH, "humidity": _DASH, "lights": _DASH,
            "satellite": _DASH, "last_motion": _DASH,
        }
    live = _area_live_readings(hass, area_id)
    seconds = live.get("last_motion_seconds")
    satellites = audio_routing.satellites_in_area(hass, area_id)
    return {
        "area_id": area_id,
        "name": _area_name(hass, area_id),
        "subtitle": "Occupied" if seconds is None else f"Occupied · {_format_duration(seconds)}",
        "coord": f"#{area_id[:8]}",
        "temp": live.get("temp") or _DASH,
        "humidity": live.get("humidity") or _DASH,
        "lights": live.get("lights") or _DASH,
        "satellite": satellites[0].split(".", 1)[-1][:20] if satellites else _DASH,
        "last_motion": _format_duration(seconds),
    }


def _tile(on: bool, on_state: str) -> dict:
    """A status tile that is live when on and an UNSET warning when not."""
    return {"state": on_state, "level": "live"} if on else {"state": "UNSET", "level": "warn"}


def _satellite_tile(online: int, total: int) -> dict:
    if total <= 0:
        return {"state": "NONE", "level": "off"}
    return {"state": f"{online} / {total}", "level": "live" if online == total else "warn"}


# ─── nova/get_panel_data ─────────────────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_panel_data",
})
@websocket_api.async_response
async def ws_get_panel_data(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any],
) -> None:
    """Everything one panel render needs, in a single round trip."""
    try:
        entry = _get_entry(hass)
        observer_running = False
        if entry is not None:
            from .runtime import observer_status
            observer_running = observer_status(entry)

        bedrooms = _entry_opt(entry, CONF_BEDROOM_AREAS, []) or []
        sleeping, why_asleep = sleep_detection.is_sleeping(
            hass,
            bedroom_area_ids=bedrooms,
            quiet_start=_entry_opt(entry, CONF_OBSERVER_QUIET_START, DEFAULT_OBSERVER_QUIET_START),
            quiet_end=_entry_opt(entry, CONF_OBSERVER_QUIET_END, DEFAULT_OBSERVER_QUIET_END),
        )
        has_gemini = bool(_entry_opt(entry, CONF_GEMINI_API_KEY, ""))
        has_broadcast = bool(_entry_opt(entry, CONF_BROADCAST_GROUP, "") or "")
        has_notify = bool(_chosen_notify_services(hass, entry, as_text=False))
        observer_wanted = bool(_runtime_opt(hass, entry, CONF_OBSERVER_ENABLED, False))
        online, total = _satellite_count(hass)
        # One walk of the entity registry serves every room lookup below.
        with area_scan(hass):
            areas = _area_rows(hass, bedrooms)
            dominant = _dominant_card(hass)
            speaker_areas = _get_speaker_assignable_areas(hass)

        if observer_running:
            observer_tile = {"state": "RUNNING", "level": "live"}
        elif observer_wanted:
            observer_tile = {"state": "READY", "level": "warn"}
        else:
            observer_tile = {"state": "DISABLED", "level": "off"}
        status = {
            "observer": observer_tile,
            "sleep": {"state": "ASLEEP", "level": "warn"} if sleeping
            else {"state": "AWAKE", "level": "live"},
            "gemini": _tile(has_gemini, "READY"),
            "broadcast": _tile(has_broadcast, "ONLINE"),
            "notify": _tile(has_notify, "READY"),
            "satellites": _satellite_tile(online, total),
        }
        uptime = _format_uptime(time.time() - _STARTUP_TS)

        picked_notify = _chosen_notify_services(hass, entry, as_text=True)
        first_notify = picked_notify[0] if picked_notify else ""
        # The doorbell log is read whole on every poll: keep it off the loop.
        doorbell = await hass.async_add_executor_job(_get_doorbell_training, hass)
        # The SQLite reads behind the tiles, together in one executor job.
        announced, knowledge_stats, activity_24h = await hass.async_add_executor_job(
            lambda: (_get_announcements_today(), _get_knowledge_stats(), _get_activity_counts()))
        home = _get_home_doors(hass, entry)
        review_provider = (_runtime_opt(hass, entry, "suggestion_review_provider", "")
                           or _runtime_opt(hass, entry, "llm_provider", "") or "")
        review_model = (_runtime_opt(hass, entry, "suggestion_review_model", "")
                        or _runtime_opt(hass, entry, "model", "") or "")
        historical = _runtime_opt(hass, entry, "camera_historical_awareness",
                                  _runtime_opt(hass, entry, "camera_event_learning", True))

        result = {
            "status": status,
            "version": _INTEGRATION_VERSION,
            "meta": {
                "bedrooms": len(bedrooms),
                "areas_monitored": len(areas),
                "announcements_today": announced,
                "est_cost": _DASH,
                "uptime": uptime,
            },
            "dominant": dominant,
            "areas": areas,
            "sleep_reason": why_asleep if sleeping else None,
            "doorbell_training": doorbell,
            "lockdown": _get_lockdown_status(),
            "intrusion": _get_intrusion_status(),
            "knowledge": knowledge_stats,
            # Both read patterns.db, so both run in the executor.
            "suggestions": await hass.async_add_executor_job(
                _get_suggestions, _entity_names(hass)),
            "suggestions_filtered": await hass.async_add_executor_job(
                _get_filtered_suggestions, _entity_names(hass)),
            "goals": _get_goals(),
            "config": {
                # Every "plain" setting, from its kind and default
                # (settings_schema); the entries below come after and win.
                **settings_schema.payload(
                    lambda key, default: _runtime_opt(hass, entry, key, default),
                    lambda key, default: _get_runtime_json(hass, entry, key, default),
                    _masked_url),
                "announcements_enabled": bool(_runtime_opt(
                    hass, entry, "announcements_enabled",
                    settings_schema.default("announcements_enabled"))),
                "sentinel_enabled": bool(_runtime_opt(hass, entry, "sentinel_enabled", True)),
                "observer_enabled": observer_wanted,
                "camera_historical_awareness": bool(historical),
                "host_health_status": _get_host_health_status(hass, entry),
                "available_labels": _available_labels(hass),
                "sleep_override": sleep_detection.current_override(),
                "ground_floor_areas": _runtime_opt(hass, entry, CONF_GROUND_FLOOR_AREAS, []) or [],
                "movie_dim_pct": _movie_dim_pct(hass, entry),
                "self_hosted_endpoints_migrated": bool(_runtime_opt(
                    hass, entry, "self_hosted_endpoints_migrated", False)),
                "notify_service": first_notify,
                "notify_services": picked_notify,
                "notify_services_available": _known_notify_services(hass),
                "lockdown_exempt_locks": _get_lockdown_exempt_locks(),
                "alarm_panels": _get_alarm_panels(hass),
                "onboarding": _get_onboarding_state(hass, entry, first_notify),
                "sentinel_rules": _get_sentinel_rules(),
                "disabled_sentinel_rules": _get_disabled_rules(hass, entry),
                "observer_stats": _get_observer_stats(activity_24h),
                "output_mutes": _get_output_mutes(),
                "lockdown": _get_lockdown_status(),
                "appliances": _get_appliance_status(),
                "memory_stats": _get_memory_stats(),
                "satellites": _get_satellites(hass),
                "cast_devices": _get_cast_devices(hass),
                "cameras": _get_cameras(hass),
                "camera_overrides": _get_camera_overrides(), "camera_names": _get_camera_names(),
                "speaker_areas": speaker_areas,
                "all_people": _get_all_people(hass),
                "garage_mode": home["garage_mode"],
                "has_garage": home["has_garage"],
                "exit_doors": home["exit_doors"],
                "exit_door_status": home["exit_door_status"],
                "exit_door_candidates": home["exit_door_candidates"],
                "basement_mode": home["basement_mode"],
                "home_features": home["home_features"],
                "suggestion_review_provider": str(review_provider),
                "suggestion_review_model": str(review_model),
                "voice_id_status": _get_voice_id_status(entry),
                "stt_entities": _get_stt_entities(hass, entry),
                "voice_id_token_set": bool(await _voice_id_token(hass)),
                "voice_id_enrol": _get_voice_id_enrol(),
                "hazard_lat": _hazard_coordinate(hass, entry, "hazard_lat", 0),
                "hazard_lon": _hazard_coordinate(hass, entry, "hazard_lon", 1),
                # Region aware since 8.8.0; a saved value is shown as saved.
                "hazard_quakes_on": _hazard_flag(hass, "hazard_quakes_on"),
                "hazard_weather_on": _hazard_flag(hass, "hazard_weather_on"),
                "hazard_disasters_on": _hazard_flag(hass, "hazard_disasters_on"),
                "hazard_met_eireann_on": _hazard_flag(hass, "hazard_met_eireann_on"),
                "hazard_source": _hazard_source(hass, entry),
                "home_context_max_entities": _int_opt(hass, entry, "home_context_max_entities", 15),
                # Which optional features are set up, so the panel can mark
                # the cards of the others (8.43.0).
                "optional_features": optional_features.status(hass),
            },
        }
        connection.send_result(msg["id"], result)
    except Exception as exc:
        _LOGGER.exception("ws_get_panel_data failed: %s", exc)
        connection.send_error(msg["id"], "panel_data_failed", safe_error_message(exc))


# ─── nova/get_activity_log ───────────────────────────────────────────────────

def _clock_time(stamp) -> str:
    """A stored UTC ISO timestamp as local HH:MM; unparseable text gives its
    first five characters."""
    try:
        from datetime import datetime, timezone
        from homeassistant.util import dt as dt_util
        moment = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
        return dt_util.as_local(moment).strftime("%H:%M")
    except Exception:
        return stamp[:5]


@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_activity_log",
    vol.Optional("limit", default=50): int,
    vol.Optional("hours", default=24): int,
})
@websocket_api.async_response
async def ws_get_activity_log(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                              msg: dict[str, Any]) -> None:
    """The latest activity rows, shaped for the panel's feed."""
    try:
        from .database import get_recent_activity
        rows = await hass.async_add_executor_job(
            partial(get_recent_activity, hours=msg["hours"], limit=msg["limit"]))
        from .cognitive.naming import humanize_text
        names = _entity_names(hass)
        entries = []
        for row in rows:
            entity_id = row.get("entity_id", "")
            label = (names.get(entity_id) or entity_id.split(".", 1)[-1])[:20]
            entries.append({
                "ts": _clock_time(row.get("timestamp", "")),
                "urgency": row.get("urgency", "low"),
                "tag": (label or row.get("source", "")).upper(),
                "msg": humanize_text(row.get("message", ""), names),
                "source": row.get("source", "observer"),
            })
        connection.send_result(msg["id"], {"entries": entries})
    except Exception as exc:
        _LOGGER.warning("ws_get_activity_log failed: %s", exc)
        connection.send_error(msg["id"], "activity_log_failed", safe_error_message(exc))


# ─── nova/reload_appliances ──────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/reload_appliances",
})
@websocket_api.async_response
async def ws_reload_appliances(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any],
) -> None:
    """Restart the appliance monitor, so profile edits apply without a
    Home Assistant restart."""
    try:
        from . import appliance_monitor, nova_config
        entry = _get_entry(hass)
        merged: dict = {}
        if entry:
            merged = await hass.async_add_executor_job(
                nova_config.effective_config_with_runtime, entry,
                _executor_runtime_config(entry))
        await appliance_monitor.start(hass, merged, entry=entry)
        connection.send_result(msg["id"], {"ok": True, "appliances": _get_appliance_status()})
    except Exception as exc:
        reason = safe_error_message(exc, where="reload_appliances", log=True)
        connection.send_error(msg["id"], "reload_failed", reason)


# ─── nova/update_config ──────────────────────────────────────────────────────

def _vet_panel_write(key: str, value) -> tuple[Optional[str], str, Any]:
    """Check a panel write before anything is touched. Returns (error code,
    message, value): the code is None when the write may go ahead, and the
    value is the one to store (provider and model names are cleaned)."""
    # Credentials only ever go through nova/set_credential and
    # nova/delete_credential. None is in PANEL_WRITABLE_KEYS, but this check
    # runs first so an allowlist slip can never store, log or show one.
    from . import ha_secrets
    if key in ha_secrets.CREDENTIAL_KEYS:
        return ("invalid_key",
                "Credentials are set via nova/set_credential, not nova/update_config", value)
    if key not in PANEL_WRITABLE_KEYS:
        return "invalid_key", f"Key '{key}' is not writable from the panel", value
    # A URL the panel showed with its password masked, sent straight back,
    # would overwrite the real one; it is refused and the saved value kept
    # (8.7.24).
    if shows_hidden_password(value):
        return "invalid_value", HIDDEN_PASSWORD_MESSAGE, value
    # AI endpoints are only saved by nova/apply_ai_config, which normalises,
    # checks the destination and tests the connection first (8.7.23).
    if key in PANEL_AI_ENDPOINT_KEYS:
        return ("invalid_key",
                f"Key '{key}' is set with nova/apply_ai_config (Settings, AI Models), "
                "not nova/update_config", value)
    if key in PANEL_AI_VALUE_KEYS:
        from .safe_errors import NovaValidationError
        try:
            value = panel_ai_value(key, value)
        except NovaValidationError as exc:
            return "invalid_value", safe_error_message(exc), value
    from . import safety_config
    if not safety_config.valid_panel_value(key, value):
        return "invalid_value", safety_config.invalid_panel_value_message(key), value
    if not settings_schema.valid_kind(key, value):
        return "invalid_value", settings_schema.invalid_kind_message(key), value
    return None, "", value


async def _save_hazard_location(hass: HomeAssistant, live: dict, key: str, value):
    """Save a hazard coordinate as half of one lat/lon pair (8.8.2). The
    other half comes from the saved override, else Home Assistant's home.
    An empty value, or a pair that matches home, removes both keys, so a
    later move of home is never hidden by a stale copy. Returns whether the
    save stuck, or None when there is no other half to pair with."""
    from . import hazard_monitor, nova_config
    home = hazard_monitor.base_home_latlon(hass)

    async def _forget_both() -> bool:
        saved = True
        for name in _HAZARD_PAIR:
            live.pop(name, None)
            saved = await hass.async_add_executor_job(nova_config.delete, name) and saved
        return saved

    if value == "":
        return await _forget_both()
    mine = _HAZARD_PAIR.index(key)
    theirs = 1 - mine
    pair = [0.0, 0.0]
    pair[mine] = float(value)
    partner = live.get(_HAZARD_PAIR[theirs], None)
    if partner in (None, ""):
        partner = home[theirs] if home else None
    if partner is None:
        return None
    pair[theirs] = float(partner)
    if home is not None and all(
            math.isclose(pair[i], home[i], rel_tol=0.0, abs_tol=1e-7) for i in (0, 1)):
        return await _forget_both()
    updates = {_HAZARD_PAIR[0]: pair[0], _HAZARD_PAIR[1]: pair[1]}
    live.update(updates)
    return await hass.async_add_executor_job(nova_config.set_many, updates)


async def _apply_sleep_override(hass: HomeAssistant, value) -> None:
    """Give a sleep override its expiry. Saving the value alone would leave
    it inert: sleep_detection.set_override schedules the return to Auto at
    the next end of quiet hours."""
    try:
        from . import nova_config
        quiet_end = await hass.async_add_executor_job(
            nova_config.get, "observer_quiet_end", "07:00")
        await hass.async_add_executor_job(sleep_detection.set_override, value, quiet_end)
    except Exception as exc:
        _LOGGER.warning("sleep_override apply failed: %s", exc)


async def _refresh_after_write(hass: HomeAssistant, runtime, key: str, value) -> None:
    """Live follow ups for keys other parts of Nova hold on to."""
    if key in ("voice_id_uri", "voice_id_fallback_stt"):
        # Check the new address now, so the card's status is current.
        voice_id = getattr(runtime, "voice_id", None)
        if voice_id is not None:
            await voice_id.async_refresh_info()
    if key in _OBSERVER_TIER_KEYS:
        # A running observer picks up a new classifier or reasoning provider
        # or model straight away, as vision already does (Phase 3, v7.108.0).
        try:
            from . import observer
            if observer.is_running():
                await observer.refresh_tier_providers(hass, {key: value})
        except Exception as exc:
            _LOGGER.debug("Observer tier refresh note: %s", exc)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/update_config",
    vol.Required("value"): vol.Any(bool, str, int, float, None),
    vol.Required("key"): str,
})
@websocket_api.async_response
async def ws_update_config(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                           msg: dict[str, Any]) -> None:
    """Change one setting from the panel.

    The value goes into NovaRuntime.runtime_config, not entry.options, since
    an options change reloads the entry and pulls the browser off the panel;
    readers look at runtime_config first. The runtime is resolved before any
    write or side effect, so an entry without one fails with update_failed
    and changes nothing. observer_enabled is all or nothing: the observer is
    started or stopped first, and only a confirmed change is recorded in the
    observer state, runtime_config and config.json.
    """
    key = msg["key"]
    code, problem, value = _vet_panel_write(key, msg["value"])
    if code == "invalid_key":
        connection.send_error(msg["id"], "invalid_key", problem)
        return
    if code is not None:
        connection.send_error(msg["id"], "invalid_value", problem)
        return

    entry = _get_entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "no_entry", "No Nova config entry found")
        return

    try:
        from .runtime import get_runtime
        # Ownership before anything else: raises (update_failed) without one.
        runtime = get_runtime(entry)
        rc = runtime.runtime_config
        if not isinstance(rc, dict):
            connection.send_error(msg["id"], "no_data", "Nova runtime data not found")
            return

        if key == "voice_id_fallback_stt" and value:
            # Falling back to Nova's own Voice ID entity would loop (8.35.0).
            own = getattr(runtime, "voice_id", None)
            if own is not None and own.entity_id == value:
                connection.send_error(msg["id"], "invalid_value",
                                      "The fallback can't be Nova Voice ID itself")
                return
        if key == "voice_id_uri" and isinstance(value, str):
            value = value.strip()

        if key in _HAZARD_PAIR:
            saved = await _save_hazard_location(hass, rc, key, value)
            if saved is None:
                connection.send_error(
                    msg["id"], "invalid_value",
                    "Both hazard coordinates are required when Home Assistant has no home location",
                )
                return
            connection.send_result(msg["id"], {"key": key, "value": value, "persisted": saved})
            return

        if key == "observer_enabled":
            # Change the observer and confirm it with is_running() before
            # anything is recorded. A failed or unconfirmed change raises
            # here, so there is nothing to roll back afterwards.
            from . import observer as observer_mod
            from .runtime import set_observer_running
            get_runtime(entry)
            if value:
                if not observer_mod.is_running():
                    from . import nova_config
                    from .runtime import runtime_config_snapshot
                    # This request's own copy plus the new value; the
                    # executor never sees the live dict.
                    candidate = runtime_config_snapshot(entry, strict=True)
                    candidate[key] = value
                    merged = await hass.async_add_executor_job(
                        nova_config.effective_config_with_runtime, entry, candidate)
                    await observer_mod.start(hass, merged, entry=entry)
                if not observer_mod.is_running():
                    raise RuntimeError("Observer did not start")
                set_observer_running(entry, True)
            else:
                if observer_mod.is_running():
                    await observer_mod.stop()
                if observer_mod.is_running():
                    raise RuntimeError("Observer did not stop")
                set_observer_running(entry, False)

        rc[key] = value
        _LOGGER.info("Nova panel: set %s = %s", key, str(value)[:80])

        if key == "cognition_enabled" and not value:
            # The observation gap starts now, so "none yet today" never
            # spans it (v7.120.2).
            from . import cognition
            cognition.mark_unobserved()

        # Saved to config.json so it survives a restart. This session keeps
        # the new value either way, but the panel is told when the save
        # failed, since the setting would quietly revert on restart.
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

        if key == "sleep_override":
            await _apply_sleep_override(hass, value)
        if key in ("security_alarm_entity", "lockdown_auto_on_arm", "lockdown_exempt_locks",
                   "intrusion_requires_confinement", "face_stand_down"):
            from . import cognitive_core
            await cognitive_core.apply_runtime_config(key, value)
        await _refresh_after_write(hass, runtime, key, value)

        connection.send_result(msg["id"], {"key": key, "value": value, "persisted": persisted})
    except Exception as exc:
        _LOGGER.warning("ws_update_config failed: %s", exc)
        connection.send_error(msg["id"], "update_failed", safe_error_message(exc))


# ─── Logs and health ─────────────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_debug_log",
})
@websocket_api.async_response
async def ws_get_debug_log(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """Nova's debug log with entities named for reading. The file on disk
    and the diagnostics export keep the raw entity_ids.

    Never answers with an error (8.7.23): the pinned contract has no error
    code for this command, so if naming fails the entries go out unnamed
    and the reason goes to the Home Assistant log."""
    entries = list(_DEBUG_LOG)
    try:
        names = _entity_names(hass)
    except Exception as exc:
        safe_error_message(exc, where="get_debug_log naming", log=True)
        names = {}
    connection.send_result(msg["id"], {"entries": _named_log_entries(entries, names)})


@websocket_api.websocket_command({
    vol.Required("type"): "nova/diagnostics",
})
@websocket_api.async_response
async def ws_diagnostics(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """Service health for the panel (v6.60.0): the LLM, embeddings, TTS and
    STT, each with its status and, when down, why."""
    try:
        from . import diagnostics
        connection.send_result(msg["id"], await diagnostics.run_service_health(hass))
    except Exception as exc:
        _LOGGER.exception("ws_diagnostics failed: %s", exc)
        connection.send_error(msg["id"], "diagnostics_failed", safe_error_message(exc))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_setup_health",
})
@websocket_api.async_response
async def ws_get_setup_health(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """The Setup Doctor (Phase 2): a read only check of the configuration
    (stale entity references, room speakers, camera overrides, the notify
    service, Assist pipelines, people, required integrations, persistence)
    with the service health checks folded in. Admin only, since the report
    names entities. It changes nothing, fixes nothing and raises no Repair
    issue; the report is for the panel alone."""
    try:
        from . import setup_health
        connection.send_result(msg["id"], await setup_health.run_setup_health(hass))
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
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """Daily LLM use per provider, model and role (Phase 5): calls, successes
    and failures, mean tokens and latency, for 1 to 90 days. No prompts,
    replies, tool arguments, images or credentials are kept; admin only even
    so, since it shows which providers and models are set up."""
    try:
        from . import provider_activity
        days = min(90, max(1, int(msg.get("days", 7))))
        db_path = provider_activity.db_path_for(hass)
        history = await hass.async_add_executor_job(
            partial(provider_activity.list_days, days, db_path=db_path))
        connection.send_result(msg["id"], {"days": history})
    except Exception as exc:
        _LOGGER.exception("ws_get_provider_activity failed: %s", exc)
        connection.send_error(msg["id"], "get_provider_activity_failed", safe_error_message(exc))


# ─── nova/semantic_search ────────────────────────────────────────────────────

@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/semantic_search",
    vol.Required("action"): vol.In(["status", "enable", "disable", "test"]),
})
@websocket_api.async_response
async def ws_semantic_search(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """Local semantic search (v6.57.0). Nova embeds text with the Ollama
    server it already talks to and keeps the vectors in its own SQLite store
    (ChromaDB's onnxruntime has no Python 3.14 wheel). Actions: status, turn
    it on or off, and test, a live embedding check."""
    action = msg["action"]
    try:
        from . import embeddings, nova_config
        if action == "status":
            def _status() -> tuple:
                # config.json may still need loading, so read off the loop.
                return (bool(nova_config.get("semantic_search", False)),
                        embeddings._ollama_base(), embeddings._model(),
                        embeddings.vector_count())
            enabled, base, model, stored = await hass.async_add_executor_job(_status)
            connection.send_result(msg["id"], {
                "enabled": enabled,
                "ollama_configured": bool(base),
                "base": _masked_url(base),
                "model": model,
                "vector_count": stored,
            })
            return
        if action == "disable":
            await hass.async_add_executor_job(nova_config.set, "semantic_search", False)
            nova_log("AGENT", "semantic search disabled — keyword (FTS) active")
            connection.send_result(msg["id"], {"enabled": False, "ok": True})
            return
        if action == "enable":
            await hass.async_add_executor_job(nova_config.set, "semantic_search", True)
            await hass.async_add_executor_job(embeddings.init_store)
        probe = await embeddings.probe(hass)
        if action == "enable":
            probe["enabled"] = True
            if probe.get("ok"):
                nova_log("AGENT", f"semantic search enabled (Ollama {probe.get('model')}, "
                                  f"dim {probe.get('dim')}) — re-ingest to embed docs")
            else:
                nova_log("AGENT", "semantic search enabled but Ollama not ready: "
                                  f"{probe.get('error')}")
        connection.send_result(msg["id"], probe)
    except Exception as exc:
        _LOGGER.exception("ws_semantic_search failed: %s", exc)
        connection.send_error(msg["id"], "semantic_search_failed", safe_error_message(exc))


# ─── nova/documents ──────────────────────────────────────────────────────────

async def _documents_ingest(hass: HomeAssistant, documents, msg: dict) -> dict:
    outcome = await documents.ingest_directory_async(hass)
    embedded = (f", {outcome.get('embedded_chunks', 0)} embedded"
                if outcome.get("semantic") else "")
    nova_log("AGENT", f"documents ingested via panel: {outcome.get('files_ingested', 0)} "
                      f"files, {outcome.get('total_chunks', 0)} chunks{embedded}")
    return outcome


async def _documents_upload(hass: HomeAssistant, documents, msg: dict) -> dict:
    outcome = await documents.save_and_ingest_upload(
        hass, msg.get("filename", ""), msg.get("content", ""))
    if outcome.get("ok"):
        nova_log("AGENT", f"document uploaded: {outcome.get('filename')} "
                          f"({outcome.get('chunks', 0)} chunks)")
    return outcome


async def _documents_scan(hass: HomeAssistant, documents, msg: dict) -> dict:
    outcome = await documents.scan_watch_folders(hass)
    if outcome.get("new_files"):
        nova_log("AGENT", f"watch-folder scan: {outcome['new_files']} new document(s) ingested")
    return outcome


async def _documents_delete(hass: HomeAssistant, documents, msg: dict) -> dict:
    outcome = await hass.async_add_executor_job(
        documents.delete_source, msg.get("filename", ""))
    if outcome.get("ok"):
        nova_log("AGENT", f"document removed: {msg.get('filename')}")
    return outcome


async def _documents_status(hass: HomeAssistant, documents, msg: dict) -> dict:
    return await hass.async_add_executor_job(documents.library_status)


async def _documents_search(hass: HomeAssistant, documents, msg: dict) -> dict:
    return {"results": await documents.search_documents_async(hass, msg.get("query", ""), 5)}


_DOCUMENT_ACTIONS = {
    "status": _documents_status,
    "ingest": _documents_ingest,
    "upload": _documents_upload,
    "scan_watch": _documents_scan,
    "delete": _documents_delete,
    "search": _documents_search,
}


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): "nova/documents",
    vol.Optional("content"): str,        # base64, for upload
    vol.Optional("filename"): str,
    vol.Optional("query"): str,
    vol.Required("action"): vol.In(["status", "ingest", "search", "upload",
                                    "scan_watch", "delete"]),
})
@websocket_api.async_response
async def ws_documents(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """The document library for the panel (v6.55.0, upload and watch folders
    v6.59.0): status, ingest (rescan), search, upload (a base64 file, saved
    and ingested), scan_watch (pull new files from the watch folders) and
    delete (drop one source). In conversation Nova reaches the same store
    through its search_documents tool."""
    handler = _DOCUMENT_ACTIONS.get(msg["action"], _documents_search)
    try:
        from . import documents
        connection.send_result(msg["id"], await handler(hass, documents, msg))
    except Exception as exc:
        _LOGGER.exception("ws_documents failed: %s", exc)
        connection.send_error(msg["id"], "documents_failed", safe_error_message(exc))


# ─── nova/get_area_sparklines ────────────────────────────────────────────────

@websocket_api.websocket_command({
    vol.Required("type"): "nova/get_area_sparklines",
})
@websocket_api.async_response
async def ws_get_area_sparklines(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any],
) -> None:
    """Recent temperature and humidity history per area, for the dashboard
    sparklines. A command of its own, polled slowly: recorder history is far
    heavier than the rest of the panel data and must not ride on the fast
    refresh."""
    try:
        sensors: dict[str, dict[str, Optional[str]]] = {}
        with area_scan(hass):
            for area_id in _all_areas_with_anything(hass):
                temp_sensor, humidity_sensor = _area_temp_humidity_entities(hass, area_id)
                if temp_sensor or humidity_sensor:
                    sensors[area_id] = {"temp": temp_sensor, "humidity": humidity_sensor}
        connection.send_result(msg["id"], {"sparklines": await _get_area_sparklines(hass, sensors)})
    except Exception as exc:
        _LOGGER.exception("get_area_sparklines failed: %s", exc)
        connection.send_error(msg["id"], "sparklines_failed", safe_error_message(exc))
