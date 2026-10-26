"""Every setting the panel can change: its kind, its default and how it is
checked, defined once (8.42.0).

The panel's allow list (websocket.PANEL_WRITABLE_KEYS), the type check on
every nova/update_config write and the plain entries of the panel's config
payload all come from SETTINGS. Checks that need more than the kind (ranges,
entity id forms, allowed words) stay in safety_config.valid_panel_value and
run as well.

Kinds and what a write may send:

* bool: a JSON true or false, nothing else.
* number: a number, a numeric string, or empty.
* text: a string, or empty.
* url: a string, or empty. The payload shows it with any password masked.
* json: a JSON list or object sent as a string, or empty.
* any: anything the websocket schema allows.

`default` is the value Nova uses when nothing is saved. None means Nova
works it out at run time (from the home's region, another setting or Home
Assistant), and that setting's payload entry is built in websocket.py.

`payload` says how the panel's config payload shows the setting: "plain"
builds the entry from the kind and default, "custom" means websocket.py
builds it, and "hidden" leaves it out.

Saved values are never checked again on read, so an older config keeps
loading.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

BOOL = "bool"
NUMBER = "number"
TEXT = "text"
URL = "url"
JSON = "json"
ANY = "any"

KINDS = frozenset({BOOL, NUMBER, TEXT, URL, JSON, ANY})


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str
    default: Any = None
    payload: str = "plain"
    # The payload shows true only for a saved JSON true, as the code that
    # reads these settings does (safety_config).
    exact_true: bool = False


def _b(key: str, default: bool, payload: str = "plain", exact_true: bool = False) -> Setting:
    return Setting(key, BOOL, default, payload, exact_true)


def _n(key: str, default: Any, payload: str = "plain") -> Setting:
    return Setting(key, NUMBER, default, payload)


def _t(key: str, default: Any = "", payload: str = "plain") -> Setting:
    return Setting(key, TEXT, default, payload)


def _u(key: str) -> Setting:
    return Setting(key, URL, "")


def _j(key: str, default: Any, payload: str = "plain") -> Setting:
    return Setting(key, JSON, default, payload)


_ALL = (
    # Language and voice
    _t("ui_language", "auto"),
    _t("ui_theme", "deep_teal"),
    _t("output_language", ""),
    _b("tts_use_ha_voice", False),
    _n("banter_level", 1),
    _b("hero_card_cycle", True),
    # Announcements and phones
    _b("announcements_enabled", True, "custom"),
    _b("announce_notify_only", False),
    _t("notify_service", "", "custom"),
    _j("notify_services", [], "custom"),
    _j("announcement_speakers", []),
    _j("room_speakers", {}),
    _t("general_speaker", ""),
    _j("satellite_pairings", {}),
    Setting("satellite_audio_out", ANY, None, "hidden"),
    _j("satellite_start_action", {}),
    _j("person_honorifics", {}),
    # Awareness and learning
    _b("sentinel_enabled", True, "custom"),
    _b("observer_enabled", False, "custom"),
    _j("disabled_sentinel_rules", [], "custom"),
    _b("cognition_enabled", True),
    _n("cognition_threshold", 0.6),
    _n("classifier_rate_limit", 30),
    _n("observer_group_debounce", 90),
    _b("pattern_learn_doors", False),
    _b("pattern_learn_presence", False),
    _b("pattern_learn_buttons", False),
    _b("pattern_learn_motion", False),
    _j("pattern_include_entities", []),
    _n("pattern_min_occurrences", 4),
    _n("pattern_confidence", 0.55),
    _j("excluded_entities", []),
    _j("excluded_domains", []),
    _j("excluded_labels", []),
    _j("outdoor_areas", []),
    _j("outdoor_entities", []),
    _j("indoor_entities", []),
    _b("adaptive_interruption_budget", False),
    _b("adaptive_suggestion_threshold", False),
    _b("adaptive_awareness", False),
    _b("suggestion_review_enabled", False),
    _t("suggestion_review_provider", None, "custom"),
    _t("suggestion_review_model", None, "custom"),
    _b("onboarding_dismissed", False),
    _b("auto_bootstrap", False),
    # Cameras and faces
    _b("camera_event_learning", True),
    _b("camera_historical_awareness", None, "custom"),
    _n("camera_awareness_min_observations", 3),
    _n("camera_event_confidence_floor", 50.0),
    _n("camera_event_dedup_window", 300.0),
    _b("scene_memory_enabled", False, exact_true=True),
    _n("scene_memory_retention_days", 14),
    _b("camera_auto_analyze", True),
    _b("camera_auto_analyze_motion", False),
    _b("package_detection", False),
    _b("visitor_learning", True),
    _j("disabled_cameras", []),
    _t("recognition_source", "off"),
    _n("identity_min_confidence", 0.45),
    # Security
    _t("security_alarm_entity", ""),
    _b("lockdown_auto_on_arm", False, exact_true=True),
    _j("lockdown_exempt_locks", None, "custom"),
    _b("intrusion_requires_confinement", False, exact_true=True),
    _b("face_stand_down", False, exact_true=True),
    _n("intrusion_response_timeout", 120),
    _b("intrusion_vision_confirm", True),
    _n("intrusion_inward_depth", 2),
    _n("intrusion_notify_image_ttl_minutes", 60.0),
    _b("voice_confirm_enabled", False),
    _t("voice_confirm_mode", "auto"),
    _j("voice_confirm_entities", []),
    # Voice ID
    _t("voice_id_uri", ""),
    _t("voice_id_fallback_stt", ""),
    _j("voice_id_hidden_people", []),
    _b("voice_id_identity", False, exact_true=True),
    _b("voice_id_learning", False, exact_true=True),
    # Leaving home and routines
    _b("departure_alerts_enabled", False),
    _b("routine_alerts_enabled", True),
    _n("departure_lead_minutes", 30),
    _n("routine_departure_lead_minutes", 15),
    _t("departure_origin_entity", ""),
    _u("departure_osrm_url"),
    _t("departure_travel_sensor", ""),
    _j("departure_excluded_calendars", []),
    _b("departure_mode_walk", True),
    _b("departure_mode_transit", True),
    _b("departure_mode_drive", True),
    _b("departure_use_google", True),
    _n("calendar_tight_gap_min", 15),
    # Memory and conversation
    _b("memory_threading_enabled", True),
    _n("memory_threading_hours", 48),
    _n("memory_threading_max", 12),
    _b("continued_conversation_enabled", False),
    _b("continued_conversation_speaker_reopen", True),
    _b("continued_conversation_multi_satellite", False),
    _b("rich_reasoning", False),
    _n("home_context_max_entities", 15, "custom"),
    # Sleep and briefings
    _t("sleep_override", "auto", "custom"),
    _b("sleep_prompt_enabled", True),
    _t("sleep_prompt_time", "23:00"),
    _b("briefing_morning_enabled", False),
    _b("briefing_evening_enabled", False),
    _t("briefing_morning_time", "07:30"),
    _t("briefing_evening_time", "19:30"),
    _b("briefing_require_home", True),
    _b("briefing_include_weather", True),
    _b("briefing_include_calendar", True),
    _b("briefing_include_presence", True),
    _b("briefing_include_events", True),
    _b("briefing_include_energy", True),
    _b("briefing_include_hazards", True),
    # Weather warnings and hazards
    _b("hazard_monitor_enabled", False),
    _n("hazard_lat", None, "custom"),
    _n("hazard_lon", None, "custom"),
    _b("hazard_quakes_on", None, "custom"),
    _b("hazard_weather_on", None, "custom"),
    _b("hazard_disasters_on", None, "custom"),
    _b("hazard_met_eireann_on", None, "custom"),
    _b("hazard_cap_on", False),
    _t("hazard_source", None, "custom"),
    _j("hazard_counties", []),
    _t("hazard_push_level", "yellow"),
    _t("hazard_speak_level", "orange"),
    _t("hazard_night_speak_level", "red"),
    _u("hazard_cap_url"),
    _j("hazard_cap_area_codes", []),
    _j("hazard_cap_area_names", []),
    _n("hazard_quake_radius_km", 300),
    _n("hazard_quake_min_mag", 2.5),
    _n("hazard_disaster_radius_km", 300.0),
    # Home layout and modes
    _n("home_stories", "1.5"),
    _b("has_basement", True),
    _n("garage_bays", 3),
    _t("garage_mode", "auto", "custom"),
    _t("basement_mode", None, "custom"),
    _j("exit_doors", [], "custom"),
    _j("door_mapping", {}),
    _t("arrival_front_door_entity", ""),
    _j("floor_plan_rooms", {}),
    _j("floor_plan_bg", {}),
    _t("floor_plan_units", "imperial"),
    _j("floor_plan_elements", {}),
    _j("floor_plan_cameras", {}),
    _j("floor_plan_property", {}),
    _j("floor_plan_entities", {}),
    _n("floor_plan_bg_opacity", "0.2"),
    _j("custom_modes", {}),
    _b("operational_mode_auto", True),
    _j("lab_areas", []),
    _t("movie_area", ""),
    _t("movie_media_player", ""),
    _n("movie_dim_pct", 15, "custom"),
    _b("light_control_enabled", True),
    # Energy and appliances
    _t("energy_agency", "advisory"),
    _n("energy_peak_watts", 8000.0),
    _j("energy_mode_bump", []),
    _t("energy_cost_today_entity", ""),
    _t("energy_cost_net_entity", ""),
    _j("appliance_profile", []),
    # Wellbeing
    _b("biometrics_enabled", False),
    _j("biometric_entities", {}),
    # Infrastructure and host health
    _t("infrastructure_audit_area", ""),
    _j("infrastructure_audit_sensors", []),
    _b("host_health_enabled", False),
    _b("host_health_alerts_enabled", False),
    _b("host_health_recovery_announce", True),
    _n("host_health_persistence_minutes", 10),
    _n("host_health_cooldown_minutes", 60),
    _j("host_health_mappings", {}),
    _j("host_health_thresholds", {}),
    # Documents and web research
    _t("document_watch_folders", ""),
    _b("semantic_search", False),
    _t("embed_model", "nomic-embed-text"),
    _u("embed_base_url"),
    _t("search_backend", "off"),
    _u("searxng_url"),
    # AI models. Endpoints are written by nova/apply_ai_config only. A model
    # left empty is the provider's own default model.
    _t("llm_provider", "groq"),
    _t("model", None),
    _t("classifier_provider", "groq"),
    _t("classifier_model", None),
    _t("reasoning_provider", "groq"),
    _t("reasoning_model", None),
    _t("review_provider", "groq"),
    _t("review_model", None),
    _t("vision_provider", "groq"),
    _t("vision_model", None),
    _t("camera_reasoning_provider", "groq"),
    _t("camera_reasoning_model", None),
    _u("llm_base_url"),
    _u("ollama_base_url"),
    _u("custom_base_url"),
    _n("ollama_num_ctx", 8192),
)

SETTINGS: dict[str, Setting] = {s.key: s for s in _ALL}

# Every key nova/update_config may write.
PANEL_KEYS = frozenset(SETTINGS)

# Keys that only ever hold a JSON true or false.
BOOL_KEYS = frozenset(k for k, s in SETTINGS.items() if s.kind == BOOL)


def default(key: str) -> Any:
    """The default for one setting, or None when it is worked out at run
    time or the key is not a panel setting."""
    s = SETTINGS.get(key)
    return None if s is None else s.default


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return False
        return number == number and number not in (float("inf"), float("-inf"))
    return False


def valid_kind(key: str, value: Any) -> bool:
    """Whether `value` is the right kind of value for `key`. Unknown keys
    are refused."""
    s = SETTINGS.get(key)
    if s is None:
        return False
    if s.kind == ANY:
        return True
    if s.kind == BOOL:
        return type(value) is bool
    if value is None or value == "":
        return True
    if s.kind == NUMBER:
        return _is_number(value)
    if s.kind in (TEXT, URL):
        return isinstance(value, str)
    if s.kind == JSON:
        if isinstance(value, (list, dict)):
            return True
        if not isinstance(value, str):
            return False
        try:
            return isinstance(json.loads(value), (list, dict))
        except ValueError:
            return False
    return False


_KIND_WORDING = {
    BOOL: "true or false",
    NUMBER: "a number",
    TEXT: "text",
    URL: "an address",
    JSON: "a JSON list or object",
}


def invalid_kind_message(key: str) -> str:
    """The panel's message for a value valid_kind refused. Fixed text only."""
    s = SETTINGS.get(key)
    if s is None:
        return f"Key '{key}' is not writable from the panel"
    return f"Key '{key}' must be {_KIND_WORDING.get(s.kind, 'a valid value')}"


def payload(read: Callable[[str, Any], Any],
            read_json: Callable[[str, Any], Any],
            mask: Callable[[Any], Any]) -> dict[str, Any]:
    """The panel's config entry for every "plain" setting.

    `read(key, default)` returns the saved value or the default,
    `read_json(key, default)` does the same for a JSON value and `mask(url)`
    hides a password in an address."""
    out: dict[str, Any] = {}
    for s in SETTINGS.values():
        if s.payload != "plain":
            continue
        if s.kind == JSON:
            out[s.key] = read_json(s.key, s.default)
            continue
        value = read(s.key, s.default)
        if s.kind == BOOL:
            out[s.key] = value is True if s.exact_true else bool(value)
        elif s.kind == TEXT:
            out[s.key] = str(value or s.default or "")
        elif s.kind == URL:
            out[s.key] = mask(value)
        else:
            out[s.key] = value
    return out
