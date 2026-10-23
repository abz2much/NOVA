"""Shared settings and small helpers for the cognitive core.

This holds the timing and threshold constants, the temperature unit
conversions, the outdoor temperature lookup, and the helpers for output
language, notification text, persona and the presence aware honorific.

The managers that save a file (ignore rules, lockdown state, autonomy grants)
call write_json_atomic as core_common.write_json_atomic. That is why it is
imported here: a test that patches it once reaches every one of them.

Split out of cognitive_core.py with no changes in 8.7.17. cognitive_core.py
still re-exports each name below as the very same object.
"""
from __future__ import annotations

import logging
from typing import Optional

from .persistence.files import write_json_atomic  # noqa: F401  (patch point)

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


# ── Tick and lockdown timing ────────────────────────────────────────────────
TICK_INTERVAL = 30               # seconds between evaluations
LOCKDOWN_CHECK_INTERVAL = 300    # seconds between lockdown scans
ALARM_ARMED_STATES = {
    "armed_home", "armed_away", "armed_night", "armed_vacation",
    "armed_custom_bypass",
}
LOCKDOWN_DOOR_COVER_CLASSES = {"door", "garage", "garage_door"}
LOCKDOWN_BREACH_COOLDOWN = 120   # seconds before a breach is announced again
# Seconds to wait before checking that a close really happened. Slow covers
# need the time.
LOCKDOWN_SECURE_VERIFY_DELAY = 25

# Locks that guard nothing physical, such as a thermostat keypad or a child
# lock. The lockdown sweep that locks every unlocked lock, and the re-lock
# after a breach, both skip whatever the "lockdown_exempt_locks" key in
# config.json names. Until that key is saved nothing is exempt (8.28.0).
# The default once named two thermostat locks belonging to one home, so an
# install that depended on it had them copied into its saved setting a single
# time (migrations.py, v7 to v8) and sees no change.
LOCKDOWN_EXEMPT_LOCKS_DEFAULT: set = set()
# That old default, kept for the migration and nothing else.
LEGACY_LOCKDOWN_EXEMPT_LOCKS = ("lock.downstairs_thermo_lock", "lock.upstairs_thermo_lock")

# ── Freeze risk ─────────────────────────────────────────────────────────────
FREEZE_WARN_TEMP_F = 35          # outdoor °F that raises pipe concern
FREEZE_CRITICAL_TEMP_F = 20      # outdoor °F at which Nova acts straight away
# Once the critical alert has been spoken, the phone reminder repeats no more
# often than this (8.39.1).
FREEZE_REMINDER_SECONDS = 3 * 3600


# ── Temperature units ───────────────────────────────────────────────────────

def _is_celsius(unit) -> bool:
    return bool(unit) and "C" in unit.upper()


def _temp_to_f(value: float, unit: str) -> float:
    """Express a reading taken in ``unit`` (``°C`` or ``°F``) in Fahrenheit,
    which keeps threshold checks correct under either unit system."""
    if _is_celsius(unit):
        return value * 9.0 / 5.0 + 32.0
    return value


def _f_to_unit(f_value: float, unit: str) -> float:
    """The reverse of :func:`_temp_to_f`: Fahrenheit into ``unit``."""
    if _is_celsius(unit):
        return (f_value - 32.0) * 5.0 / 9.0
    return f_value


def _fmt_temp(value: float, unit: str, decimals: int = 1) -> str:
    """Write a temperature with its unit, for example ``18.3°C``."""
    return f"{value:.{decimals}f}{unit or '°'}"


def _weather_reading(hass, default_unit):
    """The first usable temperature on a weather entity, as (value, unit)."""
    for state in hass.states.async_all("weather"):
        raw = state.attributes.get("temperature")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (ValueError, TypeError):
            continue
        return value, state.attributes.get("temperature_unit") or default_unit
    return None


def _outdoor_sensor_reading(hass, default_unit):
    """Read the first temperature sensor that looks like an outdoor one and has
    a numeric state. A matching sensor with no reading is skipped."""
    for state in hass.states.async_all("sensor"):
        if state.attributes.get("device_class") != "temperature":
            continue
        entity_id = state.entity_id.lower()
        name = (state.attributes.get("friendly_name") or "").lower()
        if not ("outdoor" in entity_id or "outside" in entity_id or "outdoor" in name):
            continue
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            continue
        return value, state.attributes.get("unit_of_measurement") or default_unit
    return None


def discover_outdoor_temp(hass) -> Optional[tuple[float, str]]:
    """The outdoor temperature as (value, unit), or None when none is found.

    A weather.* entity is tried first. After that comes a temperature sensor
    whose entity_id or name says "outdoor" or "outside". Never raises. The
    freeze-risk check and Sentinel's door and window left-open temperature
    gate both use it."""
    try:
        default_unit = hass.config.units.temperature_unit
    except Exception:
        default_unit = "°F"
    return (_weather_reading(hass, default_unit)
            or _outdoor_sensor_reading(hass, default_unit))


# ── Helpers that import their module on first use ───────────────────────────

def _hass_lang(hass) -> str:
    """The language Nova writes safety notifications in: the output_language
    setting, otherwise Home Assistant's own language (output_language.resolve).
    Only the wording changes. notify_i18n answers in English for any language
    it has no templates for."""
    from . import output_language
    return output_language.resolve(hass)


def _notify_i18n():
    """The localized notification table, imported when first needed so this
    module's own import order does not change."""
    from . import notify_i18n
    return notify_i18n


def _persona():
    """The persona voice module, imported when first needed. A local called
    `persona` also exists elsewhere in this file's callers (a field of the
    agent prompt, unrelated), so going through a helper avoids any mix-up with
    a top-level import."""
    from . import persona
    return persona


def _live_honorific(hass) -> str:
    """The honorific for whoever is home right now (Phase C).

    Looked up on every call and never kept on a long-lived object, because who
    is home keeps changing. Callers already read the honorific fresh from
    self.config on each tick or event, just without the presence check, so
    this replaces that read and adds no new risk of stale data."""
    try:
        from . import honorific as honorific_mod
        return honorific_mod.effective_honorific(hass)
    except Exception:
        return "sir"


# ── Proactive intelligence (v5.9.07) ────────────────────────────────────────
PROACTIVE_CHECK_INTERVAL = 120   # seconds between comfort and efficiency scans
PROACTIVE_OFFER_COOLDOWN = 1800  # seconds before the same offer is made again
DARK_LUX_THRESHOLD = 15          # under this lux with someone present: offer lights
STALE_LIGHT_MINUTES = 90         # a light left on this long in an empty room: flag it
HIGH_TEMP_AWAY_F = 78            # cooling while nobody is home: efficiency flag
LOW_TEMP_AWAY_F = 62             # heating while nobody is home: efficiency flag

# ── Intrusion investigation (v6.33.0) ───────────────────────────────────────
# Nova sends one alert and then investigates quietly until the event is
# confirmed as an intrusion or as harmless. It never repeats "motion
# detected".
INTRUSION_SPREAD_ZONES = 2       # zones with motion that mean someone is moving through
# How many rooms in from the breach the motion must travel to count as a real
# inward route (v6.74.0). Set with intrusion_inward_depth.
INTRUSION_INWARD_DEPTH = 2
INTRUSION_CLEAR_QUIET_SECS = 180      # this long without motion: nothing of note
INTRUSION_MAX_INVESTIGATE_SECS = 600  # one zone this long with no spread: harmless
# Multi-zone motion lasting this long with no known breach point counts as real.
INTRUSION_SUSTAINED_SECS = 60
# The first "investigating" alert may get no answer, neither an acknowledgement
# nor a call-off. If that lasts this long and things have not cleared, Nova
# escalates to a full alert. A possible break-in nobody answered should end in
# an alert, not in silence. Set with `intrusion_response_timeout` (v6.69.0).
INTRUSION_RESPONSE_TIMEOUT_SECS = 120
