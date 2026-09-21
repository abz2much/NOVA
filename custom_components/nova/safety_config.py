"""Strict helpers for settings that can cause physical device actions."""
from __future__ import annotations

import json
import re

LOCKDOWN_AUTO_KEY = "lockdown_auto_on_arm"

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


def automatic_lockdown_enabled(config: dict | None) -> bool:
    """Only the literal JSON boolean true enables automatic device control."""
    return isinstance(config, dict) and config.get(LOCKDOWN_AUTO_KEY) is True


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


def valid_panel_value(key: str, value) -> bool:
    """Reject truthy strings and numbers for the automatic safety opt in."""
    if key == LOCKDOWN_AUTO_KEY:
        return type(value) is bool
    if key in ("camera_event_learning", "camera_historical_awareness"):
        return type(value) is bool
    if key == CAMERA_AWARENESS_MIN_OBSERVATIONS_KEY:
        return _valid_bounded_integer(
            value, *_CAMERA_AWARENESS_MIN_OBSERVATIONS_RANGE,
        )
    if key == CAMERA_EVENT_CONFIDENCE_FLOOR_KEY:
        return _valid_bounded_number(value, *_CAMERA_EVENT_CONFIDENCE_FLOOR_RANGE)
    if key == CAMERA_EVENT_DEDUP_WINDOW_KEY:
        return _valid_bounded_number(value, *_CAMERA_EVENT_DEDUP_WINDOW_RANGE)
    if key in ("host_health_enabled", "host_health_alerts_enabled",
               "host_health_recovery_announce"):
        return type(value) is bool
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
