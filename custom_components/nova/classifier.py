"""Observer tier 1: is this state change worth Nova's attention?

A fixed rule table settles nearly every Home Assistant state change without
a provider call. Only events no rule covers go to the classifier LLM, as one
short call that answers in JSON. Nothing here raises: a provider failure
simply means "not worth considering". The Observer is the only caller.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from .const import URGENCY_CEILINGS

_LOGGER = logging.getLogger(__name__)

# Entity id prefixes that are pure bookkeeping (plain startswith match).
_NOISE_PREFIXES = (
    "sensor.sun_", "sensor.time", "sensor.date", "sensor.uptime",
    "sensor.last_boot", "sensor.cpu", "sensor.memory", "sensor.disk",
    "sensor.network", "sensor.processor", "sensor.load_", "sensor.swap",
    "button.", "number.", "input_number.", "input_select.", "input_boolean.",
    "input_text.", "input_datetime.", "update.", "automation.", "script.",
    "scene.", "zone.", "device_tracker.", "persistent_notification.",
    "counter.", "timer.",
)

_NOISE_DEVICE_CLASSES = {
    "connectivity", "plug", "power", "update", "running", "battery_charging",
}

_NOISE_TRANSITIONS = {
    ("unavailable", "unknown"), ("unknown", "unavailable"),
    ("unavailable", "unavailable"), ("unknown", "unknown"),
}

_OPENING_CLASSES = {"door", "window", "garage_door", "opening"}
_MOTION_CLASSES = {"motion", "occupancy"}
_ROUTINE_DOMAINS = ("light", "switch", "climate", "media_player")
_URGENCIES = ("low", "medium", "high", "critical")

_LLM_PROMPT = (
    "You classify a single Home Assistant event for a home assistant AI. "
    "Return ONLY JSON, no prose, with keys: "
    '"worth_considering" (bool), '
    '"urgency" (one of low|medium|high|critical) and '
    '"category" (one of appliances, doors_windows, presence, energy, '
    "security, climate, other)."
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_OBJECT_RE = re.compile(r"\{[^}]*?\}", re.DOTALL)


def _hour(now_hhmm: str) -> Optional[int]:
    try:
        return int(now_hhmm.split(":", 1)[0])
    except (AttributeError, ValueError):
        return None


def _skip(rule: str) -> dict:
    return {"worth_considering": False, "rule": rule}


def _flag(urgency: str, category: str, rule: str) -> dict:
    return {"worth_considering": True, "urgency": urgency,
            "category": category, "rule": rule}


def _rule_classify(entity_id, old_state, new_state, friendly_name,
                   device_class, now_hhmm) -> Optional[dict]:
    """The rule table. None means no rule covers the event."""
    eid = entity_id.lower()
    dc = (device_class or "").lower()

    if eid.startswith(_NOISE_PREFIXES):
        return _skip("noise_prefix")
    if (old_state, new_state) in _NOISE_TRANSITIONS or old_state == new_state:
        return _skip("noise_transition")
    if dc in _NOISE_DEVICE_CLASSES:
        return _skip("noise_dc")
    if URGENCY_CEILINGS.get(dc) == "critical":
        return _flag("critical", "security", "safety")

    hour = _hour(now_hhmm)
    if dc in _OPENING_CLASSES:
        if new_state != "on":
            return _skip("door_closed")
        late = hour is not None and (hour >= 23 or hour < 6)
        return _flag("high" if late else "medium", "doors_windows", "door_opened")
    if dc in _MOTION_CLASSES:
        if new_state == "off":
            return _skip("motion_clear")
        if hour is not None and (hour >= 23 or hour < 5):
            return _flag("low", "presence", "motion_night")
        return _skip("motion_routine")

    if eid.startswith("person."):
        if new_state == "home" and old_state != "home":
            return _flag("medium", "presence", "arrived")
        if old_state == "home" and new_state != "home":
            return _flag("medium", "presence", "left")
        return _skip("person_same")
    if eid.startswith("lock."):
        if new_state == "unlocked":
            return _flag("medium", "security", "unlocked")
        return _skip("locked")
    if eid.startswith("alarm_control_panel."):
        if new_state in ("triggered", "pending"):
            return _flag("critical", "security", "alarm")
        return _flag("low", "security", "alarm_change")
    for domain in _ROUTINE_DOMAINS:
        if eid.startswith(domain + "."):
            return _skip(f"{domain}_routine")
    if eid.startswith("sensor."):
        try:
            jump = abs(float(new_state) - float(old_state))
        except (TypeError, ValueError):
            jump = 0.0
        if jump > 1000 and ("power" in eid or "watt" in eid):
            return _flag("low", "energy", "power_spike")
        return _skip("sensor_drift")
    if eid.startswith("binary_sensor.") and not dc:
        return _skip("binary_no_dc")
    return None


def _parse_json(raw: str) -> Any:
    """Parse the provider's reply, tolerating fences and stray prose."""
    if not raw:
        return {"worth_considering": False}
    text = raw.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    span = _OBJECT_RE.search(text)
    if span:
        try:
            return json.loads(span.group(0))
        except json.JSONDecodeError:
            pass
    return {"worth_considering": False}


async def classify(hass, provider, *, entity_id: str, old_state: str, new_state: str,
                   friendly_name: str = "", device_class: Optional[str] = None,
                   now_hhmm: str = "", recent_activity_summary: str = "") -> dict:
    """Decide whether one state change deserves further thought."""
    ruled = _rule_classify(entity_id, old_state, new_state, friendly_name,
                           device_class, now_hhmm)
    if ruled is not None:
        return ruled

    _LOGGER.info("Classifier: no rule for %s (%s -> %s), asking provider",
                 entity_id, old_state, new_state)
    try:
        from .websocket import nova_log
        nova_log("CLASSIFY", f"{entity_id}: {old_state} → {new_state} (no rule, asking provider)")
    except Exception:
        pass

    try:
        from . import llm_provider
        event_line = (f"Time:{now_hhmm} Entity:{entity_id} Name:{friendly_name} "
                      f"Class:{device_class} {old_state}→{new_state}")
        messages = [
            {"role": "system", "content": _LLM_PROMPT},
            {"role": "user", "content": event_line},
        ]
        resp = await llm_provider.chat_with_activity(
            hass, provider, messages, role="classifier", data_category="text",
            temperature=0.0, max_tokens=80)
        text = resp.get("text", "") if isinstance(resp, dict) else str(resp)
        parsed = _parse_json(text)
        if not parsed.get("worth_considering"):
            return {"worth_considering": False}
        urgency = parsed.get("urgency", "low")
        if urgency not in _URGENCIES:
            urgency = "low"
        return {"worth_considering": True, "urgency": urgency,
                "category": parsed.get("category", "other")}
    except Exception as exc:
        _LOGGER.warning("Classifier provider call failed for %s: %s", entity_id, exc)
        return {"worth_considering": False}
