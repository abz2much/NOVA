"""Adaptive awareness (opt in): anticipation alerts learn from your ratings.

Nova's anticipation alerts ("the porch door usually opens by now", "usually home
by now", routine and departure nudges) each leave a Decision Record. Nova only
learns from what the user confirms: each of those alerts sends a phone
notification with Helpful and Not helpful buttons, and the panel's decision
browser can mark it too. Silence, muting and timing are never read as a verdict.
This module turns the recent mix of verdicts into one small, bounded
adjustment:

    mostly unwelcome  -> wait a bit longer before saying "not yet" and ask for
                         more days of history before trusting a routine
    almost all welcome -> wait a little less

Design rules, shared with the adaptive suggestion bar:

  * Opt in. Off unless ``adaptive_awareness`` is true. Off means no change, no
    rating buttons and no verdicts recorded.
  * Confirmations only. A verdict comes from a button tap or the panel.
  * Hysteresis. Nothing moves until enough alerts have been rated.
  * Bounded. The delta is small and the derived values are clamped.
  * Cached. The database read runs off the event loop in ``async_refresh``;
    everything the alert code calls is cache only.
  * Narrow. It reads only anticipation records. It never touches intrusion,
    lockdown, hazard or any other safety decision.
  * Never raises. A failure means "no adjustment".
"""
from __future__ import annotations

import logging
import time
from typing import Optional

_LOGGER = logging.getLogger(__name__)

KEY = "adaptive_awareness"
KIND_PREFIX = "anticipation"
WINDOW_S = 30 * 86400.0       # look back a month
MIN_JUDGED = 5                # rated alerts needed before anything moves
ACTION_PREFIX = "NOVA_AWARE_"  # phone action ids: NOVA_AWARE_GOOD_<id> / _BAD_<id>
RATING_TITLE = "Was this helpful?"
RATING_CHANNEL = "Nova ratings"  # Android: a low importance channel, so no sound
_CACHE_TTL = 300.0
_TOL_SCALE_RANGE = (0.8, 1.45)
_MAX_EXTRA_DAYS = 3

_STATE = {"ts": 0.0, "delta": 0.0, "judged": 0, "unwelcome_rate": None}


def delta_from_rate(unwelcome_rate) -> float:
    """Same mapping the suggestion bar uses (see decision_record)."""
    from . import decision_record
    return decision_record.threshold_delta_from_rate(unwelcome_rate)


def enabled() -> bool:
    try:
        from . import nova_config
        return nova_config.get(KEY, False) is True
    except Exception:
        return False


def current_delta() -> float:
    """Cache only and non blocking. 0.0 when off or not yet refreshed."""
    return float(_STATE["delta"]) if enabled() else 0.0


def tolerance_scale() -> float:
    """Multiplier for the "not yet" grace period, in [0.8, 1.45]."""
    lo, hi = _TOL_SCALE_RANGE
    return min(hi, max(lo, 1.0 + 3.0 * current_delta()))


def extra_min_days() -> int:
    """Extra days of history required before a routine is trusted, in [0, 3].
    Never lowers the baseline."""
    return min(_MAX_EXTRA_DAYS, max(0, round(current_delta() * 20)))


async def async_refresh(hass, *, db_path: Optional[str] = None) -> float:
    """Recompute and cache the delta. The SQLite read runs in the executor and
    the call throttles itself to the cache TTL."""
    if not enabled():
        _STATE.update(ts=time.time(), delta=0.0, judged=0, unwelcome_rate=None)
        return 0.0
    now = time.time()
    if now - _STATE["ts"] < _CACHE_TTL:
        return float(_STATE["delta"])
    delta, judged, rate = 0.0, 0, None
    try:
        from . import decision_record
        r = await hass.async_add_executor_job(
            decision_record.outcome_rate, KIND_PREFIX, WINDOW_S, db_path, True)
        judged = int(r.get("judged", 0))
        rate = r.get("unwelcome_rate")
        if judged >= MIN_JUDGED:
            delta = delta_from_rate(rate)
    except Exception as exc:
        _LOGGER.debug("adaptive_awareness refresh error: %s", exc)
        delta = 0.0
    _STATE.update(ts=now, delta=delta, judged=judged, unwelcome_rate=rate)
    return delta


def rating_actions(decision_id) -> list:
    """The Helpful / Not helpful buttons for one alert's phone notification.
    Empty when the setting is off or the alert has no Decision Record."""
    if decision_id is None or not enabled():
        return []
    try:
        rid = int(decision_id)
    except (TypeError, ValueError):
        return []
    return [{"action": f"{ACTION_PREFIX}GOOD_{rid}", "title": "Helpful"},
            {"action": f"{ACTION_PREFIX}BAD_{rid}", "title": "Not helpful"}]


def parse_action(action) -> Optional[tuple]:
    """(record id, helpful) for one of our button ids, else None."""
    text = str(action or "")
    if not text.startswith(ACTION_PREFIX):
        return None
    verdict, _, rid = text[len(ACTION_PREFIX):].partition("_")
    if verdict not in ("GOOD", "BAD") or not rid.isdigit():
        return None
    return int(rid), verdict == "GOOD"


def record_rating(record_id: int, helpful: bool, *,
                  db_path: Optional[str] = None) -> bool:
    """Store the user's rating of one anticipation alert. Only anticipation
    records can be rated this way, and the first verdict stands. Blocking
    (SQLite), so call it from the executor. Does nothing when off."""
    if not enabled():
        return False
    try:
        from . import decision_record
        rec = decision_record.get(int(record_id), db_path=db_path)
        if not rec or not str(rec.get("kind") or "").startswith(KIND_PREFIX):
            return False
        verdict = (decision_record.OUTCOME_GOOD if helpful
                   else decision_record.OUTCOME_UNNECESSARY)
        return decision_record.set_outcome(
            int(record_id), verdict, "phone", db_path=db_path)
    except Exception as exc:
        _LOGGER.debug("adaptive_awareness record_rating error: %s", exc)
        return False


def async_listen(hass):
    """Listen for rating button taps. Returns the unsubscribe callable."""
    from homeassistant.core import callback

    @callback
    def _on_action(event) -> None:
        parsed = parse_action(event.data.get("action"))
        if parsed is None or not enabled():
            return
        rid, helpful = parsed

        async def _store() -> None:
            if await hass.async_add_executor_job(record_rating, rid, helpful):
                _STATE["ts"] = 0.0          # recompute on the next refresh
        hass.async_create_task(_store())

    return hass.bus.async_listen("mobile_app_notification_action", _on_action)


def rating_push_data(decision_id) -> dict:
    """The notification ``data`` block that carries the rating buttons, or
    {} when there is nothing to rate."""
    actions = rating_actions(decision_id)
    if not actions:
        return {}
    return {"actions": actions}


async def async_send_rating_prompt(hass, config, message: str, decision_id) -> bool:
    """For an alert that was spoken: a silent phone notification with the
    rating buttons. No sound on iOS (passive) or Android (low importance
    channel). Returns whether anything was sent."""
    data = rating_push_data(decision_id)
    if not data:
        return False
    data.update({"push": {"interruption-level": "passive"},
                 "channel": RATING_CHANNEL, "importance": "low"})
    try:
        from .notify_targets import async_send_configured_notifications
        sent = await async_send_configured_notifications(
            hass, config,
            {"title": RATING_TITLE, "message": str(message or ""), "data": data},
            action="rating_prompt", source="proactive")
        return bool(sent)
    except Exception as exc:
        _LOGGER.debug("adaptive_awareness rating prompt failed: %s", exc)
        return False


def status() -> dict:
    """Snapshot for the panel."""
    return {
        "enabled": enabled(),
        "delta": round(current_delta(), 3),
        "judged": int(_STATE["judged"]),
        "unwelcome_rate": _STATE["unwelcome_rate"],
        "tolerance_scale": round(tolerance_scale(), 2),
        "extra_min_days": extra_min_days(),
    }


def reset() -> None:
    """Clear the cache (used by tests)."""
    _STATE.update(ts=0.0, delta=0.0, judged=0, unwelcome_rate=None)
