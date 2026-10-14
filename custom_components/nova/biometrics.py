"""
Nova — wearable readings as context.

Reads what a wearable already reports to Home Assistant (heart rate, sleep,
steps and so on) as comfort and context for Nova's own behaviour, mainly so
a wearable that says "asleep" counts towards sleep detection.

Context only, not a medical device: no diagnosis, thresholds or alarms.
Off unless the person turns it on. Never raises to callers.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)

# (kind, keywords, unit hints), checked in this order
_KINDS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("heart_rate", ("heart_rate", "heartrate", "heart rate", "_hr", "bpm", "pulse"), ("bpm",)),
    ("resting_hr", ("resting_heart", "resting hr", "rest_heart"), ("bpm",)),
    ("sleep_stage", ("sleep_stage", "sleep state", "sleep_status", "sleep phase"), ()),
    ("sleep_state", ("is_asleep", "asleep", "in_bed", "sleep", "sleeping"), ()),
    ("spo2", ("spo2", "blood_oxygen", "oxygen_saturation", "sp_o2"), ("%",)),
    ("steps", ("steps", "step_count", "step count"), ("steps",)),
    ("respiratory", ("respiratory", "breathing_rate", "resp_rate"), ("br",)),
    ("stress", ("stress", "stress_level"), ()),
    ("temperature", ("body_temp", "skin_temp", "body temperature"), ()),
)

_BLANK = {"unknown", "unavailable", "none", ""}
_ASLEEP_HINTS = ("asleep", "sleeping", "deep", "rem", "light_sleep", "light sleep", "in_bed", "on")
_AWAKE_VALUES = {"awake", "off", "not_asleep", "out_of_bed"}

_DISCLAIMER = "context only — not medical advice or assessment"


def _cfg(key: str, default: Any = None) -> Any:
    try:
        from . import nova_config
        value = nova_config.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def is_enabled() -> bool:
    return bool(_cfg("biometrics_enabled", False))


def _classify(entity_id: str, name: str, unit: str, device_class: str) -> Optional[str]:
    """The kind of reading a sensor gives, or None."""
    hay = f"{entity_id} {name}".lower()
    unit = (unit or "").lower()
    mapping = _cfg("biometric_entities", {})
    if isinstance(mapping, dict):
        for kind, eid in mapping.items():
            if eid == entity_id:
                return kind
    for kind, keywords, units in _KINDS:
        if any(word in hay for word in keywords):
            return kind
        if units and unit in units:
            return kind
    return None


def discover(hass: Any) -> dict[str, list[dict]]:
    """{kind: [{entity, value, unit, name}]} for every recognised sensor."""
    try:
        states = list(hass.states.async_all("sensor")) + list(hass.states.async_all("binary_sensor"))
    except Exception:
        return {}
    found: dict[str, list[dict]] = {}
    for st in states:
        try:
            attrs = st.attributes
            name = attrs.get("friendly_name") or ""
            unit = attrs.get("unit_of_measurement") or ""
            device_class = attrs.get("device_class") or ""
            kind = _classify(st.entity_id, name, unit, device_class)
            if kind:
                found.setdefault(kind, []).append(
                    {"entity": st.entity_id, "value": st.state, "unit": unit, "name": name})
        except Exception as exc:
            _LOGGER.debug("Skipping %s: %s", getattr(st, "entity_id", "?"), exc)
    return found


def sleep_signal(hass: Any) -> Optional[bool]:
    """True/False when a wearable says asleep/awake; None when it can't tell."""
    if not is_enabled():
        return None
    found = discover(hass)
    for kind in ("sleep_stage", "sleep_state"):
        for entry in found.get(kind, []):
            value = str(entry["value"]).strip().lower()
            if value in _BLANK:
                continue
            # Whole words only, awake first: "not_asleep" is awake and
            # "monitoring" is not "on".
            if re.sub(r"[\s-]+", "_", value) in _AWAKE_VALUES:
                return False
            words = set(re.split(r"[^a-z0-9]+", value))
            if any(set(hint.replace("_", " ").split()) <= words for hint in _ASLEEP_HINTS):
                return True
    return None


def wellbeing_context(hass: Any) -> dict:
    """A short descriptive snapshot of the wearable readings."""
    if not is_enabled():
        return {"available": False, "summary": "", "readings": {}}
    found = discover(hass)
    if not found:
        return {"available": False,
                "summary": "no biometric entities found — connect a wearable integration to Home Assistant",
                "readings": {}}

    readings: dict[str, dict] = {}
    for kind, entries in found.items():
        for entry in entries:
            value = str(entry["value"]).strip()
            if value.lower() not in _BLANK:
                readings[kind] = {"value": value, "unit": entry["unit"]}
                break

    parts = []
    if "heart_rate" in readings:
        parts.append(f"heart rate {readings['heart_rate']['value']} bpm")
    if "sleep_stage" in readings:
        parts.append(f"sleep: {readings['sleep_stage']['value']}")
    elif "sleep_state" in readings:
        parts.append(f"sleep state: {readings['sleep_state']['value']}")
    if "steps" in readings:
        parts.append(f"{readings['steps']['value']} steps today")

    return {"available": True,
            "summary": "; ".join(parts) or "biometric data present",
            "readings": readings,
            "disclaimer": _DISCLAIMER}
