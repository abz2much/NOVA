"""Constants and small helpers shared by the cognitive core.

Thresholds and intervals, the temperature unit helpers, the outdoor
temperature lookup, and the output language, notification text, persona and
presence aware honorific helpers.

write_json_atomic is imported here for the managers that save a file
(ignore rules, lockdown state, autonomy grants). They read it as
core_common.write_json_atomic, so a test that patches it on cognitive_core
reaches all of them.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import logging
from typing import Optional

from .persistence.files import write_json_atomic  # noqa: F401  (patch point)

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


TICK_INTERVAL = 30  # seconds between evaluations
LOCKDOWN_CHECK_INTERVAL = 300  # 5 min between lockdown scans
# Formal lockdown state
ALARM_ARMED_STATES = {
    "armed_home", "armed_away", "armed_night", "armed_vacation",
    "armed_custom_bypass",
}
LOCKDOWN_DOOR_COVER_CLASSES = {"door", "garage", "garage_door"}
LOCKDOWN_BREACH_COOLDOWN = 120  # seconds between repeat breach announcements
LOCKDOWN_SECURE_VERIFY_DELAY = 25  # seconds to wait before confirming a close actually took (slow covers)


# Locks that are NOT physical security (thermostat keypad/child locks, etc).
# Lockdown's "lock every unlocked lock" sweep and breach re-lock both skip
# the entities in the "lockdown_exempt_locks" config key (config.json). With
# the key never saved, nothing is exempt (8.28.0). The default used to name
# two thermostat locks from one particular home; an install that already
# relied on it has them written into its saved setting once (migrations.py
# v7 to v8), so its lockdown does not change.
LOCKDOWN_EXEMPT_LOCKS_DEFAULT: set = set()
# The old default, kept only for that one time migration.
LEGACY_LOCKDOWN_EXEMPT_LOCKS = ("lock.downstairs_thermo_lock", "lock.upstairs_thermo_lock")
FREEZE_WARN_TEMP_F = 35  # outdoor temp (°F) that triggers pipe concern
FREEZE_CRITICAL_TEMP_F = 20  # act immediately


def _temp_to_f(value: float, unit: str) -> float:
    """A temperature already in ``unit`` (``°C`` or ``°F``) → Fahrenheit, so
    threshold checks are unit-correct regardless of the home's unit system."""
    return value * 9.0 / 5.0 + 32.0 if unit and "C" in unit.upper() else value


def _f_to_unit(f_value: float, unit: str) -> float:
    """Fahrenheit → ``unit`` (inverse of :func:`_temp_to_f`)."""
    return (f_value - 32.0) * 5.0 / 9.0 if unit and "C" in unit.upper() else f_value


def _fmt_temp(value: float, unit: str, decimals: int = 1) -> str:
    """Render a temperature with its unit label (``18.3°C``)."""
    return f"{value:.{decimals}f}{unit or '°'}"


def discover_outdoor_temp(hass) -> Optional[tuple[float, str]]:
    """(value, unit) of the outdoor temperature: a weather.* entity if one
    exists, else a sensor whose entity_id/name says "outdoor"/"outside".
    None if nothing is found. Never raises. Shared by the freeze-risk check
    below and Sentinel's door/window-left-open temperature gate."""
    try:
        default_unit = hass.config.units.temperature_unit
    except Exception:
        default_unit = "°F"

    value = None
    unit = default_unit
    for state in hass.states.async_all("weather"):
        temp = state.attributes.get("temperature")
        if temp is not None:
            try:
                value = float(temp)
            except (ValueError, TypeError):
                continue
            unit = state.attributes.get("temperature_unit") or default_unit
            break

    if value is None:
        for state in hass.states.async_all("sensor"):
            if state.attributes.get("device_class") != "temperature":
                continue
            eid = state.entity_id.lower()
            fname = (state.attributes.get("friendly_name") or "").lower()
            if "outdoor" in eid or "outside" in eid or "outdoor" in fname:
                try:
                    value = float(state.state)
                except (ValueError, TypeError):
                    pass
                else:
                    unit = state.attributes.get("unit_of_measurement") or default_unit
                break

    if value is None:
        return None
    return value, unit


def _hass_lang(hass) -> str:
    """Nova's output language ('en' fallback), for localized safety
    notifications: the output_language setting, else Home Assistant's
    language (output_language.resolve). Only the words change; notify_i18n
    falls back to English for a language it has no templates for."""
    from . import output_language
    return output_language.resolve(hass)


def _notify_i18n():
    """Lazy import of the localized-notification table (keeps this heavy module's
    import order unaffected)."""
    from . import notify_i18n
    return notify_i18n


def _persona():
    """Lazy import of the persona voice module (same reasoning as _notify_i18n).
    A local variable named `persona` exists elsewhere in this file (an agent
    prompt field, unrelated) — routing through this helper avoids any
    ambiguity with a top-level import."""
    from . import persona
    return persona


def _live_honorific(hass) -> str:
    """Presence-aware honorific (Phase C), resolved fresh every call — never
    cache this on a long-lived object, since who's home changes over time.
    Every call site below already fetched honorific fresh from self.config
    on each tick/event (just not presence-aware), so swapping to this here
    is a straight replacement, not a new staleness risk."""
    try:
        from . import honorific as honorific_mod
        return honorific_mod.effective_honorific(hass)
    except Exception:
        return "sir"

# ── Proactive intelligence (v5.9.07) ────────────────────────────────────────
PROACTIVE_CHECK_INTERVAL = 120   # 2 min between comfort/efficiency scans
PROACTIVE_OFFER_COOLDOWN = 1800  # 30 min before re-offering the same thing
DARK_LUX_THRESHOLD = 15          # below this lux + occupancy → offer lights
STALE_LIGHT_MINUTES = 90         # light on this long in an empty room → flag


HIGH_TEMP_AWAY_F = 78            # cooling running while away → efficiency flag
LOW_TEMP_AWAY_F = 62             # heating running while away → efficiency flag

# ── Intrusion investigation (v6.33.0) ───────────────────────────────────────
# One alert, then investigate silently until it's a confirmed intrusion or
# confirmed benign — never a stream of "motion detected" repeats.
INTRUSION_SPREAD_ZONES = 2       # motion in this many zones ⇒ someone moving through
INTRUSION_INWARD_DEPTH = 2       # rooms deep from the breach motion must reach to
                                 # confirm a real inward route (v6.74.0);
                                 # configurable via intrusion_inward_depth
INTRUSION_CLEAR_QUIET_SECS = 180 # motion quiet this long ⇒ nothing of note
INTRUSION_MAX_INVESTIGATE_SECS = 600  # one zone this long, no spread ⇒ benign
INTRUSION_SUSTAINED_SECS = 60    # multi-zone motion sustained this long (no breach location) ⇒ real
# If the user doesn't respond to the initial "investigating" alert (neither
# acknowledges nor calls it off) within this window AND the situation hasn't
# cleared, escalate to a full alert anyway — an unanswered possible break-in
# should fail toward alerting, not toward silently waiting. Configurable via
# `intrusion_response_timeout` (v6.69.0).
INTRUSION_RESPONSE_TIMEOUT_SECS = 120

# ── Graduated autonomy (v5.9.07) ────────────────────────────────────────────
# A suggestion that the user approves repeatedly earns the right to auto-apply.
AUTONOMY_TRUST_THRESHOLD = 3     # approvals of same pattern → auto-execute tier
AUTONOMY_MIN_CONFIDENCE = 0.80   # confidence floor for auto-execution
