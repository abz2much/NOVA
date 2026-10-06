"""Strict helpers for settings that can cause physical device actions."""
from __future__ import annotations

import json
import re

LOCKDOWN_AUTO_KEY = "lockdown_auto_on_arm"
INTRUSION_CONFINEMENT_KEY = "intrusion_requires_confinement"
FACE_STAND_DOWN_KEY = "face_stand_down"

# Phase 4 (v7.109.0): camera semantic-learning tunables must stay within
# their documented bounds — see camera_semantic.py's own clamp_* helpers,
# which this mirrors so a value rejected here is rejected the same way it
# would be clamped there (defence in depth: the panel write is refused
# outright rather than silently clamped later).
CAMERA_EVENT_CONFIDENCE_FLOOR_KEY = "camera_event_confidence_floor"
CAMERA_EVENT_DEDUP_WINDOW_KEY = "camera_event_dedup_window"
_CAMERA_EVENT_CONFIDENCE_FLOOR_RANGE = (0.0, 100.0)
_CAMERA_EVENT_DEDUP_WINDOW_RANGE = (0.0, 3600.0)
CAMERA_AWARENESS_MIN_OBSERVATIONS_KEY = "camera_awareness_min_observations"
_CAMERA_AWARENESS_MIN_OBSERVATIONS_RANGE = (3, 12)

# Phase 10 (host-health awareness): thresholds/persistence/cooldown mirror
# host_health.py's own clamp_* helpers — same defense-in-depth reasoning as
# the Phase 4 camera-learning constants above: a panel write outside bounds
# is refused outright here, not silently clamped downstream only.
HOST_HEALTH_PERSISTENCE_KEY = "host_health_persistence_minutes"
_HOST_HEALTH_PERSISTENCE_RANGE = (2.0, 120.0)
HOST_HEALTH_COOLDOWN_KEY = "host_health_cooldown_minutes"
_HOST_HEALTH_COOLDOWN_RANGE = (5.0, 720.0)
_HOST_HEALTH_THRESHOLD_KEYS = {
    "cpu_percent", "memory_percent", "memory_pressure_some", "memory_pressure_full",
    "io_pressure_some", "io_pressure_full", "disk_percent", "cpu_temperature",
    "swap_percent", "cpu_pressure_some",
}
_HOST_HEALTH_THRESHOLD_RANGES = {
    "cpu_percent": (1.0, 100.0), "memory_percent": (1.0, 100.0),
    "memory_pressure_some": (0.0, 100.0), "memory_pressure_full": (0.0, 100.0),
    "io_pressure_some": (0.0, 100.0), "io_pressure_full": (0.0, 100.0),
    "disk_percent": (1.0, 100.0), "cpu_temperature": (30.0, 110.0),
    "swap_percent": (0.0, 100.0), "cpu_pressure_some": (0.0, 100.0),
}


SCENE_MEMORY_ENABLED_KEY = "scene_memory_enabled"
SCENE_MEMORY_RETENTION_KEY = "scene_memory_retention_days"
_SCENE_MEMORY_RETENTION_RANGE = (1, 90)

# Panel keys that only ever hold true or false (8.7.23): every entry in
# PANEL_WRITABLE_KEYS whose comment says bool, plus observer_enabled. The
# panel saves each of them with a toggle button or chip, which sends a JSON
# boolean, so a string such as "false" or "off" is refused rather than
# stored and later read as on. satellite_audio_out is "dict/bool" and is
# not in this set.
STRICT_BOOL_KEYS = frozenset({
    LOCKDOWN_AUTO_KEY, INTRUSION_CONFINEMENT_KEY, FACE_STAND_DOWN_KEY,
    "observer_enabled", "announce_notify_only",
    "continued_conversation_speaker_reopen", "continued_conversation_multi_satellite",
    "has_basement", "semantic_search", "operational_mode_auto", "biometrics_enabled",
    "voice_confirm_enabled", "onboarding_dismissed",
    "hazard_monitor_enabled", "hazard_quakes_on", "hazard_weather_on", "hazard_disasters_on",
    "hazard_met_eireann_on", "hazard_cap_on",
    "intrusion_vision_confirm",
    "briefing_morning_enabled", "briefing_evening_enabled", "briefing_require_home",
    "suggestion_review_enabled", "adaptive_interruption_budget",
    "adaptive_suggestion_threshold", "adaptive_awareness", "tts_use_ha_voice",
    "pattern_learn_motion", "camera_event_learning", "camera_historical_awareness",
    SCENE_MEMORY_ENABLED_KEY,
    "host_health_enabled", "host_health_alerts_enabled", "host_health_recovery_announce",
    "camera_auto_analyze", "camera_auto_analyze_motion", "package_detection",
    "visitor_learning", "rich_reasoning", "light_control_enabled", "sleep_prompt_enabled",
})

# How long an unanswered intrusion alert waits before the softer "couldn't
# reach you, please check" notice (core_safety.py, default 120 seconds).
# 30 seconds is the cognitive core's tick, so a shorter wait cannot be
# checked any sooner; 600 seconds (10 minutes) is the longest choice the
# panel offers. The panel sends the choice as a string ("120"), so a
# numeric string in range is accepted and stored as sent.
INTRUSION_RESPONSE_TIMEOUT_KEY = "intrusion_response_timeout"
_INTRUSION_RESPONSE_TIMEOUT_RANGE = (30.0, 600.0)

SECURITY_ALARM_ENTITY_KEY = "security_alarm_entity"
_ALARM_ENTITY_ID = re.compile(r"alarm_control_panel\.[a-z0-9_]+")

# Weather warnings (8.8.0).
HAZARD_LEVEL_KEYS = ("hazard_push_level", "hazard_speak_level")
HAZARD_LEVELS = ("yellow", "orange", "red")
HAZARD_NIGHT_LEVEL_KEY = "hazard_night_speak_level"
HAZARD_NIGHT_LEVELS = HAZARD_LEVELS + ("off",)
HAZARD_COUNTIES_KEY = "hazard_counties"
HAZARD_CAP_URL_KEY = "hazard_cap_url"
HAZARD_CAP_LIST_KEYS = ("hazard_cap_area_codes", "hazard_cap_area_names")

SLEEP_OVERRIDE_KEY = "sleep_override"
SLEEP_OVERRIDE_VALUES = ("auto", "awake", "asleep")


def automatic_lockdown_enabled(config: dict | None) -> bool:
    """Only the literal JSON boolean true enables automatic device control."""
    return isinstance(config, dict) and config.get(LOCKDOWN_AUTO_KEY) is True


def intrusion_requires_confinement(config: dict | None) -> bool:
    """Only the literal JSON boolean true makes confinement the master switch
    for intrusion monitoring. Anything else keeps the automatic behaviour."""
    return isinstance(config, dict) and config.get(INTRUSION_CONFINEMENT_KEY) is True


def face_stand_down_enabled(config: dict | None) -> bool:
    """Only the literal JSON boolean true lets a recognised resident stop a NEW
    intrusion investigation from opening. Off by default; anything else keeps
    the existing behaviour."""
    return isinstance(config, dict) and config.get(FACE_STAND_DOWN_KEY) is True


def _valid_bounded_number(value, lo: float, hi: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    import math
    return math.isfinite(v) and lo <= v <= hi


def _valid_bounded_integer(value, lo: int, hi: int) -> bool:
    return type(value) is int and lo <= value <= hi


def _valid_number_or_numeric_string(value, lo: float, hi: float) -> bool:
    """A bounded number, or a string holding one (what a panel select sends)."""
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return False
    return _valid_bounded_number(value, lo, hi)


def _json_string_list(value) -> list | None:
    """A JSON list of strings (as the panel sends a list), or None."""
    if not isinstance(value, str):
        return None
    try:
        items = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        return None
    return items


def _valid_cap_url(value) -> bool:
    """Empty (no custom feed), or an https URL that passes the AI endpoints'
    destination checks without resolving the host: no user:pass@, no cloud
    metadata name or link-local address. The host is resolved and checked
    again on every fetch and every redirect (hazard_cap.py)."""
    if value == "":
        return True
    if not isinstance(value, str) or len(value) > 2048:
        return False
    from urllib.parse import urlparse
    from .providers.destinations import check_url
    from .providers.errors import ProviderError
    try:
        if urlparse(value).scheme != "https":
            return False
        check_url(value, resolve=False)
    except (ProviderError, ValueError):
        return False
    return True


def valid_panel_value(key: str, value) -> bool:
    """Whether nova/update_config may write ``value`` for ``key``. Only on
    write: values already saved are never checked again here, so an older
    config keeps loading."""
    if key in STRICT_BOOL_KEYS:
        return type(value) is bool
    if key == INTRUSION_RESPONSE_TIMEOUT_KEY:
        return _valid_number_or_numeric_string(value, *_INTRUSION_RESPONSE_TIMEOUT_RANGE)
    if key == SECURITY_ALARM_ENTITY_KEY:
        # Empty means auto detect. The panel may not be online right now, so
        # only the form of the id is checked, not that it exists.
        return value == "" or (isinstance(value, str)
                               and _ALARM_ENTITY_ID.fullmatch(value) is not None)
    if key == SLEEP_OVERRIDE_KEY:
        return isinstance(value, str) and value in SLEEP_OVERRIDE_VALUES
    if key in HAZARD_LEVEL_KEYS:
        return isinstance(value, str) and value in HAZARD_LEVELS
    if key == HAZARD_NIGHT_LEVEL_KEY:
        return isinstance(value, str) and value in HAZARD_NIGHT_LEVELS
    if key == HAZARD_COUNTIES_KEY:
        from .hazard_met_eireann import COUNTIES
        items = _json_string_list(value)
        return items is not None and all(c in COUNTIES for c in items)
    if key in HAZARD_CAP_LIST_KEYS:
        items = _json_string_list(value)
        return items is not None and all(i.strip() for i in items)
    if key == HAZARD_CAP_URL_KEY:
        return _valid_cap_url(value)
    if key == SCENE_MEMORY_RETENTION_KEY:
        return _valid_bounded_integer(value, *_SCENE_MEMORY_RETENTION_RANGE)
    if key == CAMERA_AWARENESS_MIN_OBSERVATIONS_KEY:
        return _valid_bounded_integer(
            value, *_CAMERA_AWARENESS_MIN_OBSERVATIONS_RANGE,
        )
    if key == CAMERA_EVENT_CONFIDENCE_FLOOR_KEY:
        return _valid_bounded_number(value, *_CAMERA_EVENT_CONFIDENCE_FLOOR_RANGE)
    if key == CAMERA_EVENT_DEDUP_WINDOW_KEY:
        return _valid_bounded_number(value, *_CAMERA_EVENT_DEDUP_WINDOW_RANGE)
    if key == "output_language":
        from . import output_language
        return output_language.is_valid_setting(value)
    if key == HOST_HEALTH_PERSISTENCE_KEY:
        return _valid_bounded_number(value, *_HOST_HEALTH_PERSISTENCE_RANGE)
    if key == HOST_HEALTH_COOLDOWN_KEY:
        return _valid_bounded_number(value, *_HOST_HEALTH_COOLDOWN_RANGE)
    if key == "host_health_mappings":
        # JSON-encoded string, same convention as notify_services/
        # room_speakers/satellite_pairings — the websocket nova/update_config
        # schema only accepts bool/str/int/float/None for `value`, so a dict
        # config always arrives JSON-stringified, never as a native object.
        # An empty entry clears that metric's mapping (falls back to auto).
        if not isinstance(value, str):
            return False
        try:
            mapping = json.loads(value)
        except (TypeError, ValueError):
            return False
        if not isinstance(mapping, dict):
            return False
        return all(
            isinstance(k, str)
            and (v == "" or (isinstance(v, str)
                             and re.fullmatch(r"sensor\.[a-z0-9_]+", v) is not None))
            for k, v in mapping.items()
        )
    if key == "host_health_thresholds":
        if not isinstance(value, str):
            return False
        try:
            thresholds = json.loads(value)
        except (TypeError, ValueError):
            return False
        if not isinstance(thresholds, dict):
            return False
        for k, v in thresholds.items():
            if k not in _HOST_HEALTH_THRESHOLD_KEYS:
                return False
            if not _valid_bounded_number(v, *_HOST_HEALTH_THRESHOLD_RANGES[k]):
                return False
        return True
    if key == "notify_services":
        if not isinstance(value, str):
            return False
        try:
            services = json.loads(value)
        except (TypeError, ValueError):
            return False
        return (
            isinstance(services, list)
            and all(isinstance(item, str)
                    and re.fullmatch(r"notify\.[a-z0-9_]+", item) is not None
                    for item in services)
        )
    return True


def _range_text(lo, hi) -> str:
    return f"{lo:g} to {hi:g}"


def invalid_panel_value_message(key: str) -> str:
    """The panel's message for a value valid_panel_value refused, worded for
    the kind of value the key takes (8.7.23). Fixed text only."""
    if key in STRICT_BOOL_KEYS:
        return f"Key '{key}' requires a boolean value"
    if key == "output_language":
        return "Key 'output_language' must be 'auto' or a supported language code"
    if key == INTRUSION_RESPONSE_TIMEOUT_KEY:
        return (f"Key '{key}' must be a number of seconds from "
                f"{_range_text(*_INTRUSION_RESPONSE_TIMEOUT_RANGE)}")
    if key == SECURITY_ALARM_ENTITY_KEY:
        return f"Key '{key}' must be empty or an alarm_control_panel entity id"
    if key == SLEEP_OVERRIDE_KEY:
        return f"Key '{key}' must be one of: auto, awake, asleep"
    if key in HAZARD_LEVEL_KEYS:
        return f"Key '{key}' must be one of: yellow, orange, red"
    if key == HAZARD_NIGHT_LEVEL_KEY:
        return f"Key '{key}' must be one of: yellow, orange, red, off"
    if key == HAZARD_COUNTIES_KEY:
        return f"Key '{key}' must be a JSON list of Met Éireann county codes, such as [\"EI07\"]"
    if key in HAZARD_CAP_LIST_KEYS:
        return f"Key '{key}' must be a JSON list of non-empty strings"
    if key == HAZARD_CAP_URL_KEY:
        return (f"Key '{key}' must be empty or an https address with no user name or "
                "password, not a link-local or cloud metadata address")
    whole = {SCENE_MEMORY_RETENTION_KEY: _SCENE_MEMORY_RETENTION_RANGE,
             CAMERA_AWARENESS_MIN_OBSERVATIONS_KEY: _CAMERA_AWARENESS_MIN_OBSERVATIONS_RANGE}
    if key in whole:
        return f"Key '{key}' must be a whole number from {_range_text(*whole[key])}"
    number = {CAMERA_EVENT_CONFIDENCE_FLOOR_KEY: _CAMERA_EVENT_CONFIDENCE_FLOOR_RANGE,
              CAMERA_EVENT_DEDUP_WINDOW_KEY: _CAMERA_EVENT_DEDUP_WINDOW_RANGE,
              HOST_HEALTH_PERSISTENCE_KEY: _HOST_HEALTH_PERSISTENCE_RANGE,
              HOST_HEALTH_COOLDOWN_KEY: _HOST_HEALTH_COOLDOWN_RANGE}
    if key in number:
        return f"Key '{key}' must be a number from {_range_text(*number[key])}"
    if key == "host_health_mappings":
        return (f"Key '{key}' must be a JSON object mapping each metric to a "
                "sensor entity id, or to an empty string")
    if key == "host_health_thresholds":
        return (f"Key '{key}' must be a JSON object of known metrics, each "
                "with a number in its allowed range")
    if key == "notify_services":
        return f"Key '{key}' must be a JSON list of notify service ids, such as notify.mobile_app_phone"
    return f"Key '{key}' has a value that is not allowed"
