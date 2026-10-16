"""Root cause analysis: why did this entity change?

Reads the state logger's history (state_changes, commands in patterns.db) and
Nova's own activity log (conversations.db) around a focal change, ranks likely
causes with simple deterministic heuristics, and builds a merged timeline and
a one-sentence summary. No LLM, no network. Synchronous; run it in the
executor. Never raises: missing data gives an honest "couldn't determine".
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Callable, Optional

from . import paths
from .safe_errors import safe_error_message
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

# Overrides for tests; read at call time.
PATTERNS_DB: Optional[str] = None
ACTIVITY_DB: Optional[str] = None

DEFAULT_WINDOW_SECS = 1800      # look this far back before the change
AFTER_WINDOW_SECS = 120         # and this far after it (timeline context)
CASCADE_WINDOW_SECS = 90        # an upstream failure this close is a strong clue
NEAR_WINDOW_SECS = 300          # commands / Nova actions this close count
SCHEDULE_LOOKBACK_DAYS = 14
SCHEDULE_MIN_HITS = 4
CANDIDATE_CAP = 5
TIMELINE_CAP = 40

# Name words too common to tie two entities together.
_GENERIC_TOKENS = frozenset(
    "a an the main device status state level mode on off sensor binary "
    "light switch cover lock climate fan media player".split())
_SPLIT = re.compile(r"[^a-z0-9]+")
_DEAD_STATES = ("unavailable", "unknown")


def _patterns_db() -> str:
    return PATTERNS_DB or paths.patterns_db()


def _activity_db() -> str:
    return ACTIVITY_DB or paths.conversations_db()


def _dt(ts) -> Optional[datetime]:
    """Parse a stored timestamp to a naive datetime (any offset is dropped)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(
            str(ts).strip().replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _secs_before(t0: datetime, ts) -> Optional[float]:
    d = _dt(ts)
    if d is None or d > t0:
        return None
    return (t0 - d).total_seconds()


def _tokens(entity_id) -> set:
    """Distinctive name words of an entity id (domain and filler removed)."""
    obj = str(entity_id or "")
    if "." in obj:
        obj = obj.split(".", 1)[1]
    return {t for t in _SPLIT.split(obj.lower())
            if len(t) > 2 and t not in _GENERIC_TOKENS}


def _rows(db_path: str, sql: str, params) -> list[dict]:
    try:
        with _store.connect(db_path, row_factory=False, mkdir=False,
                            busy_timeout_ms=None) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _namer(names: Optional[dict]) -> Callable[[str], str]:
    if names is None:
        return lambda eid: eid
    from .cognitive import naming
    return lambda eid: naming.name_for(eid, names)


def entity_names(hass) -> dict:
    """entity_id -> friendly name, for wording causes and the timeline."""
    try:
        from .cognitive import naming
        return naming.names_from_states(hass.states.async_all())
    except Exception:
        return {}


def _focal_event(entity_id: str, event_time, pdb: str) -> Optional[dict]:
    if event_time:
        rows = _rows(pdb,
                     "SELECT * FROM state_changes WHERE entity_id = ? "
                     "AND datetime(timestamp) <= datetime(?) "
                     "ORDER BY datetime(timestamp) DESC LIMIT 1",
                     (entity_id, event_time))
    else:
        rows = _rows(pdb,
                     "SELECT * FROM state_changes WHERE entity_id = ? "
                     "ORDER BY datetime(timestamp) DESC LIMIT 1",
                     (entity_id,))
    if rows:
        return rows[0]
    if event_time:
        return {"entity_id": entity_id, "timestamp": event_time,
                "old_state": None, "new_state": None, "area_id": None,
                "domain": entity_id.split(".", 1)[0], "triggered_by": None,
                "hour": None}
    return None


def gather(entity_id: str, event_time=None,
           window_secs: float = DEFAULT_WINDOW_SECS,
           patterns_db: Optional[str] = None,
           activity_db: Optional[str] = None) -> dict:
    """Collect the evidence around one change of `entity_id`."""
    pdb = patterns_db or _patterns_db()
    adb = activity_db or _activity_db()
    focal = _focal_event(entity_id, event_time, pdb)
    t0 = _dt(focal["timestamp"]) if focal else None
    ev: dict = {"entity_id": entity_id, "focal": focal, "t0": t0,
                "changes": [], "commands": [], "actions": [], "hour_hits": 0}
    if focal is None or t0 is None:
        return ev

    start = (t0 - timedelta(seconds=window_secs)).isoformat(sep=" ")
    end = (t0 + timedelta(seconds=AFTER_WINDOW_SECS)).isoformat(sep=" ")
    in_window = ("WHERE datetime(timestamp) >= datetime(?) "
                 "AND datetime(timestamp) <= datetime(?) "
                 "ORDER BY datetime(timestamp) ASC")
    ev["changes"] = _rows(pdb, f"SELECT * FROM state_changes {in_window} LIMIT 500",
                          (start, end))
    ev["commands"] = _rows(pdb, f"SELECT * FROM commands {in_window}", (start, end))
    ev["actions"] = _rows(adb, f"SELECT * FROM activity_log {in_window}", (start, end))

    if focal.get("new_state") is not None and focal.get("hour") is not None:
        since = (t0 - timedelta(days=SCHEDULE_LOOKBACK_DAYS)).isoformat(sep=" ")
        rows = _rows(pdb,
                     "SELECT COUNT(*) AS n FROM state_changes WHERE entity_id = ? "
                     "AND new_state = ? AND hour = ? "
                     "AND datetime(timestamp) >= datetime(?) "
                     "AND datetime(timestamp) < datetime(?)",
                     (entity_id, focal["new_state"], focal["hour"], since,
                      t0.isoformat(sep=" ")))
        ev["hour_hits"] = int(rows[0]["n"]) if rows else 0
    return ev


def candidates(ev: dict, names: Optional[dict] = None) -> list[dict]:
    """Rank plausible causes, most likely first."""
    focal = ev.get("focal")
    t0 = ev.get("t0")
    if not focal or t0 is None:
        return [{"kind": "unknown", "confidence": 0.0,
                 "cause": "no recorded history for this entity",
                 "evidence": "the state logger has no rows for it yet"}]

    entity_id = ev["entity_id"]
    toks = _tokens(entity_id)
    area = focal.get("area_id")
    name = _namer(names)
    out: list[dict] = []

    # 1. The state logger recorded who changed it.
    trig = (focal.get("triggered_by") or "").strip()
    if trig and trig.lower() != "system":
        out.append({"kind": "attributed", "confidence": 0.95,
                    "cause": f"triggered by '{trig}'",
                    "evidence": "recorded by the state logger at the moment of the change"})

    # 2. A related device dropped off just before.
    for row in ev.get("changes", []):
        if row.get("entity_id") == entity_id:
            continue
        if str(row.get("new_state")).lower() not in _DEAD_STATES:
            continue
        secs = _secs_before(t0, row.get("timestamp"))
        if secs is None or secs > CASCADE_WINDOW_SECS:
            continue
        shared = _tokens(row.get("entity_id")) & toks
        same_area = bool(area) and row.get("area_id") == area
        if not (shared or same_area):
            continue
        clue = (f"shares the device name '{sorted(shared)[0]}'" if shared
                else "same room")
        out.append({"kind": "cascade", "confidence": 0.9 if shared else 0.8,
                    "cause": f"{name(row['entity_id'])} went {row['new_state']} "
                             f"{int(secs)}s earlier",
                    "evidence": clue + " — likely an upstream device or hub failure",
                    "timestamp": row.get("timestamp")})

    # 3. Someone asked for it.
    words = set(toks)
    if area:
        words |= _tokens(f"x.{area}")
    domain = focal.get("domain")
    for row in ev.get("commands", []):
        secs = _secs_before(t0, row.get("timestamp"))
        if secs is None or secs > NEAR_WINDOW_SECS:
            continue
        text = str(row.get("text") or "")
        low = text.lower()
        if any(w in low for w in words) or (domain and str(domain) in low):
            out.append({"kind": "command", "confidence": 0.85,
                        "cause": f"{row.get('person') or 'someone'} asked: "
                                 f"\"{text}\" {int(secs)}s earlier",
                        "evidence": "the request names this device, its room, or its type",
                        "timestamp": row.get("timestamp")})

    # 4. Nova itself acted on it.
    for row in ev.get("actions", []):
        secs = _secs_before(t0, row.get("timestamp"))
        if secs is None or secs > NEAR_WINDOW_SECS:
            continue
        message = str(row.get("message") or "")
        low = message.lower()
        if (row.get("entity_id") == entity_id or any(t in low for t in toks)
                or (area and str(area).lower() in low)):
            out.append({"kind": "nova", "confidence": 0.8,
                        "cause": f"Nova acted {int(secs)}s earlier: \"{message[:120]}\"",
                        "evidence": f"category {row.get('category', 'action')}",
                        "timestamp": row.get("timestamp")})

    # 5. It happens at this hour regularly.
    hits = ev.get("hour_hits", 0)
    if hits >= SCHEDULE_MIN_HITS:
        out.append({"kind": "schedule", "confidence": 0.55,
                    "cause": f"this same change has occurred at this hour on {hits} recent days",
                    "evidence": "a recurring pattern — likely a Home Assistant "
                                "automation or schedule"})

    # 6. Something else in the same room changed shortly before.
    if area:
        for row in ev.get("changes", []):
            if row.get("entity_id") == entity_id or row.get("area_id") != area:
                continue
            secs = _secs_before(t0, row.get("timestamp"))
            if secs is None or secs > 120:
                continue
            out.append({"kind": "area", "confidence": 0.5,
                        "cause": f"{name(row['entity_id'])} → {row.get('new_state')} "
                                 f"{int(secs)}s earlier in the same room",
                        "evidence": "related activity in the same area",
                        "timestamp": row.get("timestamp")})
            break

    if not out:
        return [{"kind": "unknown", "confidence": 0.0,
                 "cause": "no plausible cause found in the window",
                 "evidence": "no attributed trigger, upstream failure, command, "
                             "Nova action, or recurring pattern nearby"}]
    out.sort(key=lambda c: c["confidence"], reverse=True)
    return out[:CANDIDATE_CAP]


def _timeline(ev: dict, names: Optional[dict] = None) -> list[dict]:
    name = _namer(names)
    items: list[dict] = []
    for row in ev.get("changes", []):
        items.append({"t": row.get("timestamp"), "src": "change",
                      "text": f"{name(row.get('entity_id'))}: "
                              f"{row.get('old_state')} → {row.get('new_state')}"})
    for row in ev.get("commands", []):
        items.append({"t": row.get("timestamp"), "src": "command",
                      "text": f"{row.get('person', 'someone')}: \"{row.get('text')}\""})
    for row in ev.get("actions", []):
        items.append({"t": row.get("timestamp"), "src": "nova",
                      "text": str(row.get("message"))[:140]})
    items.sort(key=lambda i: _dt(i["t"]) or datetime.min)
    if len(items) > TIMELINE_CAP:
        items = items[-TIMELINE_CAP:]
    return items


def analyze(entity_id: str, event_time=None,
            window_secs: float = DEFAULT_WINDOW_SECS,
            patterns_db: Optional[str] = None,
            activity_db: Optional[str] = None,
            names: Optional[dict] = None) -> dict:
    """Explain why `entity_id` changed (its latest change, or the one at or
    before `event_time`)."""
    try:
        ev = gather(entity_id, event_time, window_secs, patterns_db, activity_db)
        cands = candidates(ev, names)
        top = cands[0]
        if top["kind"] == "unknown":
            summary = (f"I couldn't determine why {_namer(names)(entity_id)} changed"
                       f" — {top['evidence']}.")
        else:
            summary = (f"Most likely cause: {top['cause']} "
                       f"({int(top['confidence'] * 100)}% confident — {top['evidence']}).")
        focal = ev.get("focal") or {}
        return {
            "entity_id": entity_id,
            "event": {k: focal.get(k) for k in
                      ("timestamp", "old_state", "new_state", "area_id")},
            "candidates": cands,
            "timeline": _timeline(ev, names),
            "summary": summary,
        }
    except Exception as exc:
        _LOGGER.exception("rca.analyze failed: %s", exc)
        return {"entity_id": entity_id, "event": {}, "candidates": [],
                "timeline": [], "summary": f"analysis failed: {safe_error_message(exc)}"}
