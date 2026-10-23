"""The Local Mind: Nova's offline judgment for one household event.

Used when the provider is unreachable (breaker open, or a failed call with
nothing cached), and the one voice every locally composed announcement goes
through. The decision itself is the pure
``cognitive.evaluators.evaluate_local_mind``; this module is the façade that
gathers its inputs and voices its result:

1. self-awareness: per-entity flap memory (3 or more events in 5 minutes);
2. historical grounding: how often this entity reached this state near this
   hour in patterns.db, graded novel / unusual_hour / occasional / common /
   routine;
3. case memory: the reasoning cache's tally of past provider verdicts for
   similar events;
4. decision counters for the panel;
5. the composer and its state phrasing.

The history query is synchronous sqlite and runs in the executor. assess()
never raises.
"""
from __future__ import annotations

import collections
import contextlib
import hashlib
import logging
import sqlite3
import time
from datetime import datetime
from typing import Optional

from . import paths
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

# Tests point this at another database; None means Nova's patterns.db.
DB_PATH: Optional[str] = None

# History caches, on the monotonic clock: (expires, value).
_HIST_TTL = 600.0
_DAYS_TTL = 3600.0
_HIST_CACHE_LIMIT = 800    # past this many profiles, the oldest are dropped
_HIST_CACHE_DROP = 200
_hist_cache: dict[tuple[str, str, int], tuple[float, dict]] = {}
_days_cache: tuple[float, float] = (0.0, 0.0)

# Flap memory: entity -> recent event stamps.
_FLAP_WINDOW = 300.0
_FLAP_COUNT = 3
_recent_events: dict[str, collections.deque] = {}

_stats = dict(decisions=0, spoke=0, silent=0)

_OPENING_CLASSES = ("door", "window", "garage_door", "opening")
_MOTION_CLASSES = ("motion", "occupancy", "presence")
_OPENING_WORDS = ("door", "window", "garage", "gate")
_LEAK_WORDS = ("leak", "flood", "water_")

# What a safety-type sensor is doing when it turns on.
_SAFETY_PHRASES = {dc: "is detecting " + what for dc, what in (
    ("smoke", "smoke"), ("carbon_monoxide", "carbon monoxide"), ("gas", "gas"),
    ("moisture", "water"), ("heat", "excessive heat"), ("vibration", "vibration"),
    ("sound", "sound"))}
_SAFETY_PHRASES.update(tamper="has been tampered with",
                       problem="is reporting a problem", safety="has raised an alert")

# Plain states, read after the device's name.
_STATE_PHRASES: dict[str, str] = {
    "on": "is on", "off": "is off",
    "open": "is open", "closed": "is closed",
    "opening": "is opening", "closing": "is closing",
    "unlocked": "is unlocked", "locked": "is locked",
    "detected": "has detected activity",
    "home": "has arrived home", "not_home": "has left",
    "unavailable": "has gone unresponsive", "unknown": "is in an unknown state",
}

_NOVEL_CLAUSES = [
    "the first time I've observed this",
    "I haven't seen this before",
    "a first in my records",
]

_SPAN_SQL = ("SELECT julianday(MAX(timestamp)) - julianday(MIN(timestamp)) "
             "FROM state_changes")
_NEAR_HOUR_SQL = ("SELECT COUNT(*), SUM(CASE WHEN hour IN (?, ?, ?) THEN 1 ELSE 0 END) "
                  "FROM state_changes "
                  "WHERE entity_id = ? AND new_state = ?")


# ── History grounding ──────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection | None:
    """patterns.db, or None when it cannot be opened (no folder yet and so on)."""
    try:
        return _store.connect(DB_PATH or paths.patterns_db(), timeout=2.0,
                              mkdir=False, busy_timeout_ms=None)
    except Exception:  # no history is a normal state, not an error
        return None


def _data_days() -> float:
    """How many days of history patterns.db holds (cached for an hour)."""
    global _days_cache
    clock = time.monotonic()
    expires, span = _days_cache
    if clock < expires:
        return span
    span = 0.0
    conn = _connect()
    if conn is not None:
        with contextlib.closing(conn):
            try:
                row = conn.execute(_SPAN_SQL).fetchone()
                span = float(row[0] or 0.0) if row else 0.0
            except Exception:  # unreadable table counts as no history
                span = 0.0
    _days_cache = (clock + _DAYS_TTL, span)
    return span


def history_profile(entity_id: str, new_state: str, hour: int) -> dict:
    """Grade how familiar this entity reaching this state near this hour is."""
    key = (entity_id, str(new_state), int(hour))
    clock = time.monotonic()
    cached = _hist_cache.get(key)
    if cached is not None and cached[0] > clock:
        return cached[1]

    days = _data_days()
    profile = {"days": round(days, 1), "at_hour": 0, "total": 0, "grade": "unknown"}
    conn = _connect() if days >= 3 else None  # too little history says nothing
    if conn is not None:
        h = key[2]
        neighbours = ((h - 1) % 24, h % 24, (h + 1) % 24)
        with contextlib.closing(conn):
            try:
                total, at_hour = (int(n or 0) for n in conn.execute(
                    _NEAR_HOUR_SQL, (*neighbours, entity_id, key[1])).fetchone())
                per_day = at_hour / days
                if not total:
                    grade = "novel"
                elif not at_hour:
                    grade = "unusual_hour"
                elif per_day >= 0.5:
                    grade = "routine"
                else:
                    grade = "common" if per_day >= 0.15 else "occasional"
                profile.update(total=total, at_hour=at_hour, grade=grade)
            except Exception as exc:
                _LOGGER.debug("Local Mind history query failed for %s: %s", entity_id, exc)

    _hist_cache[key] = (clock + _HIST_TTL, profile)
    if len(_hist_cache) > _HIST_CACHE_LIMIT:
        for oldest in list(_hist_cache)[:_HIST_CACHE_DROP]:
            _hist_cache.pop(oldest)
    return profile


# ── Flap memory, duplicates, case prior ────────────────────────────────────

def _note_event(entity_id: str) -> bool:
    """Record an event; True when this entity is flapping."""
    if (stamps := _recent_events.get(entity_id)) is None:
        stamps = _recent_events[entity_id] = collections.deque(maxlen=8)
    clock = time.monotonic()
    stamps.append(clock)
    in_window = [t for t in stamps if clock - t <= _FLAP_WINDOW]
    return len(in_window) >= _FLAP_COUNT


def _ev():
    from .cognitive import evaluators
    return evaluators


def _is_duplicate(friendly_name, entity_id, recent_announcements) -> bool:
    return _ev().is_duplicate(friendly_name, entity_id, recent_announcements)


def _case_prior(domain, device_class, category, anyone_home) -> tuple[int, int]:
    """(voiced, silent) tallies of the provider's past verdicts on similar events."""
    try:
        from .reasoning_cache import similar
        return similar(domain, device_class, category, anyone_home)
    except Exception:  # no case memory to lean on
        return (0, 0)


# ── Voice ──────────────────────────────────────────────────────────────────

def _state_phrase(to_state, device_class="", entity_id="") -> str:
    """How a new state reads after the device's name."""
    state = str(to_state or "").strip().lower()
    dc, eid = (device_class or "").lower(), (entity_id or "").lower()

    # On and off on a typed sensor, strongest reading first.
    safety = dc in _SAFETY_PHRASES
    leak = dc == "moisture" or any(w in eid for w in _LEAK_WORDS)
    opening = dc in _OPENING_CLASSES or any(w in eid for w in _OPENING_WORDS)
    motion = dc in _MOTION_CLASSES or "motion" in eid or "occupancy" in eid
    ranked: tuple = ()
    if state == "on":
        ranked = ((safety, _SAFETY_PHRASES.get(dc, "")), (leak, "is detecting water"),
                  (opening, "is open"), (motion, "has detected motion"))
    elif state == "off":
        ranked = ((safety or leak, "has cleared"), (opening, "is closed"),
                  (motion, "has gone quiet"))
    phrase = next((p for hit, p in ranked if hit), "")
    if phrase:
        return phrase

    # A battery level or a sensor reading.
    try:
        value = float(state)
        if dc == "battery" or "battery" in eid:
            whole = int(value)
            level = whole if value == whole else value
            return f"battery is at {level}%"
        if eid.startswith("sensor."):
            return f"reads {to_state}"
    except (TypeError, ValueError):
        pass  # not a number

    if state in _STATE_PHRASES:
        return _STATE_PHRASES[state]
    return f"is now {to_state}" if to_state else "needs attention"


def _pick(variants: list[str], entity_id: str, hour: int) -> str:
    """A stable choice per entity and hour."""
    seed = int(hashlib.md5(f"{entity_id}|{hour}".encode()).hexdigest()[:6], 16)
    return variants[seed % len(variants)]


def _compose(honorific, friendly_name, entity_id, to_state, hour, *, novelty, away,
             escalated, device_class="", urgency="") -> str:
    """One spoken sentence: opener, device and state, then any context."""
    if friendly_name:
        name = friendly_name
    else:
        name = (entity_id.split(".", 1)[-1].replace("_", " ").title()
                if entity_id else "A device")
    body = f"{name} {_state_phrase(to_state, device_class, entity_id)}"

    notes = ["while no one is home"] if away else []
    if novelty == "novel":
        notes.append(_pick(_NOVEL_CLAUSES, entity_id, hour))
    elif novelty == "unusual_hour":
        notes.append("which is unusual at this hour of the night" if 0 <= hour <= 5
                     else "which is out of pattern for this time of day")
    tail = " — " + ", ".join(notes) if notes else ""

    try:
        from . import persona
        tone = persona.register_for(urgency) if urgency else (
            "urgent" if escalated else "neutral")
        opener = persona.announce_opener(honorific, tone)
    except Exception:  # persona unavailable: the honorific alone
        titled = " ".join(part.capitalize() for part in (honorific or "").split())
        opener = f"{titled}," if titled else ""
    return f"{opener} {body}{tail}." if opener else f"{body}{tail}."


def compose_announcement(honorific, friendly_name, entity_id="", to_state="",
                         device_class="", *, hour=None, novelty="unknown", away=False,
                         escalated=False, urgency="") -> str:
    """The sentence for one device event, at the current hour unless given."""
    when = datetime.now().hour if hour is None else int(hour)
    return _compose(honorific, friendly_name, entity_id, to_state, when,
                    novelty=novelty, away=away, escalated=escalated,
                    device_class=device_class, urgency=urgency)


# ── The assessment ─────────────────────────────────────────────────────────

def assess_core(*, honorific, entity_id, domain, device_class, category, from_state,
                to_state, friendly_name, urgency, anyone_home, recent_announcements,
                hour, history, prior) -> dict:
    """Decide and voice one event from already-gathered inputs."""
    from .cognitive.models import EventSnapshot
    from .cognitive.presentation import voiced
    flapping = _note_event(entity_id or friendly_name or "?")
    snapshot = EventSnapshot.build(
        entity_id=entity_id, domain=domain, device_class=device_class,
        from_state=from_state, to_state=to_state, friendly_name=friendly_name,
        category=category, urgency=urgency, anyone_home=anyone_home,
        recent_announcements=recent_announcements)
    verdict = _ev().evaluate_local_mind(
        snapshot, history=history, prior=prior, flapping=flapping, hour=hour,
        flap_count=_FLAP_COUNT, flap_window_s=_FLAP_WINDOW)
    for counter in ("decisions", "spoke" if verdict.speak else "silent"):
        _stats[counter] += 1
    return voiced(verdict, honorific).as_dict()


async def assess(hass, *, honorific, entity_id="", domain="", device_class="",
                 category="", from_state="", to_state="", friendly_name="",
                 urgency="medium", anyone_home=False, recent_announcements=None) -> dict:
    """Gather history and case memory, then decide offline."""
    hour = datetime.now().hour
    prior = _case_prior(domain, device_class, category, anyone_home)
    try:
        history = await hass.async_add_executor_job(
            history_profile, entity_id, to_state, hour)
    except Exception:  # executor gone: decide without history
        history = {"grade": "unknown", "days": 0, "at_hour": 0, "total": 0}
    verdict = assess_core(
        honorific=honorific, entity_id=entity_id, domain=domain,
        device_class=device_class, category=category, from_state=from_state,
        to_state=to_state, friendly_name=friendly_name, urgency=urgency,
        anyone_home=anyone_home, recent_announcements=recent_announcements or [],
        hour=hour, history=history, prior=prior)
    with contextlib.suppress(Exception):  # the panel log is best effort
        from .websocket import nova_log
        why = str(verdict.get("reason", "")).removeprefix("local mind: ")
        said = "SPEAK" if verdict.get("speak") else "silent"
        nova_log("LOCAL", f"{entity_id or friendly_name}: {said} — {why}")
    return verdict


def stats() -> dict:
    """A copy of the decision counters, for the panel."""
    return dict(_stats)
