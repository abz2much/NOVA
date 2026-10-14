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

import hashlib
import logging
import sqlite3
import time
from collections import deque
from typing import Optional

from . import paths
from .persistence import sqlite as _store

_LOGGER = logging.getLogger(__name__)

# Tests point this at another database; None means Nova's patterns.db.
DB_PATH: Optional[str] = None

# History caches, on the monotonic clock.
_HIST_TTL = 600.0
_DAYS_TTL = 3600.0
_hist_cache: dict[tuple, tuple[float, dict]] = {}
_days_cache: tuple[float, float] = (0.0, 0.0)

# Flap memory: entity -> recent event stamps.
_FLAP_WINDOW = 300.0
_FLAP_COUNT = 3
_recent_events: dict[str, deque] = {}

_stats = {"decisions": 0, "spoke": 0, "silent": 0}

_OPENING_CLASSES = ("door", "window", "garage_door", "opening")
_MOTION_CLASSES = ("motion", "occupancy", "presence")

# What a safety-type sensor is doing when it turns on.
_SAFETY_PHRASES = {dc: "is detecting " + what for dc, what in (
    ("smoke", "smoke"), ("carbon_monoxide", "carbon monoxide"), ("gas", "gas"),
    ("moisture", "water"), ("heat", "excessive heat"), ("vibration", "vibration"),
    ("sound", "sound"))}
_SAFETY_PHRASES.update(tamper="has been tampered with",
                       problem="is reporting a problem", safety="has raised an alert")

_STATE_PHRASES = {
    "on": "is on",
    "off": "is off",
    "open": "is open",
    "closed": "is closed",
    "opening": "is opening",
    "closing": "is closing",
    "unlocked": "is unlocked",
    "locked": "is locked",
    "detected": "has detected activity",
    "home": "has arrived home",
    "not_home": "has left",
    "unavailable": "has gone unresponsive",
    "unknown": "is in an unknown state",
}

_NOVEL_CLAUSES = [
    "the first time I've observed this",
    "I haven't seen this before",
    "a first in my records",
]


# ── History grounding ──────────────────────────────────────────────────────

def _connect() -> Optional[sqlite3.Connection]:
    try:
        return _store.connect(DB_PATH or paths.patterns_db(), timeout=2.0,
                              mkdir=False, busy_timeout_ms=None)
    except Exception:
        return None


def _data_days() -> float:
    """How many days of history patterns.db holds (cached for an hour)."""
    global _days_cache
    expires, cached = _days_cache
    clock = time.monotonic()
    if clock < expires:
        return cached
    span = 0.0
    conn = _connect()
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT julianday(MAX(timestamp)) - julianday(MIN(timestamp)) "
                "FROM state_changes").fetchone()
            span = float(row[0] or 0.0) if row else 0.0
        except Exception:
            span = 0.0
        finally:
            conn.close()
    _days_cache = (clock + _DAYS_TTL, span)
    return span


def history_profile(entity_id: str, new_state: str, hour: int) -> dict:
    """Grade how familiar this entity reaching this state near this hour is."""
    key = (entity_id, str(new_state), int(hour))
    now = time.monotonic()
    hit = _hist_cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]

    days = _data_days()
    profile = {"days": round(days, 1), "at_hour": 0, "total": 0, "grade": "unknown"}
    conn = _connect() if days >= 3 else None
    if conn is not None:
        try:
            h = int(hour)
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN hour IN (?, ?, ?) THEN 1 ELSE 0 END) "
                "FROM state_changes WHERE entity_id = ? AND new_state = ?",
                ((h - 1) % 24, h % 24, (h + 1) % 24, entity_id, str(new_state)),
            ).fetchone()
            total = int(row[0] or 0)
            profile["total"] = total
            at_hour = int(row[1] or 0)
            profile["at_hour"] = at_hour
            rate = at_hour / days
            if total == 0:
                profile["grade"] = "novel"
            elif at_hour == 0:
                profile["grade"] = "unusual_hour"
            elif rate >= 0.5:
                profile["grade"] = "routine"
            elif rate >= 0.15:
                profile["grade"] = "common"
            else:
                profile["grade"] = "occasional"
        except Exception as exc:
            _LOGGER.debug("Local Mind history query failed for %s: %s", entity_id, exc)
        finally:
            conn.close()

    _hist_cache[key] = (now + _HIST_TTL, profile)
    if len(_hist_cache) > 800:
        for old in list(_hist_cache)[:200]:
            del _hist_cache[old]
    return profile


# ── Flap memory, duplicates, case prior ────────────────────────────────────

def _note_event(entity_id: str) -> bool:
    """Record an event; True when this entity is flapping."""
    stamps = _recent_events.get(entity_id)
    if stamps is None:
        stamps = _recent_events[entity_id] = deque(maxlen=8)
    now = time.monotonic()
    stamps.append(now)
    return sum(1 for t in stamps if now - t <= _FLAP_WINDOW) >= _FLAP_COUNT


def _ev():
    from .cognitive import evaluators
    return evaluators


def _is_duplicate(friendly_name, entity_id, recent_announcements) -> bool:
    return _ev().is_duplicate(friendly_name, entity_id, recent_announcements)


def _case_prior(domain, device_class, category, anyone_home) -> tuple[int, int]:
    try:
        from . import reasoning_cache
        return reasoning_cache.similar(domain, device_class, category, anyone_home)
    except Exception:
        return (0, 0)


# ── Voice ──────────────────────────────────────────────────────────────────

def _state_phrase(to_state, device_class="", entity_id="") -> str:
    """How a new state reads after the device's name."""
    s = str(to_state or "").strip().lower()
    dc = (device_class or "").lower()
    eid = (entity_id or "").lower()
    opening = dc in _OPENING_CLASSES or any(k in eid for k in ("door", "window", "garage", "gate"))
    motion = dc in _MOTION_CLASSES or "motion" in eid or "occupancy" in eid
    leak = dc == "moisture" or any(k in eid for k in ("leak", "flood", "water_"))

    if s == "on":
        if dc in _SAFETY_PHRASES:
            return _SAFETY_PHRASES[dc]
        if leak:
            return "is detecting water"
        if opening:
            return "is open"
        if motion:
            return "has detected motion"
    elif s == "off":
        if dc in _SAFETY_PHRASES or leak:
            return "has cleared"
        if opening:
            return "is closed"
        if motion:
            return "has gone quiet"

    try:
        num = float(s)
        if dc == "battery" or "battery" in eid:
            whole = int(num)
            return f"battery is at {whole if num == whole else num}%"
        if eid.startswith("sensor."):
            return f"reads {to_state}"
    except (TypeError, ValueError):
        pass

    if s in _STATE_PHRASES:
        return _STATE_PHRASES[s]
    return f"is now {to_state}" if to_state else "needs attention"


def _pick(variants: list[str], entity_id: str, hour: int) -> str:
    """A stable choice per entity and hour."""
    digest = hashlib.md5(f"{entity_id}|{hour}".encode()).hexdigest()
    return variants[int(digest[:6], 16) % len(variants)]


def _compose(honorific, friendly_name, entity_id, to_state, hour, *, novelty, away,
             escalated, device_class="", urgency="") -> str:
    """One spoken sentence: opener, device and state, then any context."""
    if friendly_name:
        name = friendly_name
    elif entity_id:
        name = entity_id.split(".", 1)[-1].replace("_", " ").title()
    else:
        name = "A device"
    core = f"{name} {_state_phrase(to_state, device_class, entity_id)}"

    clauses = []
    if away:
        clauses.append("while no one is home")
    if novelty == "novel":
        clauses.append(_pick(_NOVEL_CLAUSES, entity_id, hour))
    elif novelty == "unusual_hour":
        clauses.append("which is unusual at this hour of the night" if 0 <= hour <= 5
                       else "which is out of pattern for this time of day")
    tail = " — " + ", ".join(clauses) if clauses else ""

    try:
        from . import persona
        if urgency:
            register = persona.register_for(urgency)
        else:
            register = "urgent" if escalated else "neutral"
        opener = persona.announce_opener(honorific, register)
    except Exception:
        h = " ".join(word.capitalize() for word in (honorific or "").split())
        opener = f"{h}," if h else ""
    return opener + (" " if opener else "") + core + tail + "."


def compose_announcement(honorific, friendly_name, entity_id="", to_state="",
                         device_class="", *, hour=None, novelty="unknown", away=False,
                         escalated=False, urgency="") -> str:
    if hour is None:
        from datetime import datetime
        hour = datetime.now().hour
    opts = dict(novelty=novelty, away=away, escalated=escalated,
                device_class=device_class, urgency=urgency)
    return _compose(honorific, friendly_name, entity_id, to_state, int(hour), **opts)


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
    d = _ev().evaluate_local_mind(snapshot, history=history, prior=prior,
                                  flapping=flapping, hour=hour, flap_count=_FLAP_COUNT,
                                  flap_window_s=_FLAP_WINDOW)
    _stats["decisions"] += 1
    _stats["spoke" if d.speak else "silent"] += 1
    return voiced(d, honorific).as_dict()


async def assess(hass, *, honorific, entity_id="", domain="", device_class="",
                 category="", from_state="", to_state="", friendly_name="",
                 urgency="medium", anyone_home=False, recent_announcements=None) -> dict:
    """Gather history and case memory, then decide offline."""
    from datetime import datetime
    hour = datetime.now().hour
    prior = _case_prior(domain, device_class, category, anyone_home)
    try:
        history = await hass.async_add_executor_job(history_profile, entity_id,
                                                    to_state, hour)
    except Exception:
        history = dict(grade="unknown", days=0, at_hour=0, total=0)
    dec = assess_core(
        entity_id=entity_id, friendly_name=friendly_name, domain=domain,
        device_class=device_class, category=category, urgency=urgency,
        from_state=from_state, to_state=to_state, honorific=honorific,
        recent_announcements=recent_announcements or [], anyone_home=anyone_home,
        prior=prior, history=history, hour=hour)
    try:
        from .websocket import nova_log
        why = str(dec.get("reason", "")).removeprefix("local mind: ")
        verdict = "SPEAK" if dec.get("speak") else "silent"
        nova_log("LOCAL", f"{entity_id or friendly_name}: {verdict} — {why}")
    except Exception:
        pass
    return dec


def stats() -> dict:
    return dict(_stats)
