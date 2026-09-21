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


def valid_panel_value(key: str, value) -> bool:
    """Reject truthy strings and numbers for the automatic safety opt in."""
    if key == LOCKDOWN_AUTO_KEY:
        return type(value) is bool
    if key == "camera_event_learning":
        return type(value) is bool
    if key == CAMERA_EVENT_CONFIDENCE_FLOOR_KEY:
        return _valid_bounded_number(value, *_CAMERA_EVENT_CONFIDENCE_FLOOR_RANGE)
    if key == CAMERA_EVENT_DEDUP_WINDOW_KEY:
        return _valid_bounded_number(value, *_CAMERA_EVENT_DEDUP_WINDOW_RANGE)
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
