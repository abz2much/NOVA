"""Adaptive awareness (opt in): anticipation alerts learn from how they land.

Nova's anticipation alerts ("the porch door usually opens by now", "usually home
by now", routine and departure nudges) each leave a Decision Record. When the
user mutes or ignores something right after one of those alerts, the matching
record is judged "unnecessary". An alert left unmuted for a day counts as
welcome, as does one judged "good" by hand. This module turns the recent mix
into one small, bounded adjustment:

    mostly unwelcome  -> wait a bit longer before saying "not yet" and ask for
                         more days of history before trusting a routine
    almost all welcome -> wait a little less

Design rules, shared with the adaptive suggestion bar:

  * Opt in. Off unless ``adaptive_awareness`` is true. Off means no change and
    no verdicts recorded.
  * Hysteresis. Nothing moves until enough alerts have been judged.
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
MIN_JUDGED = 5                # judged alerts needed before anything moves
IGNORE_WINDOW_S = 86400.0     # an ignore counts against alerts from the last day
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
        # An alert nobody muted within a day counts as welcome. Without this
        # the only automatic verdict is "unnecessary", so the rate could
        # only ever say "wait longer".
        settled = await hass.async_add_executor_job(
            decision_record.settled_unjudged_count, KIND_PREFIX, WINDOW_S,
            IGNORE_WINDOW_S, db_path, True)
        unwelcome = int(r.get("unnecessary", 0)) + int(r.get("wrong", 0))
        judged = int(r.get("judged", 0)) + int(settled)
        rate = round(unwelcome / judged, 4) if judged else None
        if judged >= MIN_JUDGED:
            delta = delta_from_rate(rate)
    except Exception as exc:
        _LOGGER.debug("adaptive_awareness refresh error: %s", exc)
        delta = 0.0
    _STATE.update(ts=now, delta=delta, judged=judged, unwelcome_rate=rate)
    return delta


def note_ignored(entity_pattern: str, *, db_path: Optional[str] = None) -> int:
    """The user ignored or muted something. Judge matching recent anticipation
    alerts "unnecessary". Blocking (SQLite), so call it from the executor.
    Returns how many records were judged. Does nothing when the setting is off."""
    if not enabled() or not entity_pattern:
        return 0
    try:
        from . import decision_record
        return decision_record.set_outcome_for_entity(
            entity_pattern, decision_record.OUTCOME_UNNECESSARY, "ignore",
            kind_prefix=KIND_PREFIX, max_age=IGNORE_WINDOW_S, db_path=db_path)
    except Exception as exc:
        _LOGGER.debug("adaptive_awareness note_ignored error: %s", exc)
        return 0


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
