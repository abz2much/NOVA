"""The stat helpers behind nova/get_panel_data.

Moved verbatim out of websocket.py. They read Nova's own stores (knowledge,
suggestions, goals, routines, reasoning and observer stats) and the recorder
for the room sparklines, and they never raise. They import nothing from Home
Assistant at import time.

The logger keeps the name it had in websocket.py, so log lines are unchanged.
websocket.py imports these names, so they stay reachable through it.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(f"{__package__}.websocket")


def _format_uptime(seconds: float) -> str:
    """'2d 14h' / '14h 22m' / '42m 10s' format."""
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _get_announcements_today() -> int:
    """Get today's spoken announcement count from DB. Returns 0 on error."""
    try:
        from .database import get_activity_count_today
        return get_activity_count_today()
    except Exception:
        return 0


def _get_doorbell_training(hass: HomeAssistant) -> dict:
    """Doorbell training-dataset stats + the most recent analysed events, for
    the panel's Doorbell Training view. Does file I/O: run it in the
    executor, never on the event loop. Never raises."""
    try:
        from . import doorbell_training
        from datetime import timedelta as _timedelta
        from homeassistant.util import dt as dt_util
        try:
            offset = int((dt_util.now().utcoffset() or _timedelta()).total_seconds() // 60)
        except Exception:
            offset = 0
        return {
            "stats": doorbell_training.stats(),
            "recent": doorbell_training.load_events(limit=12),
            "patterns": doorbell_training.find_patterns(utc_offset_minutes=offset),
        }
    except Exception:
        return {"stats": {"total": 0}, "recent": [], "patterns": []}


def _entity_names(hass: HomeAssistant) -> dict:
    """entity_id -> friendly name for every state, read on the event loop.
    Used to show text written with entity_ids (older rows, log lines,
    decision records) with names. Never raises."""
    try:
        from .cognitive.naming import names_from_states
        return names_from_states(hass.states.async_all())
    except Exception:
        return {}


# Decision Record fields that are words for a person; ids, kinds and
# outcomes are left exactly as stored.
_DECISION_TEXT_FIELDS = ("decision", "reason", "observation",
                         "interpretation", "evidence")


def _named_decision(rec, names: dict):
    """A display copy of one decision with entities named. Never raises."""
    try:
        from .cognitive.naming import humanize_record
        if not isinstance(rec, dict) or not names:
            return rec
        out = dict(rec)
        for field in _DECISION_TEXT_FIELDS:
            if field in out:
                out[field] = humanize_record(out[field], names)
        return out
    except Exception:
        return rec


def _get_suggestions(names: Optional[dict] = None) -> list[dict]:
    """Pending automation suggestions from the pattern engine, panel-shaped.
    Includes the EVIDENCE behind each one (v6.80.0) so review shows why.
    Never raises."""
    try:
        from .automation.api import panel_suggestion_items
        from .automation.patterns import get_analyzer
        return panel_suggestion_items(get_analyzer().get_pending_suggestions(),
                                      names)
    except Exception:
        return []


def _get_goals() -> list[dict]:
    """Active + recently closed goals, panel-shaped. Never raises."""
    try:
        from . import goals
        out = []
        for g in goals.recent(limit=20):
            steps = g.get("steps") or []
            done = sum(1 for s in steps if s.get("status") == "done")
            out.append({
                "id": g.get("id"),
                "title": g.get("title", ""),
                "outcome": g.get("outcome", ""),
                "status": g.get("status", "active"),
                "steps_done": done,
                "steps_total": len(steps),
                "steps": steps,
                "next_check_ts": g.get("next_check_ts", ""),
                "deadline_ts": g.get("deadline_ts"),
                "last_result": g.get("last_result", ""),
                "updated_ts": g.get("updated_ts", ""),
            })
        return out
    except Exception:
        return []


def _get_filtered_suggestions(names: Optional[dict] = None) -> list[dict]:
    """Suggestions the AI review turned down, panel-shaped. Never raises."""
    try:
        from .automation.api import panel_rejected_items
        from .automation.patterns import get_analyzer
        return panel_rejected_items(get_analyzer().get_rejected_suggestions(),
                                    names)
    except Exception:
        return []


def _get_person_routines(names: Optional[dict] = None) -> dict:
    """Per-person learned routines from the pattern engine, grouped by
    person for the Memory panel. Descriptions stored with entity_ids by
    older releases are shown with names. Never raises."""
    try:
        from .automation.patterns import get_analyzer
        from .cognitive.naming import humanize_text
        rows = get_analyzer().get_person_patterns()
        grouped: dict[str, list[dict]] = {}
        for r in rows:
            person = r.get("person", "")
            if not person:
                continue
            grouped.setdefault(person, []).append({
                "id": r.get("id"),
                "pattern_type": r.get("pattern_type", ""),
                "description": humanize_text(r.get("description", ""), names),
                "confidence": round(float(r.get("confidence", 0) or 0), 2),
                "occurrences": r.get("occurrences", 0),
                "last_seen": r.get("last_seen", ""),
            })
        return grouped
    except Exception:
        return {}


def _downsample(vals: list[float], n: int) -> list[float]:
    """Evenly-spaced downsample to at most n points — a sparkline doesn't
    need every recorder sample, just the shape."""
    if len(vals) <= n or n <= 0:
        return vals
    step = len(vals) / n
    return [vals[int(i * step)] for i in range(n)]


async def _get_area_sparklines(hass: HomeAssistant, entity_map: dict[str, dict[str, Optional[str]]],
                                hours: float = 12.0, points: int = 20) -> dict:
    """Compact recent history for area-tile sparklines, keyed by area_id:
    {area_id: {"temp": [floats], "humidity": [floats]}}. entity_map is
    {area_id: {"temp": entity_id_or_None, "humidity": entity_id_or_None}}.

    v6.43.0 — the first use of HA's recorder in this integration. Pattern
    learning deliberately built its own telemetry (patterns.db) instead of
    depending on recorder, but that store explicitly excludes sensor/
    binary_sensor domains as noise — exactly the domains a temperature
    sparkline needs. Recorder is the right tool for this one job. Read-only,
    wrapped defensively throughout: recorder internals vary by HA version
    and this integration has no other code path exercising them.
    Never raises — an empty dict just means no sparklines this cycle.
    """
    entity_ids = sorted({eid for m in entity_map.values() for eid in m.values() if eid})
    if not entity_ids:
        return {}
    try:
        from datetime import timedelta
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.util import dt as dt_util
    except Exception:
        return {}

    end = dt_util.utcnow()
    start = end - timedelta(hours=hours)

    def _fetch() -> dict:
        return history.get_significant_states(
            hass, start, end, entity_ids,
            minimal_response=True, no_attributes=True)

    try:
        raw = await get_instance(hass).async_add_executor_job(_fetch)
    except Exception as exc:
        _LOGGER.debug("sparkline history fetch failed: %s", exc)
        return {}
    if not raw:
        return {}

    def _series(eid: str) -> list[float]:
        vals: list[float] = []
        for s in (raw.get(eid) or []):
            # minimal_response mixes full State objects (first/last entry)
            # with plain {"state": ..., "last_changed": ...} dicts.
            raw_state = getattr(s, "state", None) if not isinstance(s, dict) else s.get("state")
            try:
                vals.append(float(raw_state))
            except (TypeError, ValueError):
                continue
        return _downsample(vals, points)

    out: dict = {}
    for area_id, m in entity_map.items():
        entry: dict = {}
        if m.get("temp"):
            v = _series(m["temp"])
            if v:
                entry["temp"] = v
        if m.get("humidity"):
            v = _series(m["humidity"])
            if v:
                entry["humidity"] = v
        if entry:
            out[area_id] = entry
    return out


def _get_sentinel_rules() -> list[dict]:
    """Return list of sentinel rule IDs and descriptions."""
    try:
        from .sentinel import DEFAULT_RULES
        return [{"id": r["id"], "desc": r.get("message", "")[:60]} for r in DEFAULT_RULES]
    except Exception:
        return []


def _get_lockdown_status() -> dict:
    """Formal lockdown state for the panel."""
    try:
        from . import cognitive_core
        return cognitive_core.lockdown_status()
    except Exception:
        return {"active": False, "since": 0.0, "reason": "", "auto": False, "exempt_windows": 0}


def _get_alarm_panels(hass: HomeAssistant) -> list[dict]:
    """Alarm choices for the public security source setting."""
    try:
        from . import alarm_source
        return alarm_source.available(hass)
    except Exception:
        return []


def _get_intrusion_status() -> dict:
    """Active intrusion investigation (breach point + route) for the panel."""
    try:
        from . import cognitive_core
        return cognitive_core.intrusion_status()
    except Exception:
        return {"active": False, "confirmed": False}


def _get_knowledge_stats() -> dict:
    """Curated-knowledge summary (counts) for the panel."""
    try:
        from . import knowledge
        return knowledge.stats()
    except Exception:
        return {"total": 0, "by_kind": {}, "by_subject": {}}


def _get_appliance_status() -> dict:
    """Appliance monitor state — declared profile (with learned watts) and what
    Nova is currently tracking — for the Settings → Appliances panel."""
    try:
        from . import appliance_monitor
        st = appliance_monitor.status()
        return {
            "running": st.get("running", False),
            "profile": st.get("profile", []),
            "tracked_sensors": [
                {"entity": eid, "name": s.get("friendly_name", eid),
                 "appliance": s.get("appliance"), "phase": s.get("phase"),
                 "power_w": round(s.get("power_w", 0) or 0),
                 "discovery": s.get("discovery")}
                for eid, s in (st.get("sensors") or {}).items()
            ],
            "native": [
                {"entity": eid, "name": n.get("device_name", eid),
                 "appliance": n.get("appliance"), "state": n.get("current_state")}
                for eid, n in (st.get("native_appliances") or {}).items()
            ],
            "whole_home": bool(st.get("whole_home_delta")),
        }
    except Exception:
        return {"running": False, "profile": [], "tracked_sensors": [],
                "native": [], "whole_home": False}


def _get_reasoning_stats() -> dict:
    """Learned-reasoning cache + connectivity breaker stats for the panel."""
    out = {
        "learned_patterns": 0, "cloud_calls": 0, "local_decisions": 0,
        "local_rate": 0, "llm_breaker": "closed",
    }
    try:
        from . import reasoning_cache
        out.update(reasoning_cache.stats())
    except Exception:
        pass
    try:
        from . import connectivity
        st = connectivity.status()
        out["llm_breaker"] = st.get("state", "closed") if isinstance(st, dict) else "closed"
    except Exception:
        pass
    return out


def _get_output_mutes() -> dict:
    """What Nova is muted on, for the panel's Muted card: the entity and
    category mutes and the blanket shush (all three are saved across restarts)."""
    try:
        from . import output_gate
        st = output_gate.status()
        return {
            "entities": list(st.get("muted_entities", [])),
            "categories": list(st.get("muted_categories", [])),
            "all": bool(st.get("mute_all", False)),
        }
    except Exception:
        return {"entities": [], "categories": [], "all": False}


def _get_observer_stats() -> dict:
    """Return observer pipeline stats for the tuning dashboard."""
    try:
        from . import observer as obs
        from .database import get_recent_activity
        state = obs._STATE

        # Classifier calls in last hour
        now = time.time()
        calls_last_hour = sum(1 for ts in state.classifier_timestamps if ts > now - 3600) if hasattr(state, 'classifier_timestamps') else 0

        # Activity stats from DB
        recent = get_recent_activity(hours=24, limit=500)
        total_events = len(recent)
        spoken = sum(1 for e in recent if e.get("was_spoken"))
        flagged = sum(1 for e in recent if "flagged" in (e.get("message") or ""))
        dropped = sum(1 for e in recent if "not worth" in (e.get("message") or ""))

        try:
            from . import cognition as _cog
            cog_stats = _cog.stats()
        except Exception:
            cog_stats = {"entities_tracked": 0, "events_seen": 0, "anomalies_escalated": 0}

        try:
            from . import cognition as _cog2
            presence = _cog2.presence_status(state.hass) if getattr(state, "hass", None) else []
        except Exception:
            presence = []

        return {
            "running": state.running,
            "calls_last_hour": calls_last_hour,
            "rate_limit": obs._effective_rate_limit(),
            "events_24h": total_events,
            "flagged_24h": flagged,
            "dropped_24h": dropped,
            "spoken_24h": spoken,
            "cognition_enabled": obs._cognition_enabled(),
            "cognition_threshold": obs._cognition_threshold(),
            "cog_entities": cog_stats.get("entities_tracked", 0),
            "cog_events_seen": cog_stats.get("events_seen", 0),
            "cog_escalated": cog_stats.get("anomalies_escalated", 0),
            "cog_predictable": cog_stats.get("predictable", 0),
            "cog_routines": cog_stats.get("routines", 0),
            "cog_presence": cog_stats.get("presence_routines", 0),
            "presence": presence,
            **_get_reasoning_stats(),
        }
    except Exception:
        return {"running": False, "calls_last_hour": 0, "rate_limit": 30,
                "events_24h": 0, "flagged_24h": 0, "dropped_24h": 0, "spoken_24h": 0,
                "cognition_enabled": True, "cognition_threshold": 0.6,
                "cog_entities": 0, "cog_events_seen": 0, "cog_escalated": 0,
                "cog_predictable": 0, "cog_routines": 0, "cog_presence": 0,
                "presence": [], "learned_patterns": 0, "cloud_calls": 0,
                "local_decisions": 0, "local_rate": 0, "llm_breaker": "closed"}
