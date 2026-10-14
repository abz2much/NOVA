"""Read only helpers that fill in nova/get_panel_data.

Each one reads one of Nova's stores (knowledge, suggestions, goals,
routines, reasoning, observer, appliance and mute state) or the recorder,
and answers with a safe empty shape instead of raising, so one broken store
never blanks the panel. Home Assistant modules are imported inside the
functions that need them. websocket.py re-exports these names; the logger
uses the websocket module's name so log filters still match.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(f"{__package__}.websocket")


# ── small formatting helpers ────────────────────────────────────────────────

def _format_uptime(seconds: float) -> str:
    """The two largest units: '2d 14h', '14h 22m', '42m 10s' or '9s'."""
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    for big, big_unit, small, small_unit in (
        (days, "d", hours, "h"),
        (hours, "h", minutes, "m"),
        (minutes, "m", secs, "s"),
    ):
        if big:
            return f"{big}{big_unit} {small}{small_unit}"
    return f"{secs}s"


def _downsample(vals: list[float], n: int) -> list[float]:
    """At most n evenly spread points, always ending on the newest reading,
    so a sparkline keeps its shape and finishes at the current value."""
    count = len(vals)
    if n <= 0 or count <= n:
        return vals
    if n == 1:
        return [vals[-1]]
    stride = (count - 1) / (n - 1)
    return [vals[int(i * stride + 0.5)] for i in range(n)]


# ── names ───────────────────────────────────────────────────────────────────

def _entity_names(hass: HomeAssistant) -> dict:
    """entity_id to friendly name for every state, so stored text written
    with entity ids can be shown with names. Runs on the event loop."""
    try:
        from .cognitive.naming import names_from_states
        return names_from_states(hass.states.async_all())
    except Exception:
        return {}


# Decision fields written for people; ids, kinds and outcomes stay as stored.
_DECISION_TEXT_FIELDS = ("decision", "reason", "observation", "interpretation", "evidence")


def _named_decision(rec, names: dict):
    """A copy of one decision record with entity ids swapped for names."""
    if not isinstance(rec, dict) or not names:
        return rec
    try:
        from .cognitive.naming import humanize_record
        shown = dict(rec)
        for field in _DECISION_TEXT_FIELDS:
            if field in shown:
                shown[field] = humanize_record(shown[field], names)
        return shown
    except Exception:
        return rec


# ── stores read in the executor (SQLite and files) ──────────────────────────

def _get_announcements_today() -> int:
    """How many announcements were spoken today (0 if the log can't be read)."""
    try:
        from .database import get_activity_count_today
        return get_activity_count_today()
    except Exception:
        return 0


def _get_activity_counts() -> dict:
    """The last 24 hours of the activity log, counted in the database."""
    try:
        from .database import get_activity_counts
        return get_activity_counts(hours=24)
    except Exception:
        return {"total": 0, "spoken": 0, "flagged": 0, "dropped": 0}


def _get_knowledge_stats() -> dict:
    """Counts of curated knowledge for the panel."""
    try:
        from . import knowledge
        return knowledge.stats()
    except Exception:
        return {"total": 0, "by_kind": {}, "by_subject": {}}


def _get_doorbell_training(hass: HomeAssistant) -> dict:
    """The doorbell training set: stats, the latest analysed events and the
    patterns found. Reads files, so call it in the executor."""
    try:
        from homeassistant.util import dt as dt_util
        from . import doorbell_training
        try:
            utc_offset = dt_util.now().utcoffset() or timedelta()
            offset = int(utc_offset.total_seconds() // 60)
        except Exception:
            offset = 0
        return {
            "stats": doorbell_training.stats(),
            "recent": doorbell_training.load_events(limit=12),
            "patterns": doorbell_training.find_patterns(utc_offset_minutes=offset),
        }
    except Exception:
        return {"stats": {"total": 0}, "recent": [], "patterns": []}


def _get_suggestions(names: Optional[dict] = None) -> list[dict]:
    """Pending automation suggestions, with the evidence behind each one."""
    try:
        from .automation.api import panel_suggestion_items
        from .automation.patterns import get_analyzer
        pending = get_analyzer().get_pending_suggestions()
        return panel_suggestion_items(pending, names)
    except Exception:
        return []


def _get_filtered_suggestions(names: Optional[dict] = None) -> list[dict]:
    """Suggestions the AI review turned down."""
    try:
        from .automation.api import panel_rejected_items
        from .automation.patterns import get_analyzer
        rejected = get_analyzer().get_rejected_suggestions()
        return panel_rejected_items(rejected, names)
    except Exception:
        return []


def _get_person_routines(names: Optional[dict] = None) -> dict:
    """Learned routines grouped by person for the Memory tab. Older rows
    stored with entity ids are shown with names."""
    try:
        from .automation.patterns import get_analyzer
        from .cognitive.naming import humanize_text
        grouped: dict[str, list[dict]] = {}
        for row in get_analyzer().get_person_patterns():
            person = row.get("person", "")
            if person:
                grouped.setdefault(person, []).append({
                    "id": row.get("id"),
                    "pattern_type": row.get("pattern_type", ""),
                    "description": humanize_text(row.get("description", ""), names),
                    "confidence": round(float(row.get("confidence", 0) or 0), 2),
                    "occurrences": row.get("occurrences", 0),
                    "last_seen": row.get("last_seen", ""),
                })
        return grouped
    except Exception:
        return {}


def _goal_row(goal: dict) -> dict:
    steps = goal.get("steps") or []
    return {
        "id": goal.get("id"),
        "title": goal.get("title", ""),
        "outcome": goal.get("outcome", ""),
        "status": goal.get("status", "active"),
        "steps_done": len([s for s in steps if s.get("status") == "done"]),
        "steps_total": len(steps),
        "steps": steps,
        "next_check_ts": goal.get("next_check_ts", ""),
        "deadline_ts": goal.get("deadline_ts"),
        "last_result": goal.get("last_result", ""),
        "updated_ts": goal.get("updated_ts", ""),
    }


def _get_goals() -> list[dict]:
    """Active and recently closed goals."""
    try:
        from . import goals
        return [_goal_row(g) for g in goals.recent(limit=20)]
    except Exception:
        return []


# ── recorder sparklines ─────────────────────────────────────────────────────

def _numeric_series(samples, points: int) -> list[float]:
    """Numbers from recorder samples, thinned to `points`. A minimal
    response mixes State objects with plain dicts; non numbers are skipped."""
    values: list[float] = []
    for sample in samples or []:
        if isinstance(sample, dict):
            raw = sample.get("state")
        else:
            raw = getattr(sample, "state", None)
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            pass
    return _downsample(values, points)


async def _get_area_sparklines(hass: HomeAssistant, entity_map: dict[str, dict[str, Optional[str]]],
                                hours: float = 12.0, points: int = 20) -> dict:
    """Recent temperature and humidity per area for the room tiles.

    entity_map is {area_id: {"temp": entity_id or None, "humidity": ...}};
    the answer is {area_id: {"temp": [...], "humidity": [...]}} with empty
    series left out. Nova's own pattern store skips sensors, so this reads
    Home Assistant's recorder instead, read only. Recorder internals differ
    between versions, so any failure just means no sparklines this time.
    """
    wanted = sorted({eid for kinds in entity_map.values() for eid in kinds.values() if eid})
    if not wanted:
        return {}
    try:
        from homeassistant.components.recorder import get_instance, history
        from homeassistant.util import dt as dt_util
    except Exception:
        return {}
    end = dt_util.utcnow()
    window = (end - timedelta(hours=hours), end)

    def _read() -> dict:
        return history.get_significant_states(
            hass, *window, wanted, minimal_response=True, no_attributes=True)

    try:
        history_rows = await get_instance(hass).async_add_executor_job(_read)
    except Exception as exc:
        _LOGGER.debug("sparkline history fetch failed: %s", exc)
        return {}
    if not history_rows:
        return {}

    sparklines: dict = {}
    for area_id, kinds in entity_map.items():
        lines = {}
        for kind in ("temp", "humidity"):
            entity_id = kinds.get(kind)
            series = _numeric_series(history_rows.get(entity_id), points) if entity_id else []
            if series:
                lines[kind] = series
        if lines:
            sparklines[area_id] = lines
    return sparklines


# ── live state held in memory ───────────────────────────────────────────────

def _get_sentinel_rules() -> list[dict]:
    """Every built in sentinel rule: id and a short description."""
    try:
        from .sentinel import DEFAULT_RULES
        return [{"id": rule["id"], "desc": rule.get("message", "")[:60]} for rule in DEFAULT_RULES]
    except Exception:
        return []


def _get_lockdown_status() -> dict:
    """Formal lockdown state."""
    try:
        from . import cognitive_core
        return cognitive_core.lockdown_status()
    except Exception:
        return {"active": False, "since": 0.0, "reason": "", "auto": False, "exempt_windows": 0}


def _get_intrusion_status() -> dict:
    """The active intrusion investigation, if any (breach point and route)."""
    try:
        from . import cognitive_core
        return cognitive_core.intrusion_status()
    except Exception:
        return {"active": False, "confirmed": False}


def _get_alarm_panels(hass: HomeAssistant) -> list[dict]:
    """Alarm panels offered for the security source setting."""
    try:
        from . import alarm_source
        return alarm_source.available(hass)
    except Exception:
        return []


def _voice_id_entity(entry):
    """Nova's Voice ID entity for this entry, or None."""
    from .runtime import lifecycle_runtime
    runtime = lifecycle_runtime(entry) if entry is not None else None
    return getattr(runtime, "voice_id", None) if runtime is not None else None


def _get_voice_id_status(entry) -> dict:
    """Status for the Nova Voice ID card: not_set, checking, connected (with
    the app's version and models) or unreachable."""
    try:
        entity = _voice_id_entity(entry)
        if entity is not None:
            return entity.status()
    except Exception:
        pass
    return {"state": "unavailable", "name": "", "version": "", "models": [],
            "languages": [], "error": "", "checked_at": None, "entity_id": None}


def _get_stt_entities(hass: HomeAssistant, entry) -> list[dict]:
    """Speech to text entities offered as the Voice ID fallback, sorted by
    name, leaving out Nova's own."""
    own: set[str] = set()
    try:
        entity = _voice_id_entity(entry)
        if entity is not None and entity.entity_id:
            own.add(entity.entity_id)
        if entry is not None:
            from homeassistant.helpers import entity_registry as er
            registered = er.async_get(hass).async_get_entity_id(
                "stt", "nova", f"{entry.entry_id}-voice-id-stt")
            if registered:
                own.add(registered)
    except Exception:
        pass
    try:
        choices = [
            {"entity_id": state.entity_id,
             "name": str((state.attributes or {}).get("friendly_name") or state.entity_id)}
            for state in hass.states.async_all("stt")
            if state.entity_id not in own
        ]
    except Exception:
        return []
    choices.sort(key=lambda choice: choice["name"].lower())
    return choices


def _get_appliance_status() -> dict:
    """The appliance monitor for Settings, Appliances: the declared profile
    (with learned watts), the sensors being tracked and native appliances."""
    try:
        from . import appliance_monitor
        status = appliance_monitor.status()
        tracked = [
            {"entity": eid,
             "name": sensor.get("friendly_name", eid),
             "appliance": sensor.get("appliance"),
             "phase": sensor.get("phase"),
             "power_w": round(sensor.get("power_w", 0) or 0),
             "discovery": sensor.get("discovery")}
            for eid, sensor in (status.get("sensors") or {}).items()
        ]
        native = [
            {"entity": eid,
             "name": device.get("device_name", eid),
             "appliance": device.get("appliance"),
             "state": device.get("current_state")}
            for eid, device in (status.get("native_appliances") or {}).items()
        ]
        return {
            "running": status.get("running", False),
            "profile": status.get("profile", []),
            "tracked_sensors": tracked,
            "native": native,
            "whole_home": bool(status.get("whole_home_delta")),
        }
    except Exception:
        return {"running": False, "profile": [], "tracked_sensors": [],
                "native": [], "whole_home": False}


def _get_output_mutes() -> dict:
    """What Nova is muted on: entities, categories and the blanket mute."""
    try:
        from . import output_gate
        status = output_gate.status()
        return {
            "entities": list(status.get("muted_entities", [])),
            "categories": list(status.get("muted_categories", [])),
            "all": bool(status.get("mute_all", False)),
        }
    except Exception:
        return {"entities": [], "categories": [], "all": False}


_REASONING_DEFAULTS = {
    "learned_patterns": 0, "cloud_calls": 0, "local_decisions": 0,
    "local_rate": 0, "llm_breaker": "closed",
}


def _get_reasoning_stats() -> dict:
    """The learned reasoning cache counters and the AI connection breaker."""
    stats = dict(_REASONING_DEFAULTS)
    try:
        from . import reasoning_cache
        stats.update(reasoning_cache.stats())
    except Exception:
        pass
    try:
        from . import connectivity
        breaker = connectivity.status()
        if isinstance(breaker, dict):
            stats["llm_breaker"] = breaker.get("state", "closed")
    except Exception:
        pass
    return stats


_OBSERVER_FALLBACK = {
    "running": False, "calls_last_hour": 0, "rate_limit": 30,
    "events_24h": 0, "flagged_24h": 0, "dropped_24h": 0, "spoken_24h": 0,
    "cognition_enabled": True, "cognition_threshold": 0.6,
    "cog_entities": 0, "cog_events_seen": 0, "cog_escalated": 0,
    "cog_predictable": 0, "cog_routines": 0, "cog_presence": 0,
    "presence": [], **_REASONING_DEFAULTS,
}


def _get_observer_stats(activity: Optional[dict] = None) -> dict:
    """The observer tuning card. `activity` is the 24 hour count from
    _get_activity_counts, read in the executor; without it the count is
    read here."""
    try:
        from . import observer as obs
        state = obs._STATE
        counts = activity if activity is not None else _get_activity_counts()
        hour_ago = time.time() - 3600
        stamps = getattr(state, "classifier_timestamps", None) or []
        try:
            from . import cognition
            cog = cognition.stats()
        except Exception:
            cog = {"entities_tracked": 0, "events_seen": 0, "anomalies_escalated": 0}
        try:
            from . import cognition
            hass = getattr(state, "hass", None)
            presence = cognition.presence_status(hass) if hass else []
        except Exception:
            presence = []
        return {
            "running": state.running,
            "calls_last_hour": len([ts for ts in stamps if ts > hour_ago]),
            "rate_limit": obs._effective_rate_limit(),
            "events_24h": counts.get("total", 0),
            "flagged_24h": counts.get("flagged", 0),
            "dropped_24h": counts.get("dropped", 0),
            "spoken_24h": counts.get("spoken", 0),
            "cognition_enabled": obs._cognition_enabled(),
            "cognition_threshold": obs._cognition_threshold(),
            "cog_entities": cog.get("entities_tracked", 0),
            "cog_events_seen": cog.get("events_seen", 0),
            "cog_escalated": cog.get("anomalies_escalated", 0),
            "cog_predictable": cog.get("predictable", 0),
            "cog_routines": cog.get("routines", 0),
            "cog_presence": cog.get("presence_routines", 0),
            "presence": presence,
            **_get_reasoning_stats(),
        }
    except Exception:
        return dict(_OBSERVER_FALLBACK)
