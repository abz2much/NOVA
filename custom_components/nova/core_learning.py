"""Where pattern learning starts.

Four jobs live here: deciding whether an entity's changes may be logged at
all (the opt in toggles and a per entity rate limit), logging commands and
camera events, importing recorder history for Analyze Now, and running that
analysis on demand. cognitive_core.py re-exports every name below as the same
object, so older imports keep working.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.core import HomeAssistant, callback

from . import core_state as _m_state
from .core_pattern_store import META_DOMAINS, NOISY_DOMAINS

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")

# Binary sensor classes each learning toggle covers.
_OPENING_CLASSES = ("door", "window", "garage_door", "opening")
_MOTION_CLASSES = ("motion", "occupancy", "presence", "moving")

# Classes that pulse quickly get a much longer gap between logged changes, so
# a motion sensor marks "it fired around now" instead of flooding the store.
_HIGH_FREQ_CLASSES = {"motion", "occupancy", "presence", "moving", "vibration", "sound"}

# entity_id -> epoch of its last logged change (the rate limit's memory).
_PATTERN_LOG_LAST: dict = {}

# History import limits: entities per run, kept changes per entity, rows overall.
_BACKFILL_MAX_ENTITIES = 250
_BACKFILL_PER_ENTITY = 1000
_BACKFILL_MAX_ROWS = 20000
_SKIPPED_STATES = ("unavailable", "unknown")

_NOT_ENOUGH_HISTORY = ("Not enough history yet — pattern learning needs about a "
                       "week of data (≥ 7 days and ≥ 50 recorded changes).")


@callback
def _pattern_opted_in(entity_id: str, device_class: str = "") -> bool:
    """True when the user chose to learn from an entity that is normally too
    noisy: listed by name, or covered by one of the group toggles (doors and
    windows, motion, presence, buttons). Each toggle is checked on its own,
    so turning on doors never hides motion (8.49.37). Everything is off by
    default."""
    try:
        from . import nova_config
        if entity_id in (nova_config.get("pattern_include_entities", []) or []):
            return True
        domain = entity_id.split(".")[0]
        if domain == "binary_sensor":
            if device_class in _OPENING_CLASSES and nova_config.get("pattern_learn_doors", False):
                return True
            return bool(device_class in _MOTION_CLASSES
                        and nova_config.get("pattern_learn_motion", False))
        if domain in ("device_tracker", "person"):
            return bool(nova_config.get("pattern_learn_presence", False))
        if domain == "event":
            # Remote and button presses arrive as event entities.
            return bool(nova_config.get("pattern_learn_buttons", False))
    except Exception:
        pass
    return False


def _config_seconds(key: str, default: float) -> float:
    try:
        return float((_m_state._CORE.config or {}).get(key, default))
    except Exception:
        return default


def _pattern_log_interval(device_class: str = "") -> float:
    """Smallest gap in seconds between two logged changes of one entity.

    The base gap is ``pattern_log_min_interval`` (60 by default, 0 turns it
    off). The fast pulsing classes use ``pattern_motion_min_interval`` (300 by
    default) whenever that is the larger of the two."""
    base = max(0.0, _config_seconds("pattern_log_min_interval", 60.0))
    if device_class not in _HIGH_FREQ_CLASSES:
        return base
    return max(base, _config_seconds("pattern_motion_min_interval", 300.0))


def _pattern_rate_ok(entity_id: str, now: float, device_class: str = "") -> bool:
    """Whether this entity may be logged at ``now``. A yes is remembered, so
    a burst of changes leaves one row per interval."""
    previous = _PATTERN_LOG_LAST.get(entity_id, 0.0)
    if now - previous < _pattern_log_interval(device_class):
        return False
    _PATTERN_LOG_LAST[entity_id] = now
    return True


def log_command(text: str, handled_by: str = "agent",
                entity_ids: list = None, person: str = "unknown"):
    """Store a spoken or typed command for pattern learning."""
    store = _m_state._CORE.state_logger
    if store:
        store.log_command(text, handled_by, entity_ids, person)


def learning_active() -> bool:
    """Whether pattern learning is on right now. The state logger exists only
    between the core's start() and stop(), so this is the one switch every
    learner (camera_semantic.py included) checks."""
    core = _m_state._CORE
    return bool(core.running and core.state_logger)


def log_camera_event(entity_id: str, new_state: str, area_id: str = "",
                      person: str = "unknown", person_confidence: float = 0.0,
                      detection_confidence: Optional[float] = None) -> bool:
    """Store a camera event as a state change, in the same table as every
    other entity, so routines can use it.

    ``entity_id`` is a made up camera_event.<place> id that controls nothing;
    ``new_state`` is the event label (for parcels, the delivered, stranded,
    taken or mail state). Nothing is stored, and False comes back, while
    learning is off. This writes to SQLite, so call it from the executor."""
    if not learning_active():
        return False
    _m_state._CORE.state_logger.log_state_change(
        entity_id, "", new_state, area_id,
        triggered_by="camera", person=person,
        person_confidence=float(person_confidence or 0.0),
        detection_confidence=detection_confidence,
    )
    return True


# ── history import for Analyze Now ──────────────────────────────────────────

def _backfill_filter_states(events: list, interval: float, cap: int = 1000) -> list:
    """Thin out one entity's ``(epoch, state)`` history, oldest first, the way
    live logging would have: unreadable states and repeats go, changes closer
    than ``interval`` to the last kept one go, and only the newest ``cap``
    stay. A month of a busy motion sensor therefore stays small."""
    kept: deque = deque(maxlen=max(1, cap))
    previous_state = None
    last_kept = None
    for epoch, state in events:
        if state is None or state in _SKIPPED_STATES or state == previous_state:
            continue
        previous_state = state
        if last_kept is not None and epoch - last_kept < interval:
            continue
        kept.append((epoch, state))
        last_kept = epoch
    return list(kept)


def _learnable(entity_id: str, device_class: str) -> bool:
    """The live logger's domain rules (core_pattern_store.py), so history
    brings in exactly what live logging would have stored (8.49.37)."""
    domain = entity_id.split(".")[0]
    if domain in META_DOMAINS:
        return False
    return domain not in NOISY_DOMAINS or _pattern_opted_in(entity_id, device_class)


def _row_time_and_state(row):
    """(when, state) from a recorder row: a State for the first entry, a dict
    for the rest."""
    if isinstance(row, dict):
        return row.get("last_changed") or row.get("last_updated"), row.get("state")
    state = getattr(row, "state", None)
    when = getattr(row, "last_changed", None) or getattr(row, "last_updated", None)
    return when, state


def _history_events(rows, recorder_epoch) -> list:
    events = []
    for row in rows:
        try:
            when, state = _row_time_and_state(row)
            if state is None or when is None:
                continue
            epoch = recorder_epoch(when)
        except Exception:
            continue
        if epoch is not None:
            events.append((epoch, state))
    return events


async def backfill_from_history(hass: HomeAssistant, days: int = 30) -> dict:
    """Bring recent recorder history into the pattern store for entities Nova
    has no rows for yet (a sensor just opted in, say), so Analyze Now can see
    past behaviour. It follows live logging's domain rules and rate limits,
    skips any entity that already has rows so nothing is counted twice, and is
    capped per entity and overall."""
    result = {"imported": 0, "entities": 0, "considered": 0}
    store = _m_state._CORE.state_logger
    if not store:
        return result
    try:
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.util import dt as dt_util
        from .automation.recorder_time import recorder_epoch
    except Exception:
        return result

    classes: dict = {}
    wanted: list = []
    try:
        for state in hass.states.async_all():
            device_class = state.attributes.get("device_class") or ""
            classes[state.entity_id] = device_class
            if _learnable(state.entity_id, device_class):
                wanted.append(state.entity_id)
    except Exception:
        return result
    if not wanted:
        return result

    logged = await hass.async_add_executor_job(store.distinct_logged_entities)
    todo = [eid for eid in wanted if eid not in logged][:_BACKFILL_MAX_ENTITIES]
    result["considered"] = len(todo)
    if not todo:
        return result

    try:
        days = max(1, min(int(days), 30))
    except Exception:
        days = 30
    end = dt_util.utcnow()
    start = end - timedelta(days=days)

    try:
        found = await get_instance(hass).async_add_executor_job(
            lambda: history.get_significant_states(
                hass, start, end, todo, minimal_response=True, no_attributes=True))
    except Exception as exc:
        _LOGGER.debug("Nova backfill: history fetch failed: %s", exc)
        return result
    if not found:
        return result

    rows: list = []
    for eid, entries in found.items():
        domain = eid.split(".")[0]
        kept = _backfill_filter_states(
            _history_events(entries, recorder_epoch),
            _pattern_log_interval(classes.get(eid, "")), _BACKFILL_PER_ENTITY)
        if kept:
            result["entities"] += 1
        rows.extend((datetime.fromtimestamp(epoch).isoformat(), eid, domain, "unknown", state)
                    for epoch, state in kept)
        if len(rows) >= _BACKFILL_MAX_ROWS:
            break

    result["imported"] = await hass.async_add_executor_job(store.bulk_insert_history, rows)
    return result


# ── Analyze Now ─────────────────────────────────────────────────────────────

async def _logged_totals(hass: HomeAssistant) -> dict:
    store = _m_state._CORE.state_logger
    if not store:
        return {}
    try:
        return await hass.async_add_executor_job(store.get_pattern_stats)
    except Exception:
        return {}


async def run_analysis_now(hass: HomeAssistant) -> dict:
    """Run pattern analysis now, skipping only the six hour wait between runs.

    The enough data check still applies (7 days and 50 changes), so a new
    install gets a reason rather than noise. History is imported first. The
    result says whether it ran and why not, or what it found."""
    from .automation.patterns import get_analyzer, set_thresholds
    from .core_runtime import _analysis_thresholds

    analyzer = get_analyzer()
    analyzer._last_analysis = 0.0

    try:
        imported = await backfill_from_history(hass, days=30)
    except Exception:
        imported = {}
    totals = await _logged_totals(hass)
    changes = totals.get("state_changes", 0)
    days_seen = totals.get("days_of_data", 0)

    if not await hass.async_add_executor_job(analyzer.should_analyze):
        return {"ran": False, "reason": _NOT_ENOUGH_HISTORY,
                "days_of_data": days_seen, "state_changes": changes,
                "backfill": imported}

    set_thresholds(*_analysis_thresholds(_m_state._CORE.config or {}))
    from . import suggestion_review
    reviewer = await suggestion_review.reviewer_for(hass)
    if reviewer is None:
        patterns = await analyzer.analyze(hass)
    else:
        patterns = await analyzer.analyze(hass, reviewer=reviewer)
    from . import nova_state
    nova_state.notify()   # suggestions may have been added or retired

    result = dict(analyzer._last_result)
    result["ran"] = True
    result.setdefault("patterns_found", len(patterns))
    result.update(state_changes=changes, days_of_data=days_seen, backfill=imported)
    try:
        result["diagnostic"] = await hass.async_add_executor_job(analyzer.pattern_diagnostic)
    except Exception:
        result["diagnostic"] = {}
    return result
