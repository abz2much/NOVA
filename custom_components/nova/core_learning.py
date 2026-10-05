"""Pattern learning entry points: the per entity logging gate, command and
camera event logging, the history backfill and the manual analysis run.

Moved out of cognitive_core.py unchanged in 8.7.17. cognitive_core.py
still exports every name defined here, as the same object.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from homeassistant.core import callback, HomeAssistant

from . import core_state as _m_state

_LOGGER = logging.getLogger(f"{__package__}.cognitive_core")


@callback
def _pattern_opted_in(entity_id: str, device_class: str = "") -> bool:
    """Whether a normally-excluded entity (door/window, presence) should still be
    learned for routines — via a per-entity opt-in or a domain group toggle
    (v7.11.0). Default is off; noisy domains stay filtered unless chosen."""
    try:
        from . import nova_config
    except Exception:
        return False
    try:
        incl = nova_config.get("pattern_include_entities", []) or []
        if entity_id in incl:
            return True
        domain = entity_id.split(".")[0]
        if domain == "binary_sensor" and nova_config.get("pattern_learn_doors", False):
            return device_class in ("door", "window", "garage_door", "opening")
        if domain == "binary_sensor" and nova_config.get("pattern_learn_motion", False):
            # Motion/occupancy triggers — learnable for "when X, do Y" automations
            # (motion → light). Chatty, so the class-aware rate limit below caps
            # them hard; opt-in and off by default.
            return device_class in ("motion", "occupancy", "presence", "moving")
        if domain in ("device_tracker", "person") and nova_config.get("pattern_learn_presence", False):
            return True
        if domain == "event" and nova_config.get("pattern_learn_buttons", False):
            # Button/remote presses surface as event.* entities; learnable for
            # "press → scene/action" automations. Opt-in, off by default.
            return True
    except Exception:
        pass
    return False


# Per-entity rate limit for pattern logging (v7.67.0): entity_id -> last logged
# epoch. Caps a flapping / high-frequency actuator from flooding the store.
_PATTERN_LOG_LAST: dict = {}

# Device classes that pulse rapidly — capped far harder than the base interval
# (see _pattern_log_interval) so re-including them as triggers can't re-flood.
_HIGH_FREQ_CLASSES = {"motion", "occupancy", "presence", "moving", "vibration", "sound"}


def _pattern_log_interval(device_class: str = "") -> float:
    """Minimum seconds between logged pattern changes for the SAME entity.

    Base is configurable via ``pattern_log_min_interval`` (0 disables). High-
    frequency trigger classes (motion/occupancy/presence) use a much larger
    floor (``pattern_motion_min_interval``, default 5 min) so re-including them
    as triggers logs at most one "it fired in this window" marker — enough for
    sequence detection, never a flood.
    """
    try:
        base = max(0.0, float((_m_state._CORE.config or {}).get("pattern_log_min_interval", 60)))
    except Exception:
        base = 60.0
    if device_class in _HIGH_FREQ_CLASSES:
        try:
            hf = float((_m_state._CORE.config or {}).get("pattern_motion_min_interval", 300))
        except Exception:
            hf = 300.0
        return max(base, hf)
    return base


def _pattern_rate_ok(entity_id: str, now: float, device_class: str = "") -> bool:
    """Per-entity rate gate for pattern logging. Returns True (and records the
    time) when this entity may be logged now, False to drop it. Pure + testable
    — a motion storm through this gate stays bounded to one log per interval."""
    if (now - _PATTERN_LOG_LAST.get(entity_id, 0.0)) < _pattern_log_interval(device_class):
        return False
    _PATTERN_LOG_LAST[entity_id] = now
    return True


def log_command(text: str, handled_by: str = "agent",
                entity_ids: list = None, person: str = "unknown"):
    """Record a command for pattern learning."""
    if _m_state._CORE.state_logger:
        _m_state._CORE.state_logger.log_command(text, handled_by, entity_ids, person)


def learning_active() -> bool:
    """Whether Nova's pattern-learning subsystem is currently running —
    the master learning setting every downstream consumer (including
    camera_semantic.py, Phase 4 v7.109.0) gates on. There's no separate
    always-on pattern-learning process independent of Observer/Cognitive
    Core: state_logger is only ever created in start() and torn down in
    stop(), so this is the same on/off surface every other state-change
    row already depends on."""
    return bool(_m_state._CORE.running and _m_state._CORE.state_logger)


def log_camera_event(entity_id: str, new_state: str, area_id: str = "",
                      person: str = "unknown", person_confidence: float = 0.0,
                      detection_confidence: Optional[float] = None) -> bool:
    """Record a synthetic camera-event state change for pattern learning
    (Phase 4, v7.109.0) — the SAME state_changes table and StateLogger every
    other entity's pattern data already goes through, not a parallel store.
    `entity_id` is the synthetic, non-actuating camera_event.<location> id;
    `new_state` is the canonical label or, for package events, the specific
    delivered/stranded/taken/mail sub-state.

    Returns False (a no-op, nothing written) when the learning subsystem
    isn't running — camera events must not be stored while learning is
    disabled, same as everything else state_logger gates on.

    Blocking (SQLite) — callers MUST invoke this via the executor, never
    directly from the event loop."""
    if not learning_active():
        return False
    _m_state._CORE.state_logger.log_state_change(
        entity_id, "", new_state, area_id,
        triggered_by="camera", person=person,
        person_confidence=float(person_confidence or 0.0),
        detection_confidence=detection_confidence,
    )
    return True


def _backfill_filter_states(events: list, interval: float, cap: int = 1000) -> list:
    """Chronological ``(epoch, state)`` events → filtered ``[(epoch, state)]``
    for backfill: drop unavailable/unknown and no-change, apply the per-entity
    rate limit, and keep only the most recent ``cap``. This is the anti-flood
    core — importing 30 days of a chatty motion sensor's history through this
    stays bounded, so backfill can't reintroduce the flood that was just killed.
    """
    from collections import deque
    out: deque = deque(maxlen=max(1, cap))
    last = None
    prev = None
    for epoch, st in events:
        if st is None or st in ("unavailable", "unknown") or st == prev:
            continue
        prev = st
        if last is not None and (epoch - last) < interval:
            continue
        out.append((epoch, st))
        last = epoch
    return list(out)


async def backfill_from_history(hass: HomeAssistant, days: int = 30) -> dict:
    """Import recent recorder history for pattern-relevant entities Nova isn't
    already logging (e.g. motion/occupancy just opted in), so Analyze Now can
    find routines from PAST behavior instead of only data since a setting was
    enabled. Safe by construction: applies the same domain filter + class-aware
    rate limit as live logging (motion capped hard), only touches entities with
    no existing rows (no double-count), and is capped per-entity and globally.
    """
    out = {"imported": 0, "entities": 0, "considered": 0}
    if not _m_state._CORE.state_logger:
        return out
    try:
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.util import dt as dt_util
        from .automation.recorder_time import recorder_epoch
    except Exception:
        return out

    _meta =("automation", "script", "scene", "input_boolean", "input_number")
    _noisy = ("sensor", "binary_sensor", "weather", "sun", "update", "device_tracker")
    dc_by: dict = {}
    relevant: list = []
    try:
        for state in hass.states.async_all():
            eid = state.entity_id
            dom = eid.split(".")[0]
            dc = state.attributes.get("device_class") or ""
            dc_by[eid] = dc
            if dom in _meta:
                continue
            if dom in _noisy and not _pattern_opted_in(eid, dc):
                continue
            relevant.append(eid)
    except Exception:
        return out
    if not relevant:
        return out

    existing = await hass.async_add_executor_job(
        _m_state._CORE.state_logger.distinct_logged_entities)
    todo = [e for e in relevant if e not in existing][:250]   # cap breadth
    out["considered"] = len(todo)
    if not todo:
        return out

    try:
        days = max(1, min(int(days), 30))
    except Exception:
        days = 30
    end = dt_util.utcnow()
    start = end - timedelta(days=days)

    def _fetch():
        return history.get_significant_states(
            hass, start, end, todo, minimal_response=True, no_attributes=True)

    try:
        raw = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception as exc:
        _LOGGER.debug("Nova backfill: history fetch failed: %s", exc)
        return out
    if not raw:
        return out

    rows: list = []
    for eid, states in raw.items():
        dom = eid.split(".")[0]
        interval = _pattern_log_interval(dc_by.get(eid, ""))
        events: list = []
        for s in states:
            try:
                st = getattr(s, "state", None)
                when = (getattr(s, "last_changed", None)
                        or getattr(s, "last_updated", None))
                if st is None and isinstance(s, dict):
                    st = s.get("state")
                    when = s.get("last_changed") or s.get("last_updated")
                if st is None or when is None:
                    continue
                epoch = recorder_epoch(when)
                if epoch is not None:
                    events.append((epoch, st))
            except Exception:
                continue
        kept = _backfill_filter_states(events, interval)
        for epoch, st in kept:
            rows.append((datetime.fromtimestamp(epoch).isoformat(),
                         eid, dom, "unknown", st))
        if kept:
            out["entities"] += 1
        if len(rows) >= 20000:                                # global safety cap
            break

    out["imported"] = await hass.async_add_executor_job(
        _m_state._CORE.state_logger.bulk_insert_history, rows)
    return out


async def run_analysis_now(hass: HomeAssistant) -> dict:
    """Force a pattern-analysis pass now, bypassing ONLY the 6-hour throttle.

    The data-sufficiency gate still applies (>= 7 days and >= 50 recorded
    changes), so this can't produce noise on a fresh install. Returns a result
    dict the panel can show: whether it ran, and if not, why; if it did, how many
    patterns were found and suggestions stored.
    """
    from .automation.patterns import get_analyzer, set_thresholds
    analyzer = get_analyzer()
    analyzer._last_analysis = 0.0  # bypass the 6h throttle for this manual run

    # Backfill recorder history for any pattern-relevant entities we aren't
    # logging yet (e.g. motion/occupancy just opted in), so this finds routines
    # from PAST behavior instead of only data since the setting was enabled.
    backfill = {}
    try:
        backfill = await backfill_from_history(hass, days=30)
    except Exception:
        backfill = {}

    stats = {}
    if _m_state._CORE.state_logger:
        try:
            stats = await hass.async_add_executor_job(
                _m_state._CORE.state_logger.get_pattern_stats)
        except Exception:
            stats = {}

    if not await hass.async_add_executor_job(analyzer.should_analyze):
        return {
            "ran": False,
            "reason": ("Not enough history yet — pattern learning needs about a "
                       "week of data (\u2265 7 days and \u2265 50 recorded changes)."),
            "days_of_data": stats.get("days_of_data", 0),
            "state_changes": stats.get("state_changes", 0),
            "backfill": backfill,
        }

    cfg = _m_state._CORE.config or {}
    try:
        _occ = int(cfg.get("pattern_min_occurrences", 4) or 4)
    except Exception:
        _occ = 4
    try:
        _conf = float(cfg.get("pattern_confidence", 0.55) or 0.55)
    except Exception:
        _conf = 0.55
    set_thresholds(_occ, _conf)

    from . import suggestion_review
    _reviewer = await suggestion_review.reviewer_for(hass)
    if _reviewer is not None:
        patterns = await analyzer.analyze(hass, reviewer=_reviewer)
    else:
        patterns = await analyzer.analyze(hass)
    res = dict(analyzer._last_result)
    res["ran"] = True
    res.setdefault("patterns_found", len(patterns))
    res["state_changes"] = stats.get("state_changes", 0)
    res["days_of_data"] = stats.get("days_of_data", 0)
    res["backfill"] = backfill
    try:
        res["diagnostic"] = await hass.async_add_executor_job(
            analyzer.pattern_diagnostic)
    except Exception:
        res["diagnostic"] = {}
    return res
