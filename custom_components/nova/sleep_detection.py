"""
Nova — Sleep detection (v5.7.00, area-driven).

Determines whether Username is asleep or napping. Rules (simple and explainable):

  SLEEPING =
    (manual nap service still active)
    OR
    (explicit sleep-state override set to "asleep", not yet expired)
    OR, absent an override,
    (wearable sleep signal, if configured)
    OR
    (occupancy in any bedroom-flagged area) AND (in quiet hours)

User configures bedroom areas via a per-area toggle in the Nova options flow.
No ML, no opaque classifier.

Integrates with:
  - bedroom_areas: list of HA area_ids user flagged as bedrooms
  - observer_quiet_start / _end: the nightly quiet hours window
  - nova.nap service: manual, short-lived override
  - sleep-state override (v7.86.0): an explicit Auto/Awake/Asleep choice —
    set directly (panel dropdown) or via `maybe_prompt_sleep`'s actionable
    "Heading to bed?" notification — that beats the occupancy heuristic
    entirely until the next quiet-hours end, when it reverts to Auto on its
    own. This exists because occupancy is house-wide: one person going to bed
    otherwise marks the whole house "asleep" even while someone else is still
    up, which is exactly the false-positive this override is for.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, time, timedelta
from typing import Iterable, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from . import nova_config
from .audio_routing import is_any_bedroom_occupied

_LOGGER = logging.getLogger(__name__)

# How long a "Heading to bed?" notification stays answerable before Nova
# stops listening for a tap. Generous — this isn't a blocking confirmation,
# it's a one-shot nightly prompt.
_SLEEP_PROMPT_LISTEN_TIMEOUT = 30 * 60  # 30 minutes

# Module-level: which local date the nightly prompt was last sent on, so a
# 10-minute scheduler tick doesn't re-send it all evening. Resets naturally
# on integration reload/restart, same as _NAP_UNTIL below.
_PROMPTED_DATE: Optional[str] = None


# Module-level manual nap override
_NAP_UNTIL: Optional[datetime] = None


def set_nap(duration_minutes: int = 30) -> None:
    """Called by nova.nap service — sets manual mute for N minutes."""
    global _NAP_UNTIL
    _NAP_UNTIL = dt_util.utcnow() + timedelta(minutes=duration_minutes)
    _LOGGER.info("Nova: manual nap for %d min until %s", duration_minutes, _NAP_UNTIL)


def clear_nap() -> None:
    """Explicit wake — cancels manual nap override."""
    global _NAP_UNTIL
    _NAP_UNTIL = None
    _LOGGER.info("Nova: manual nap cleared")


def _next_quiet_end(quiet_end: str) -> datetime:
    """The next instant the given local quiet_end (HH:MM) occurs — today's, or
    tomorrow's if today's has already passed. Used to expire the sleep-state
    override automatically, so an "Asleep"/"Awake" choice can't linger and
    skew the following day. Returned tz-aware in the local zone; comparing
    aware datetimes works correctly regardless of zone, so callers don't need
    to convert this to UTC first."""
    try:
        end = time.fromisoformat(quiet_end)
    except (ValueError, TypeError):
        end = time(7, 0)
    now_local = dt_util.now()
    candidate = datetime.combine(now_local.date(), end, tzinfo=now_local.tzinfo)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def _read_override() -> tuple[str, Optional[datetime]]:
    """(value, expires_utc). value is 'auto' unless an unexpired 'awake' or
    'asleep' override is on record."""
    value = nova_config.get("sleep_override", "auto") or "auto"
    if value not in ("awake", "asleep"):
        return "auto", None
    exp_iso = nova_config.get("sleep_override_expires")
    if not exp_iso:
        return "auto", None
    try:
        expires = datetime.fromisoformat(exp_iso)
    except (ValueError, TypeError):
        return "auto", None
    if dt_util.utcnow() >= expires:
        return "auto", None
    return value, expires


def current_override() -> str:
    """Effective sleep_override value ('auto'/'awake'/'asleep'), accounting
    for expiry. For display (the panel dropdown) — reading the raw stored
    key directly would keep showing 'Asleep'/'Awake' forever after it
    expires, since nothing rewrites the stored value on expiry; only this
    computed read reflects the auto-revert."""
    value, _ = _read_override()
    return value


def set_override(value: str, quiet_end: str = "07:00") -> None:
    """Explicitly set sleep state: 'auto' clears any override; 'awake' or
    'asleep' holds until the next quiet-hours end, then reverts to Auto on
    its own. Called from the panel dropdown, or by `maybe_prompt_sleep`'s
    notification response."""
    if value not in ("auto", "awake", "asleep"):
        raise ValueError(f"invalid sleep override: {value!r}")
    if value == "auto":
        nova_config.set("sleep_override", "auto")
        nova_config.set("sleep_override_expires", None)
        _LOGGER.info("Nova: sleep-state override cleared (Auto)")
        return
    nova_config.set("sleep_override", value)
    nova_config.set("sleep_override_expires", _next_quiet_end(quiet_end).isoformat())
    _LOGGER.info("Nova: sleep-state override set to %s until %s", value, quiet_end)


def _in_quiet_hours(quiet_start: str, quiet_end: str) -> bool:
    """Check if current local time is within the configured quiet window.
    Handles windows that cross midnight (e.g. 22:00 → 07:00)."""
    try:
        start = time.fromisoformat(quiet_start)
        end   = time.fromisoformat(quiet_end)
    except (ValueError, TypeError):
        _LOGGER.warning("invalid quiet hours '%s' -> '%s'", quiet_start, quiet_end)
        return False
    now_local = dt_util.now().time()
    if start <= end:
        return start <= now_local < end
    return now_local >= start or now_local < end


def is_sleeping(
    hass: HomeAssistant,
    *,
    bedroom_area_ids: Iterable[str] = (),
    quiet_start: str = "22:00",
    quiet_end: str = "07:00",
) -> tuple[bool, str]:
    """
    Return (sleeping, reason_string).

    Priority:
      1. Manual nap override (highest)
      2. Explicit sleep-state override (Awake/Asleep, not yet expired)
      3. Wearable sleep signal
      4. Bedroom occupancy during quiet hours
      5. Otherwise not sleeping
    """
    global _NAP_UNTIL

    # Manual nap check
    if _NAP_UNTIL is not None:
        if dt_util.utcnow() < _NAP_UNTIL:
            minutes_left = (_NAP_UNTIL - dt_util.utcnow()).total_seconds() / 60
            return True, f"manual nap ({minutes_left:.0f}min remaining)"
        _NAP_UNTIL = None

    # Explicit override (v7.86.0) — a deliberate answer (panel dropdown, or
    # the "Heading to bed?" prompt) beats every inferred signal below. This is
    # what stops one person going to bed from marking the WHOLE house asleep
    # while someone else is still up: they just say so.
    override, expires = _read_override()
    if override == "asleep":
        return True, "set to Asleep"
    if override == "awake":
        return False, "set to Awake"

    # Wearable sleep signal (v6.63.0) — if a biometric sleep entity is present
    # and enabled, it's stronger evidence than bedroom occupancy. Opt-in; returns
    # None when unavailable, so this is a no-op on systems without a wearable.
    try:
        from . import biometrics
        sig = biometrics.sleep_signal(hass)
        if sig is True:
            return True, "wearable reports asleep"
        if sig is False and not (bedroom_area_ids and _in_quiet_hours(quiet_start, quiet_end)):
            # wearable says awake and no strong occupancy signal → trust awake
            return False, "wearable reports awake"
    except Exception:
        pass

    # Bedroom + quiet hours
    if bedroom_area_ids and _in_quiet_hours(quiet_start, quiet_end):
        occupied, area_id = is_any_bedroom_occupied(hass, bedroom_area_ids)
        if occupied:
            return True, f"bedroom ({area_id}) occupied during quiet hours"

    return False, "awake"


def should_suppress(
    hass: HomeAssistant,
    *,
    urgency: str,
    bedroom_area_ids: Iterable[str] = (),
    quiet_start: str = "22:00",
    quiet_end: str = "07:00",
) -> tuple[bool, str]:
    """
    Should this announcement be suppressed due to sleep state?

    Critical NEVER suppresses (safety override).
    High:   audio suppressed, caller may still send push notification.
    Medium/Low: always suppressed when sleeping.
    """
    if urgency == "critical":
        return False, "critical urgency overrides sleep"

    sleeping, reason = is_sleeping(
        hass,
        bedroom_area_ids=bedroom_area_ids,
        quiet_start=quiet_start,
        quiet_end=quiet_end,
    )
    if sleeping:
        return True, reason
    return False, "awake"


def _prompt_eligible(hass: HomeAssistant, config: dict) -> bool:
    """Should `maybe_prompt_sleep` send tonight's prompt right now?"""
    if not config.get("sleep_prompt_enabled", True):
        return False
    override, _ = _read_override()
    if override != "auto":
        return False  # already explicitly set for tonight
    today = dt_util.now().date().isoformat()
    if _PROMPTED_DATE == today:
        return False  # already asked tonight
    prompt_time_s = config.get("sleep_prompt_time", "23:00")
    try:
        prompt_time = time.fromisoformat(prompt_time_s)
    except (ValueError, TypeError):
        prompt_time = time(23, 0)
    if dt_util.now().time() < prompt_time:
        return False
    tv_entity = config.get("movie_media_player") or ""
    if tv_entity:
        st = hass.states.get(tv_entity)
        if st is not None and st.state != "off":
            return False  # TV still on — not a good moment to ask
    return True


async def maybe_prompt_sleep(hass: HomeAssistant, config: dict) -> None:
    """Once per night, past a configurable time (default 23:00) with the TV
    off, ask via an actionable phone notification whether the household is
    going to sleep (v7.86.0). Yes → explicit "asleep" override until
    quiet-hours end. No → explicit "awake" override until quiet-hours end
    (a deliberate "not yet" should hold, not fall back to an occupancy
    heuristic that could still flip to asleep from someone else's bedroom).
    No answer at all → stays on Auto, i.e. today's occupancy heuristic.

    Intended to be called from a periodic scheduler tick; no-ops silently
    when it isn't yet time, already asked tonight, or already overridden."""
    global _PROMPTED_DATE
    if not _prompt_eligible(hass, config):
        return
    # Mark asked BEFORE sending, so a slow/retried notify call can't
    # double-fire this on the next tick.
    _PROMPTED_DATE = dt_util.now().date().isoformat()
    quiet_end = config.get("observer_quiet_end", "07:00")
    await _send_sleep_prompt(hass, quiet_end, config)


async def _send_sleep_prompt(
    hass: HomeAssistant, quiet_end: str, config: dict,
) -> None:
    """Push an actionable Yes/No notification to configured normal targets."""
    req_id = uuid.uuid4().hex[:8]
    yes_action = f"NOVA_SLEEP_YES_{req_id}"
    no_action = f"NOVA_SLEEP_NO_{req_id}"

    from .notify_targets import async_send_configured_notifications
    sent = await async_send_configured_notifications(
        hass,
        config,
        {
            "title": "Nova",
            "message": "Heading to bed?",
            "data": {
                "actions": [
                    {"action": yes_action, "title": "Yes"},
                    {"action": no_action, "title": "No"},
                ],
                "push": {"interruption-level": "active"},
            },
        },
        action="sleep_prompt",
        source="proactive",
    )
    if not sent:
        return

    @callback
    def _on_action(event) -> None:
        action = event.data.get("action")
        if action == yes_action:
            set_override("asleep", quiet_end=quiet_end)
            _unsub()
        elif action == no_action:
            set_override("awake", quiet_end=quiet_end)
            _unsub()

    _unsub = hass.bus.async_listen("mobile_app_notification_action", _on_action)

    @callback
    def _expire(_now) -> None:
        # An unanswered prompt just stops listening — override stays Auto.
        _unsub()

    from homeassistant.helpers.event import async_call_later
    async_call_later(hass, _SLEEP_PROMPT_LISTEN_TIMEOUT, _expire)
