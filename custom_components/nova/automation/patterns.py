"""Learning what the household does on repeat.

The analyzer reads ``state_changes`` and ``commands`` from patterns.db,
finds behaviour that repeats, and hands the confident finds to the
suggestion store (suggestions.py). It runs every six hours once there are at
least seven days of history, and whenever asked.

What it looks for:

1. Daily routines: a device reaching a state at about the same time on most
   days ("the porch light goes on around 18:05").
2. Sequences: one change reliably followed by another within ten minutes
   ("when the garage closes, the front door locks").
3. Repeated commands: the same request around the same hour.
4. Thresholds: an action that follows a sensor crossing a value.

Changes made by an automation (``triggered_by`` automation or
nova_automation) never teach a pattern. Every pattern gets a confidence
between 0 and 1, and those at or above the effective threshold become
suggestions.
"""
from __future__ import annotations

import asyncio
import bisect
from contextlib import closing
import logging
import math
import sqlite3
import statistics
import time
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .area_presence import (
    EMPTY_CONTEXT,
    AreaPresenceContext,
    numeric_trigger_times,
    presence_gate_condition,
    presence_release,
)
from .models import (
    MATCH_EXACT,
    MATCH_OVERLAP,
    SUGGESTION_PENDING,
    SUGGESTION_REJECTED,
    DetectedPattern,
)
from .recorder_time import recorder_epoch
from .suggestions import (  # DB_PATH and MIN_DAYS are shared with the stats
    DB_PATH,
    MIN_DAYS,
    SuggestionStore,
    _db_path,
    _name_for,
    _trigger_phrase,
    generate_automation,
    normalize_suggestion_automation,
    service_for,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MIN_OCCURRENCES = 5               # a pattern must repeat at least this often
CONFIDENCE_THRESHOLD = 0.65       # the bar for becoming a suggestion
ANALYSIS_INTERVAL = 21600         # seconds between scheduled analyses (6 h)
KNOWLEDGE_FACT_CONFIDENCE = 0.75  # routines and commands this sure also become facts
PERSON_DOMINANCE_RATIO = 0.8      # share of a pattern one person needs to own it
_AUTOMATED_SOURCE_SQL = "('automation','nova_automation')"
_LOOKBACK_SQL = "timestamp > datetime('now', '-30 days')"

# A daily routine is the busiest stretch of ROUTINE_SLOTS_EITHER_SIDE five
# minute slots either side of a centre (about half an hour each way), so a
# habit that drifts across the hour (22:55 one night, 23:05 the next) is one
# routine, not two halves. How tightly it keeps to its time is judged over a
# wider stretch, about an hour and a half each way.
_SLOT_MINUTES = 5
_DAY_SLOTS = 24 * 60 // _SLOT_MINUTES
ROUTINE_SLOTS_EITHER_SIDE = 6
SPREAD_SLOTS_EITHER_SIDE = 18
MAX_TIME_ROUTINES = 20


def _source_filter(conn: sqlite3.Connection, alias: str = "") -> str:
    """SQL that leaves out rows an automation produced.

    A table without the ``triggered_by`` column (an old or minimal one, as in
    recovery or a test) has nothing to filter, so every row counts: unknown
    is not automated."""
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(state_changes)")}
    except Exception:
        return "1=1"
    if "triggered_by" not in columns:
        return "1=1"
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}triggered_by, 'system') NOT IN {_AUTOMATED_SOURCE_SQL}"


def _local(timestamp) -> Optional[datetime]:
    """A stored ISO timestamp as a naive local time, or None."""
    try:
        moment = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _days_since(timestamp, now: datetime) -> float:
    """Whole calendar days from a stored timestamp to ``now``, so the answer
    does not depend on when in the day the analysis runs. 0.0 when it cannot
    be read, which never makes a routine look stale."""
    moment = _local(timestamp)
    if moment is None:
        return 0.0
    return float(max(0, (now.date() - moment.date()).days))


def set_thresholds(min_occurrences: int | None = None,
                   confidence: float | None = None) -> None:
    """Apply the panel's pattern settings; the cognitive tick calls this
    before each analysis. Out of range values are clamped, bad ones ignored."""
    global MIN_OCCURRENCES, CONFIDENCE_THRESHOLD
    try:
        if min_occurrences is not None:
            MIN_OCCURRENCES = max(2, int(min_occurrences))
    except Exception:
        pass
    try:
        if confidence is not None:
            CONFIDENCE_THRESHOLD = min(0.95, max(0.3, float(confidence)))
    except Exception:
        pass


# The adaptive suggestion bar (opt in). How welcome recent suggestions were
# moves the bar a little: mostly dismissed as unneeded makes it stricter,
# nearly all acted on makes it slightly looser. The move is bounded and the
# result clamped, and it reads only "suggestion" outcomes, never anything to
# do with intrusion, lockdown or safety.
_ADAPT_CACHE = {"ts": 0.0, "delta": 0.0}
_ADAPT_MIN_JUDGED = 5           # judged suggestions needed before it moves
_ADAPT_WINDOW_S = 30 * 86400.0  # a month of outcomes


def _learned_threshold_delta() -> float:
    """How far (between -0.07 and +0.15) recent outcomes move the bar. 0.0
    when the option is off, on any error, or with too few outcomes. Kept for
    five minutes."""
    try:
        from .. import nova_config
        switched_on = bool(nova_config.get("adaptive_suggestion_threshold", False))
    except Exception:
        switched_on = False
    if not switched_on:
        return 0.0
    now = time.time()
    if now - _ADAPT_CACHE["ts"] < 300.0:
        return _ADAPT_CACHE["delta"]
    delta = 0.0
    try:
        from .. import decision_record
        rate = decision_record.outcome_rate("suggestion", window_s=_ADAPT_WINDOW_S)
        if int(rate.get("judged", 0)) >= _ADAPT_MIN_JUDGED:
            delta = decision_record.threshold_delta_from_rate(rate.get("unwelcome_rate"))
    except Exception:
        delta = 0.0
    _ADAPT_CACHE.update(ts=now, delta=delta)
    return delta


def _effective_threshold() -> float:
    """The confidence bar after the learned move, kept within 0.3 to 0.95."""
    return min(0.95, max(0.3, CONFIDENCE_THRESHOLD + _learned_threshold_delta()))


# ── conditions learned from when things happen ──────────────────────────────

def _time_window_condition(epochs: list) -> Optional[dict]:
    """A Home Assistant time condition when the times fall in one clear daily
    stretch, with at least eight quiet hours around it; otherwise None. Pure.

    This is what stops a learned "motion, then the light" that only ever
    happens in the evening from switching the light on at noon. A stretch
    that runs past midnight works, since the time condition wraps."""
    if len(epochs) < MIN_OCCURRENCES:
        return None
    hours = sorted({datetime.fromtimestamp(e).hour for e in epochs})
    if len(hours) == 1:
        only = hours[0]
        return {"condition": "time", "after": f"{(only - 1) % 24:02d}:00:00",
                "before": f"{(only + 1) % 24:02d}:00:00"}
    # The longest gap between neighbouring hours (going round the clock) is
    # the quiet part; the active stretch is everything else.
    quiet, quiet_from, quiet_to = 0, hours[0], hours[0]
    for here, nxt in zip(hours, hours[1:] + hours[:1]):
        gap = (nxt - here) % 24
        if gap > quiet:
            quiet, quiet_from, quiet_to = gap, here, nxt
    if quiet < 8:
        return None
    return {"condition": "time", "after": f"{quiet_to:02d}:00:00",
            "before": f"{(quiet_from + 1) % 24:02d}:00:00"}


def _is_dark_at(epoch: float, lat: float, lon: float) -> Optional[bool]:
    """Whether the sun was down at ``epoch`` for this location; None when
    astral is missing or fails. The deciding is done in _sun_condition so it
    can be tested without astral."""
    try:
        from datetime import timezone

        from astral import LocationInfo
        from astral.sun import sun as astral_sun
        moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
        place = LocationInfo(latitude=float(lat), longitude=float(lon))
        day = astral_sun(place.observer, date=moment.date(), tzinfo=timezone.utc)
        return moment < day["sunrise"] or moment > day["sunset"]
    except Exception:
        return None


def _sun_condition(epochs: list, lat, lon) -> Optional[dict]:
    """A sun condition (after sunset, before sunrise) when the action nearly
    always happens in the dark; otherwise None. Unlike a clock window it
    follows the seasons."""
    if lat is None or lon is None or len(epochs) < MIN_OCCURRENCES:
        return None
    readings = [d for d in (_is_dark_at(e, lat, lon) for e in epochs) if d is not None]
    if len(readings) < MIN_OCCURRENCES:
        return None
    if sum(readings) / len(readings) >= 0.8:
        return {"condition": "sun", "after": "sunset", "before": "sunrise"}
    return None


class _IdNames(dict):
    """A names map that names every entity by its entity_id, the wording
    used before friendly names (v7.125)."""

    def get(self, key, default=None):
        return key


_AS_IDS = _IdNames()


def _condition_phrase(cond, names: Optional[dict] = None) -> str:
    """The words a description gains from its learned condition(s): one
    dict, a list of them (all apply), or nothing. Entities are named with
    ``names``."""
    if isinstance(cond, list):
        return "".join(_condition_phrase(c, names) for c in cond)
    kind = cond.get("condition") if isinstance(cond, dict) else None
    if kind == "time":
        return f", mostly between {cond.get('after', '')[:5]} and {cond.get('before', '')[:5]}"
    if kind == "sun":
        return ", mostly after dark"
    if kind == "numeric_state":
        sensor = _name_for(cond.get("entity_id", ""), names)
        if "below" in cond:
            return f", mostly while {sensor} is below {cond['below']:g}"
        if "above" in cond:
            return f", mostly while {sensor} is above {cond['above']:g}"
    if kind == "state":
        return f", only when {_name_for(cond.get('entity_id', ''), names)} is {cond.get('state', '')}"
    return ""


# ── numeric sensors ─────────────────────────────────────────────────────────

def _numeric_value_at(epochs: list, values: list, t: float):
    """The reading in force at time ``t`` (sorted ``epochs`` with matching
    ``values``), or None before the first reading. Pure."""
    if not epochs:
        return None
    at = bisect.bisect_right(epochs, t) - 1
    return values[at] if at >= 0 else None


def _nice_threshold(v: float, op: str) -> float:
    """A whole number threshold that still includes the observed side: one
    above the floor for "below", one below the ceiling for "above"."""
    return float(math.floor(v) + 1) if op == "below" else float(math.ceil(v) - 1)


def _percentile(ordered: list, pct: float):
    last = len(ordered) - 1
    return ordered[min(last, max(0, int(round(pct / 100.0 * last))))]


def _numeric_trigger_from(occ: list, baseline: list) -> Optional[dict]:
    """``{"below": T}``, ``{"above": T}`` or None: whether an action keeps
    happening on one side of a threshold, given the sensor's readings when
    it happened (``occ``) and all its readings (``baseline``).

    Two guards against coincidence: the readings at the action sit clearly
    in the low (or high) part of the sensor's range, and the sensor spends
    real time on the other side of the threshold. A sensor that is simply
    always low never yields a "below"."""
    if len(occ) < MIN_OCCURRENCES or len(baseline) < 10:
        return None
    at_action, overall = sorted(occ), sorted(baseline)
    middle = statistics.median(overall)
    low, high = _percentile(overall, 10), _percentile(overall, 90)
    spread = high - low
    if spread <= 0:
        return None                       # a flat sensor explains nothing
    typical = statistics.median(at_action)
    top = _percentile(at_action, 90)
    if typical < middle and top < middle:
        threshold = _nice_threshold(top, "below")
        if high > threshold + 0.1 * spread:
            return {"below": threshold}
    bottom = _percentile(at_action, 10)
    if typical > middle and bottom > middle:
        threshold = _nice_threshold(bottom, "above")
        if low < threshold - 0.1 * spread:
            return {"above": threshold}
    return None


# What a numeric threshold may drive, and how soon after the crossing the
# action must follow for the sensor to count as its trigger.
NUMERIC_ACTION_DOMAINS = ("light", "switch", "cover", "lock", "climate", "fan",
                          "media_player", "humidifier", "water_heater", "valve")
NUMERIC_TRIGGER_WINDOW = 600.0
# A threshold automation acts on every crossing, so crossings must be
# followed by the action at least this often (sequences use their own bars).
NUMERIC_MIN_CONDITIONAL = 0.5
# A sequence becomes an automation that acts on every trigger, so the trigger
# must be followed by the action at least this often; higher without areas
# to compare, and higher still across two different areas.
SEQUENCE_MIN_CONDITIONAL = 0.5
SEQUENCE_NO_AREA_CONDITIONAL = 0.7
SEQUENCE_CROSS_AREA_CONDITIONAL = 0.8
SEQUENCE_CANDIDATES = 60
SEQUENCE_WINDOW_SECONDS = 600.0
_SEQUENCE_WINDOW_CAP = 200   # bounds the pairing work in a burst of activity
_SAMPLE_CAP = 40             # times kept per pair for windows and gates
_LAG_CAP = 200               # gaps kept per pair for the typical delay
# Someone arriving or leaving has no area of its own.
PRESENCE_TRIGGER_DOMAINS = ("person", "device_tracker")
# The most new suggestions the AI review judges in one pass; the rest wait
# for the next pass, which bounds the cost and the time.
REVIEW_MAX_PER_PASS = 10


def _sequence_link_ok(trigger: str, action: str, conditional: float,
                      areas: dict) -> bool:
    """Whether a learned "after A, B" is believable as an automation. Pure."""
    if conditional < SEQUENCE_MIN_CONDITIONAL:
        return False
    if trigger.split(".", 1)[0] in PRESENCE_TRIGGER_DOMAINS:
        return True
    trigger_area, action_area = areas.get(trigger), areas.get(action)
    if trigger_area and action_area:
        return trigger_area == action_area or conditional >= SEQUENCE_CROSS_AREA_CONDITIONAL
    return conditional >= SEQUENCE_NO_AREA_CONDITIONAL


def _threshold_crossings(series: list, op: str, threshold: float) -> list:
    """When a numeric series crosses ``threshold`` going ``op`` ("below" or
    "above"). Pure."""
    readings = sorted(series)
    crossed = []
    for (_before_t, before), (after_t, after) in zip(readings, readings[1:]):
        if op == "below" and before >= threshold > after:
            crossed.append(after_t)
        elif op == "above" and before <= threshold < after:
            crossed.append(after_t)
    return crossed


def _prepare_numeric_history(sensor_hist: dict) -> dict:
    """``{sensor: (epochs, values)}``, time ordered and numeric only, for
    sensors with at least ten readings. Built once per analysis so each
    candidate does not sort every series again."""
    prepared: dict = {}
    for sensor, readings in (sensor_hist or {}).items():
        kept = sorted((t, v) for t, v in readings if isinstance(v, (int, float)))
        if len(kept) >= 10:
            prepared[sensor] = ([t for t, _ in kept], [v for _, v in kept])
    return prepared


def _numeric_condition(times: list, sensor_hist: dict,
                       prepared: Optional[dict] = None) -> Optional[dict]:
    """The best numeric_state condition for an action that keeps happening
    while a sensor sits on one side of a threshold ("only while it is below
    16"), or None. Uses the same guards as a numeric trigger. When several
    sensors qualify, the one with readings for the most of ``times`` wins."""
    if not sensor_hist or len(times) < MIN_OCCURRENCES:
        return None
    if prepared is None:
        prepared = _prepare_numeric_history(sensor_hist)
    best, best_cover = None, 0
    for sensor, (epochs, values) in prepared.items():
        at_action = [v for v in (_numeric_value_at(epochs, values, t) for t in times)
                     if v is not None]
        if len(at_action) < MIN_OCCURRENCES:
            continue
        side = _numeric_trigger_from(at_action, values)
        if side and len(at_action) > best_cover:
            (op, threshold), = side.items()
            best_cover = len(at_action)
            best = {"condition": "numeric_state", "entity_id": sensor, op: threshold}
    return best


# ── daily routines: finding the usual time ──────────────────────────────────

def _slot_of(minute_of_day: int) -> int:
    return (minute_of_day // _SLOT_MINUTES) % _DAY_SLOTS


def _around(slot: int, either_side: int) -> range:
    return range(slot - either_side, slot + either_side + 1)


def _days_by_slot(changes: list) -> dict:
    """slot -> the days with a change in it."""
    by_slot: dict = {}
    for c in changes:
        by_slot.setdefault(_slot_of(c.minute), set()).add(c.day)
    return by_slot


def _days_near(by_slot: dict, centre: int, either_side: int) -> set:
    days: set = set()
    for s in _around(centre, either_side):
        days |= by_slot.get(s % _DAY_SLOTS, set())
    return days


def _busiest_slot(by_slot: dict, counts: dict) -> int:
    """The centre whose window holds the most days, then the most events,
    then the earliest time."""
    def score(centre: int):
        days = _days_near(by_slot, centre, ROUTINE_SLOTS_EITHER_SIDE)
        events = sum(counts.get(s % _DAY_SLOTS, 0)
                      for s in _around(centre, ROUTINE_SLOTS_EITHER_SIDE))
        return (len(days), events, -centre)
    return max(sorted(by_slot), key=score)


def _within(minute: int, centre_slot: int, either_side: int) -> bool:
    gap = abs(_slot_of(minute) - centre_slot)
    return min(gap, _DAY_SLOTS - gap) <= either_side


def _usual_minute(first_each_day: list, centre_slot: int) -> int:
    """The median of each day's first time, measured relative to the centre
    so a routine either side of midnight averages correctly, and rounded to
    five minutes."""
    centre = centre_slot * _SLOT_MINUTES
    offsets = sorted(((m - centre + 720) % 1440) - 720 for m in first_each_day)
    typical = centre + statistics.median(offsets)
    return int(round(typical / _SLOT_MINUTES) * _SLOT_MINUTES) % 1440


def _pick_owner(weights: dict, counts: dict) -> Optional[str]:
    """The one person who accounts for most of a pattern, or None for the
    household. ``weights`` and ``counts`` are by person; the unknown bucket
    is ignored rather than allowed to block an owner."""
    named = {p: w for p, w in weights.items() if p and p != "unknown"}
    total = sum(named.values())
    if not named or total <= 0:
        return None
    top = max(named, key=lambda person: named[person])
    if named[top] / total >= PERSON_DOMINANCE_RATIO and counts.get(top, 0) >= MIN_OCCURRENCES:
        return top
    return None


class _Change:
    __slots__ = ("day", "minute", "stamp", "person", "weight")

    def __init__(self, day, minute, stamp, person, weight):
        self.day, self.minute, self.stamp = day, minute, stamp
        self.person, self.weight = person, weight


class PatternAnalyzer:
    """Finds repeating behaviour in the accumulated history."""

    def __init__(self):
        self._last_analysis: float = 0.0
        self._last_result: dict = {}
        self._last_patterns: list[DetectedPattern] = []
        self._db = _db_path()
        # entity_id -> friendly name, read on the event loop at the start of
        # each analysis; the detectors run in an executor, away from states.
        self._names: dict = {}
        self._person_map: dict = {}
        self._numeric_ran = False
        self._completed_types: set = set()
        # One analysis at a time: a manual run never overlaps the scheduled one.
        self._analysis_lock = asyncio.Lock()

    @property
    def analysis_running(self) -> bool:
        return self._analysis_lock.locked()

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            if not Path(self._db).exists():
                return None
            from ..persistence import sqlite as _store  # package layering
            return _store.connect(self._db, mkdir=False)
        except Exception:
            return None

    def should_analyze(self) -> bool:
        """Whether the interval has passed and there is enough history: at
        least MIN_DAYS days and 50 changes not made by automations."""
        if time.time() - self._last_analysis < ANALYSIS_INTERVAL:
            return False
        conn = self._connect()
        if not conn:
            return False
        try:
            keep = _source_filter(conn)
            first = conn.execute(f"SELECT MIN(timestamp) FROM state_changes WHERE {keep}").fetchone()[0]
            if not first:
                return False
            days = (datetime.now() - datetime.fromisoformat(first)).days
            total = conn.execute(f"SELECT COUNT(*) FROM state_changes WHERE {keep}").fetchone()[0]
            conn.close()
            return days >= MIN_DAYS and total >= 50
        except Exception:
            return False

    def pattern_diagnostic(self) -> dict:
        """Why routines may not be forming: the busiest sources (a flooding
        sensor shows here) and the strongest hourly candidates with the share
        of days each covered, so a near miss is visible instead of just
        "nothing found". Reads the database only."""
        out: dict[str, Any] = {"top_sources": [], "candidates": [], "total_days": 0,
                               "min_days": 0, "min_occurrences": MIN_OCCURRENCES}
        conn = self._connect()
        if not conn:
            return out
        with closing(conn):
            self._diagnose(conn, out)
        return out

    @staticmethod
    def _diagnose(conn, out: dict) -> None:
        try:
            keep = _source_filter(conn)
            total_days = int(conn.execute(
                f"SELECT COUNT(DISTINCT date(timestamp)) FROM state_changes "
                f"WHERE {_LOOKBACK_SQL} AND {keep}").fetchone()[0] or 0)
            out["total_days"] = total_days
            # Coverage of 0.3 needs ceil(0.3 x days) distinct days.
            out["min_days"] = (total_days * 3 + 9) // 10 if total_days else 0
            for entity, changes in conn.execute(
                    f"SELECT entity_id, COUNT(*) c FROM state_changes "
                    f"WHERE {_LOOKBACK_SQL} AND {keep} "
                    f"GROUP BY entity_id ORDER BY c DESC LIMIT 10"):
                out["top_sources"].append({"entity_id": entity, "changes": int(changes)})
            for entity, state, hour, seen, days in conn.execute(
                    f"SELECT entity_id, new_state, hour, COUNT(*) cnt, "
                    f"COUNT(DISTINCT date(timestamp)) days FROM state_changes "
                    f"WHERE {_LOOKBACK_SQL} AND {keep} "
                    f"GROUP BY entity_id, new_state, hour HAVING cnt >= 2 "
                    f"ORDER BY days DESC, cnt DESC LIMIT 12"):
                out["candidates"].append({
                    "entity_id": entity, "state": state, "hour": int(hour),
                    "occurrences": int(seen), "days": int(days),
                    "coverage": round(int(days) / total_days, 2) if total_days else 0.0,
                })
        except Exception:
            pass

    # ── one analysis pass ────────────────────────────────────────────────────

    async def analyze(self, hass: HomeAssistant, reviewer=None) -> list[DetectedPattern]:
        """Run a full analysis and return what was detected.

        ``reviewer`` is an optional ``async (hass, pattern) -> verdict | None``
        that judges each new suggestion before it is stored (the AI review in
        suggestion_review.py). It can only reject: a rejected suggestion is
        stored as rejected and not suggested again, and one it could not
        judge waits for a later pass.

        A caller arriving while a pass runs waits for it and gets its result
        rather than starting another."""
        if self._analysis_lock.locked():
            async with self._analysis_lock:
                return list(self._last_patterns)
        async with self._analysis_lock:
            # Without a reviewer the call keeps its old shape (tests patch it).
            if reviewer is None:
                patterns = await self._analyze_once(hass)
            else:
                patterns = await self._analyze_once(hass, reviewer=reviewer)
            self._last_patterns = list(patterns)
            return patterns

    async def _analyze_once(self, hass: HomeAssistant, reviewer=None) -> list[DetectedPattern]:
        self._last_analysis = time.time()
        context = await self._gather_context(hass)
        self._numeric_ran = False
        self._completed_types = set()
        # Every SQLite read runs in one executor job that opens, uses and
        # closes its own connection, so no connection crosses threads.
        patterns = await hass.async_add_executor_job(self._detect_patterns, *context)
        if patterns is None:          # no patterns.db yet
            return []
        # The adaptive move reads decisions.db, so it is worked out off the loop.
        bar = await hass.async_add_executor_job(_effective_threshold)
        tally: Counter = Counter()
        kept: list = []
        near_misses: list = []
        for pattern in patterns:
            if pattern.confidence >= bar:
                await self._file(hass, pattern, reviewer, tally, kept)
            elif pattern.occurrences >= MIN_OCCURRENCES and len(near_misses) < 8:
                near_misses.append(self._near_miss(pattern, bar))
        # The most reliable routines and commands also become observed facts
        # (the Memory tab marks them ~), owned by one person when theirs alone.
        facts = await hass.async_add_executor_job(self._promote_to_knowledge, patterns)
        # A pending suggestion this pass no longer supports (not found, under
        # the bar, or not automatable) is retired: hidden, and pending again
        # if found later. Only for detectors that completed this pass.
        retired = 0
        for kind in sorted(self._completed_types):
            retired += await hass.async_add_executor_job(
                self._suggestions().retire_missing, kind,
                [p for p in kept if p.pattern_type == kind])
        # "Ran and found nothing worth showing" must read differently from
        # "never ran", so the panel gets the outcome of every pass.
        self._last_result = {
            "ts": time.time(),
            "patterns_found": len(patterns),
            "new_suggestions": tally["new"],
            "already_automated": tally["automated"],
            "reviewed": tally["reviewed"],
            "rejected_by_review": tally["rejected"],
            "review_deferred": tally["deferred"],
            "retired": retired,
            "person_routines": tally["person"],
            "facts": facts,
            "near_misses": near_misses,
        }
        if patterns:
            _LOGGER.info(
                "Pattern analysis: %d patterns found, %d new suggestions, "
                "%d facts learned, %d person routines (threshold=%.0f%%)",
                len(patterns), tally["new"], facts, tally["person"], bar * 100)
        return patterns

    async def _gather_context(self, hass) -> tuple:
        """Everything the detectors need from the event loop (the state
        machine, the recorder), as the arguments for _detect_patterns."""
        person_map = self._person_entity_map(hass)
        self._person_map = person_map
        try:
            from ..cognitive.naming import names_from_states
            self._names = names_from_states(hass.states.async_all())
        except Exception:
            self._names = {}
        try:
            sensor_hist = await self._fetch_numeric_sensor_history(hass)
        except Exception:
            sensor_hist = {}
        try:
            area_presence = await self._fetch_area_presence_context(hass)
        except Exception:
            area_presence = EMPTY_CONTEXT
        # The area of every entity (its own or its device's): thresholds and
        # sequences only link things in the same area.
        try:
            ids = list(sensor_hist) + [s.entity_id for s in hass.states.async_all()]
            entity_areas = await self._fetch_entity_areas(hass, dict.fromkeys(ids))
        except Exception:
            entity_areas = {}
        lat = getattr(hass.config, "latitude", None)
        lon = getattr(hass.config, "longitude", None)
        return person_map, lat, lon, sensor_hist, area_presence, entity_areas

    @staticmethod
    def _near_miss(pattern: DetectedPattern, bar: float) -> dict:
        """A pattern found but under the bar, shown building up so "not enough
        data yet" reads differently from "nothing found". A sequence stores
        once count / (MIN_OCCURRENCES x 3) reaches the bar, so it also says
        how many repeats that takes."""
        needed = None
        if pattern.pattern_type == "sequence":
            exact = bar * MIN_OCCURRENCES * 3
            needed = int(exact) + (1 if exact > int(exact) else 0)
        return {"type": pattern.pattern_type, "description": pattern.description,
                "occurrences": pattern.occurrences, "needed": needed}

    async def _file(self, hass, pattern: DetectedPattern, reviewer, tally: Counter,
                    kept: list) -> None:
        """Store one confident pattern, as a suggestion when it can be one."""
        if not self._installable(pattern):
            # Not something Nova can automate (a read only device, a voice
            # command), so no suggestion; a person's routine is still kept.
            await self._keep_person_routine(hass, pattern, tally)
            return
        kept.append(pattern)
        match = self._automation_match(hass, pattern)
        pattern.details["automation_match"] = match
        if match.get("status") == "already_automated":
            tally["automated"] += 1
            return
        if reviewer is not None and not await self._passes_review(hass, pattern, reviewer, tally):
            return
        if await hass.async_add_executor_job(self._store_suggestion, pattern):
            tally["new"] += 1
        # A pattern clearly owned by one person also goes in their own
        # routine store, apart from the household suggestions.
        await self._keep_person_routine(hass, pattern, tally)

    async def _keep_person_routine(self, hass, pattern: DetectedPattern, tally: Counter) -> None:
        if pattern.details.get("person") and await hass.async_add_executor_job(
                self._store_person_pattern, pattern):
            tally["person"] += 1

    async def _passes_review(self, hass, pattern: DetectedPattern, reviewer,
                             tally: Counter) -> bool:
        """Put a pattern that has not been reviewed yet to the AI review.
        False means do not store it this pass: rejected, or new and not
        judged (over the per pass limit, or the review had no answer)."""
        found = await hass.async_add_executor_job(self._suggestions().lookup, pattern)
        if found is not None and (found["status"] != SUGGESTION_PENDING or found["reviewed"]):
            return True
        if tally["reviewed"] >= REVIEW_MAX_PER_PASS:
            tally["deferred"] += 1
            return found is not None
        tally["reviewed"] += 1
        try:
            verdict = await reviewer(hass, pattern)
        except Exception:
            verdict = None
        if verdict is None:
            tally["deferred"] += 1
            return found is not None
        pattern.details["review"] = verdict
        if verdict.get("verdict") != "reject":
            return True
        tally["rejected"] += 1
        if found is None:
            await hass.async_add_executor_job(self._store_rejected, pattern)
        else:
            await hass.async_add_executor_job(self._suggestions().reject, found["id"], verdict)
        return False

    def _detect_patterns(self, person_map: dict, lat, lon, sensor_hist: dict,
                         area_presence: AreaPresenceContext = EMPTY_CONTEXT,
                         entity_areas: Optional[dict] = None,
                         ) -> Optional[list[DetectedPattern]]:
        """Run every detector over one connection. Synchronous: executor only.

        The connection is made, used and closed in this one thread, since
        sqlite3 refuses one used from another thread. None when patterns.db
        does not exist yet. A detector that fails keeps what the ones before
        it found."""
        conn = self._connect()
        if not conn:
            return None
        found: list[DetectedPattern] = []
        done = self._completed_types
        try:
            found.extend(self._find_time_routines(conn, person_map))
            done.add("time_routine")
            found.extend(self._find_repeated_commands(conn))
            done.add("repeated_command")
            found.extend(self._find_sequence_patterns(conn, lat, lon, sensor_hist,
                                                      area_presence, entity_areas))
            done.add("sequence")
            found.extend(self._find_numeric_triggers(conn, sensor_hist, area_presence,
                                                     entity_areas))
            self._numeric_ran = bool(sensor_hist)
            if self._numeric_ran:
                done.add("numeric_trigger")
            # The old presence detector is no longer run (v7.126.1): it never
            # checked how often an arrival is followed by the change, and the
            # sequence detector covers person and tracker triggers. Marking it
            # done retires its leftover suggestions.
            done.add("presence")
        except Exception as exc:
            _LOGGER.warning("Pattern analysis error: %s", exc)
        finally:
            conn.close()
        for pattern in found:
            names = self._names_for(pattern)
            if names:
                pattern.details["names"] = names
        return found

    def _automation_match(self, hass, pattern: DetectedPattern) -> dict:
        """How the automation this pattern would become compares with the
        automations Home Assistant already has (its cached inventory)."""
        try:
            norm = normalize_suggestion_automation(self._generate_automation(pattern))
            if not norm.get("installable"):
                return {"status": "advisory", "matches": [],
                        "reason": norm.get("reason", "not installable")}
            from .inventory import get_inventory
            inventory = get_inventory(hass)
            if inventory is None:
                return {"status": "inventory_unavailable", "matches": [],
                        "reason": "automation inventory unavailable"}
            from .matching import classify_result
            candidate = {"triggers": norm["trigger"], "conditions": norm.get("condition") or [],
                         "actions": norm["action"], "mode": norm.get("mode", "single")}
            result = classify_result(candidate, inventory.records())
            if result.status == MATCH_OVERLAP and result.same_effect:
                # Another automation already does this to the same device,
                # just from a different trigger: nothing new to suggest.
                return {"status": MATCH_EXACT,
                        "matches": [m.to_dict() for m in result.matches],
                        "reason": "a loaded automation already performs this "
                                  "action on the same device"}
            return result.to_dict()
        except Exception:
            return {"status": "inventory_unavailable", "matches": [],
                    "reason": "automation comparison failed"}

    def _person_entity_map(self, hass) -> dict:
        """Every way a person may be recorded (friendly name, normalised
        name, entity_id) mapped to their ``person.*`` entity, so a routine's
        owner can become a condition."""
        try:
            from ..identity import normalize
        except Exception:
            def normalize(name: str) -> str:
                return "_".join((name or "").strip().lower().split())
        found: dict = {}
        try:
            for person in hass.states.async_all("person"):
                entity = person.entity_id
                shown = person.attributes.get("friendly_name") or entity.split(".", 1)[-1]
                for alias in (shown, normalize(shown), entity):
                    if alias:
                        found[alias] = entity
        except Exception:
            return {}
        return found

    def _name(self, entity_id: str) -> str:
        """How a person knows this entity: its friendly name, else a
        readable id."""
        return _name_for(entity_id, getattr(self, "_names", None))

    def _person_label(self, person: str) -> str:
        """A recorded person as their person entity's friendly name."""
        entity = (getattr(self, "_person_map", None) or {}).get(person)
        shown = (getattr(self, "_names", None) or {}).get(entity) if entity else None
        return str(shown or person)

    def _names_for(self, pattern: DetectedPattern) -> dict:
        """Friendly names of every entity a pattern mentions, carried in its
        details so wording built later can use them."""
        names = getattr(self, "_names", None) or {}
        found: dict = {}

        def visit(value):
            if isinstance(value, str):
                if value in names:
                    found[value] = names[value]
            elif isinstance(value, dict):
                for inner in value.values():
                    visit(inner)
            elif isinstance(value, (list, tuple)):
                for inner in value:
                    visit(inner)
        visit(pattern.entity_ids)
        visit(pattern.details)
        return found

    # ── daily routines ───────────────────────────────────────────────────────

    def _routine_rows(self, conn: sqlite3.Connection):
        """The last 30 days of changes as (entity, state, change, automated),
        plus whether owners were recorded with a confidence."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(state_changes)")}
        keep = _source_filter(conn)
        automated = "0" if keep == "1=1" else f"NOT ({keep})"
        person = "person" if "person" in columns else "NULL"
        weighted = "person_confidence" in columns
        confidence = "person_confidence" if weighted else "NULL"
        rows = conn.execute(
            f"SELECT entity_id, new_state, timestamp, {person} AS who, "
            f"{confidence} AS sure, {automated} AS auto FROM state_changes "
            f"WHERE {_LOOKBACK_SQL}").fetchall()
        out = []
        for row in rows:
            moment = _local(row["timestamp"])
            if moment is None:
                continue
            # Older attributions without a confidence count half; a table
            # without the column at all counts every row fully.
            weight = (float(row["sure"]) if row["sure"] else 0.5) if weighted else 1.0
            change = _Change(moment.date(), moment.hour * 60 + moment.minute,
                             row["timestamp"], row["who"], weight)
            out.append((row["entity_id"], row["new_state"], change, bool(row["auto"])))
        return out

    def _find_time_routines(self, conn: sqlite3.Connection,
                            person_map: Optional[dict] = None) -> list[DetectedPattern]:
        """Devices that reach a state at about the same time on most days."""
        from ..cognitive import patterns as scoring
        by_kind: dict = {}
        automated_by_kind: dict = {}
        observed_days: set = set()
        for entity, state, change, automated in self._routine_rows(conn):
            if automated:
                automated_by_kind.setdefault((entity, state), []).append(change)
            else:
                observed_days.add(change.day)
                by_kind.setdefault((entity, state), []).append(change)
        # Days Nova was watching at all: the denominator for every routine.
        total_days = len(observed_days) or 1
        now = datetime.now()
        found: list[DetectedPattern] = []
        for (entity, state), changes in by_kind.items():
            # A device can have more than one routine (on at 07:00 and again
            # at 19:00): take the strongest, then look again in what is left.
            remaining = changes
            # How tightly a routine keeps its time is judged against every
            # change of this kind, even those another routine already took.
            every_day_by_slot = _days_by_slot(changes)
            while self._could_be_routine(remaining, total_days, scoring):
                pattern, used = self._routine_from(
                    entity, state, remaining, automated_by_kind.get((entity, state), []),
                    total_days, now, person_map or {}, scoring, every_day_by_slot)
                if pattern is None:
                    break
                found.append(pattern)
                remaining = [c for c in remaining if c not in used]
        # The strongest routines, not merely the busiest.
        found.sort(key=lambda p: (-p.confidence, -p.occurrences, p.entity_ids[0]))
        return found[:MAX_TIME_ROUTINES]

    @staticmethod
    def _could_be_routine(changes: list, total_days: int, scoring) -> bool:
        """Cheap rejections first: too few changes, or too few days to reach
        the bar wherever the window lands."""
        if len(changes) < MIN_OCCURRENCES:
            return False
        days = len({c.day for c in changes})
        return days >= scoring.MIN_DISTINCT_DAYS and days / total_days >= scoring.MIN_COVERAGE

    def _routine_from(self, entity: str, state: str, changes: list, automated: list,
                      total_days: int, now: datetime, person_map: dict,
                      scoring, every_day_by_slot: dict) -> tuple:
        """(the routine in the busiest window, the changes it used), or
        (None, []) when that window is not a routine."""
        by_slot = _days_by_slot(changes)
        counts = Counter(_slot_of(c.minute) for c in changes)
        centre = _busiest_slot(by_slot, counts)
        inside = [c for c in changes if _within(c.minute, centre, ROUTINE_SLOTS_EITHER_SIDE)]
        days = {c.day for c in inside}
        # Coverage weighs the days it did not happen: 42 of 45 days is far
        # stronger than 42 of 120, though both were "seen 42 times".
        coverage = len(days) / total_days
        if coverage < scoring.MIN_COVERAGE or len(days) < scoring.MIN_DISTINCT_DAYS:
            return None, []
        nearby_days = _days_near(every_day_by_slot, centre, SPREAD_SLOTS_EITHER_SIDE)
        by_automation = sum(1 for c in automated
                            if _within(c.minute, centre, ROUTINE_SLOTS_EITHER_SIDE))
        latest = max(inside, key=lambda c: (c.day, c.minute)).stamp
        # Confidence is coverage, discounted for a small sample (3 of 3 must
        # not beat 40 of 45), for loose timing and for a routine that has
        # stopped; cognitive/patterns.py explains each factor.
        score = scoring.score_time_routine(
            scoring.RoutineEvidence(
                observations=len(inside), positive_days=len(days),
                eligible_days=total_days, window_days=len(nearby_days),
                days_since_last=_days_since(latest, now),
                automated_observations=by_automation),
            min_occurrences=MIN_OCCURRENCES)
        if not score.accepted:
            return None, []

        first_each_day: dict = {}
        for c in inside:
            here = first_each_day.get(c.day)
            if here is None or c.minute < here:
                first_each_day[c.day] = c.minute
        usual = _usual_minute(list(first_each_day.values()), centre)
        hour, minute = divmod(usual, 60)
        at = f"{hour:02d}:{minute:02d}"
        details: dict[str, Any] = {
            "hour": hour, "minute": minute, "state": state,
            "coverage": round(coverage, 2),
            "consistency": round(coverage, 2),   # older readers use this name
            "observed_days": len(days),
            "opportunity_days": total_days,
            "skipped_days": max(0, total_days - len(days)),
            "evidence": score.evidence(),
        }
        # One person can own the routine outright; otherwise it is the
        # household's.
        weights: Counter = Counter()
        seen: Counter = Counter()
        for c in inside:
            weights[c.person] += c.weight
            seen[c.person] += 1
        owner = _pick_owner(weights, seen)
        if owner:
            details["person"] = owner
            # A time trigger knows nothing of presence, so "only when they are
            # home" is a real guard (and a person still approves it).
            person_entity = person_map.get(owner)
            if person_entity:
                details["condition"] = {"condition": "state", "entity_id": person_entity,
                                        "state": "home"}
        span = f"on {len(days)} of {total_days} days"

        def words(label: str, who) -> str:
            if owner:
                return f"{label} turns {state} around {at} {span} when {who} is home"
            if state in ("on", "off"):
                return f"{label} turns {state} around {at} {span}"
            return f"{label} changes to '{state}' around {at} {span}"
        return DetectedPattern(
            pattern_type="time_routine",
            description=words(self._name(entity), self._person_label(owner or "")),
            entity_ids=[entity],
            confidence=score.confidence,
            occurrences=len(inside),
            coverage=round(coverage, 3),
            details=details,
            legacy_description=words(entity, owner),
        ), inside

    # ── repeated commands ────────────────────────────────────────────────────

    def _find_repeated_commands(self, conn: sqlite3.Connection) -> list[DetectedPattern]:
        """Requests said again and again around the same hour."""
        try:
            rows = conn.execute(f"""
                SELECT text, hour, COUNT(*) AS cnt FROM commands
                WHERE {_LOOKBACK_SQL}
                GROUP BY text, hour HAVING cnt >= ?
                ORDER BY cnt DESC LIMIT 20
            """, (MIN_OCCURRENCES,)).fetchall()
        except Exception:
            return []
        found: list[DetectedPattern] = []
        for row in rows:
            text, hour, count = row["text"], row["hour"], row["cnt"]
            # Measured over the same 30 days as the count, so old history
            # does not water the share down.
            said = conn.execute(f"SELECT COUNT(*) FROM commands WHERE text = ? AND {_LOOKBACK_SQL}",
                                (text,)).fetchone()[0]
            details: dict[str, Any] = {"command": text, "hour": hour}
            owner = self._dominant_person(conn, "commands", text=text, hour=hour)
            regularly = f"says '{text}' around {hour:02d}:00 regularly ({count} times)"
            if owner:
                details["person"] = owner
                description = f"{self._person_label(owner)} {regularly}"
                legacy = f"{owner} {regularly}"
            else:
                description = legacy = (f"'{text}' is said around {hour:02d}:00 "
                                        f"regularly ({count} times)")
            found.append(DetectedPattern(
                pattern_type="repeated_command",
                description=description,
                entity_ids=[],
                confidence=min(1.0, count / max(said, count, 1) * 0.8 + 0.2),
                occurrences=count,
                details=details,
                legacy_description=legacy,
            ))
        return found

    # ── sequences ────────────────────────────────────────────────────────────

    def _find_sequence_patterns(self, conn: sqlite3.Connection, lat=None, lon=None,
                                sensor_hist=None,
                                area_presence: AreaPresenceContext = EMPTY_CONTEXT,
                                entity_areas: Optional[dict] = None,
                                ) -> list[DetectedPattern]:
        """Changes that keep following another change within ten minutes.

        One pass over the rows in time order with a sliding window (an SQL
        self join never finished on a large history). Pairs may cross
        domains: a switch can trigger a light, a cover a fan. The typical gap
        is measured so the automation waits the real time."""
        from ..cognitive import patterns as scoring
        try:
            rows = conn.execute(
                "SELECT timestamp, entity_id, new_state FROM state_changes "
                f"WHERE {_LOOKBACK_SQL} AND {_source_filter(conn)} ORDER BY timestamp"
            ).fetchall()
        except Exception:
            return []
        pairs = self._pair_up(rows)
        prepared = _prepare_numeric_history(sensor_hist or {})
        areas = entity_areas or {}
        best: dict = {}   # (action entity, state) -> the strongest pattern for it
        for key, support in pairs["counts"].most_common(SEQUENCE_CANDIDATES):
            if support < MIN_OCCURRENCES:
                break
            pattern = self._sequence_from(key, support, pairs, scoring, lat, lon,
                                          sensor_hist or {}, prepared, area_presence, areas)
            if pattern is None:
                continue
            target = (key[2], key[3])
            if target not in best or pattern.confidence > best[target].confidence:
                best[target] = pattern
        return list(best.values())

    @staticmethod
    def _pair_up(rows) -> dict:
        """Count every (A reached a, then B reached b) inside the window, with
        the gaps and times each pair needs."""
        window: deque = deque()          # (epoch, entity, state)
        counts: Counter = Counter()      # (ea, sa, eb, sb) -> support
        lags: dict = {}                  # pair -> gaps in seconds
        action_times: dict = {}          # pair -> times the second change happened
        trigger_times: dict = {}         # pair -> distinct times of the first
        offs: dict = {}                  # entity -> times it turned off
        triggers: Counter = Counter()    # (entity, state) -> how often it happened
        for row in rows:
            moment = _local(row["timestamp"])
            if moment is None:
                continue
            epoch = moment.timestamp()
            entity, state = row["entity_id"], row["new_state"]
            if state == "off":
                offs.setdefault(entity, []).append(epoch)
            while window and window[0][0] < epoch - SEQUENCE_WINDOW_SECONDS:
                window.popleft()
            triggers[(entity, state)] += 1
            this_change: set = set()
            for before, other, other_state in reversed(window):
                if other == entity:
                    continue
                key = (other, other_state, entity, state)
                if key in this_change:
                    continue
                this_change.add(key)
                counts[key] += 1
                gaps = lags.setdefault(key, [])
                if len(gaps) < _LAG_CAP:
                    gaps.append(epoch - before)
                done = action_times.setdefault(key, [])
                if len(done) < _SAMPLE_CAP:
                    done.append(epoch)
                started = trigger_times.setdefault(key, [])
                if before not in started and len(started) < _SAMPLE_CAP:
                    started.append(before)
            window.append((epoch, entity, state))
            if len(window) > _SEQUENCE_WINDOW_CAP:
                window.popleft()
        return {"counts": counts, "lags": lags, "action_times": action_times,
                "trigger_times": trigger_times, "offs": offs, "triggers": triggers}

    def _sequence_from(self, key: tuple, support: int, pairs: dict, scoring, lat, lon,
                       sensor_hist: dict, prepared: dict,
                       area_presence: AreaPresenceContext, areas: dict,
                       ) -> Optional[DetectedPattern]:
        first, first_state, then, then_state = key
        if service_for(then, then_state) is None:
            return None
        happened = pairs["triggers"].get((first, first_state), support)
        times = pairs["action_times"].get(key, [])
        score = scoring.score_sequence(
            scoring.SequenceEvidence(
                support=support, trigger_count=happened,
                distinct_days=len({datetime.fromtimestamp(t).date() for t in times})),
            min_occurrences=MIN_OCCURRENCES)
        if not score.accepted:
            return None
        conditional = support / max(support, happened, 1)
        if not _sequence_link_ok(first, then, conditional, areas):
            return None
        # The typical gap: one slow evening must not stretch every wait.
        gaps = pairs["lags"].get(key) or [0.0]
        delay = int(round(statistics.median(gaps)))
        shown_conditions: list = []
        window = _sun_condition(times, lat, lon) or _time_window_condition(times)
        if window:
            shown_conditions.append(window)
        numeric = _numeric_condition(times, sensor_hist, prepared)
        if numeric:
            shown_conditions.append(numeric)
        conditions = list(shown_conditions)
        gate = presence_gate_condition(then, pairs["trigger_times"].get(key, []),
                                       area_presence, exclude=(first,))
        if gate:
            conditions.append(gate["condition"])
        release = None
        if gate and str(then_state).lower() == "on":
            release = presence_release(then, pairs["offs"].get(then, ()), area_presence, gate)

        def words(names) -> str:
            text = (f"{_trigger_phrase(first, first_state, names)}, {_name_for(then, names)} "
                    f"turns {then_state} shortly after ({support} times in 30 days, "
                    f"~{delay}s later)" + _condition_phrase(shown_conditions, names))
            if gate:
                text += f", only when presence is detected in {gate['area_name']}"
            return text
        details: dict[str, Any] = {
            "trigger": {"entity": first, "state": first_state},
            "action": {"entity": then, "state": then_state},
            "delay_seconds": delay,
            "condition": conditions or None,
        }
        if gate:
            details["presence_gate"] = {k: gate[k] for k in ("entity_id", "area_id", "area_name")}
        if release:
            details["presence_release"] = release
        details.update(conditional=round(conditional, 3), trigger_count=happened,
                       evidence=score.evidence())
        return DetectedPattern(
            pattern_type="sequence",
            description=words(self._names),
            legacy_description=words(_AS_IDS),
            entity_ids=[first, then],
            confidence=score.confidence,
            occurrences=support,
            details=details,
        )

    # ── thresholds ───────────────────────────────────────────────────────────

    def _find_numeric_triggers(self, conn: sqlite3.Connection, sensor_hist: dict,
                               area_presence: AreaPresenceContext = EMPTY_CONTEXT,
                               entity_areas: Optional[dict] = None,
                               ) -> list[DetectedPattern]:
        """Actions that follow a sensor crossing a threshold. ``sensor_hist``
        maps sensor -> time ordered ``[(epoch, value)]``, fetched by the caller
        so this needs no recorder.

        A sensor only explains an action when the two share an area (every
        room gets dark in the evening; the landing's light level says nothing
        of the kitchen), when it actually crosses shortly before the action
        rather than merely reading low, and when crossings are followed by the
        action often enough to automate. Only the strongest sensor is kept
        for each action."""
        from ..cognitive import patterns as scoring
        if not sensor_hist:
            return []
        areas = entity_areas or {}
        domains = ",".join(f"'{d}'" for d in NUMERIC_ACTION_DOMAINS)
        try:
            rows = conn.execute(
                "SELECT entity_id, new_state, timestamp FROM state_changes "
                f"WHERE {_LOOKBACK_SQL} AND domain IN ({domains}) "
                f"AND {_source_filter(conn)} ORDER BY timestamp").fetchall()
        except Exception:
            return []
        actions: dict = {}
        for row in rows:
            if row["new_state"] in ("unavailable", "unknown"):
                continue
            moment = _local(row["timestamp"])
            if moment is not None:
                actions.setdefault((row["entity_id"], row["new_state"]), []).append(
                    moment.timestamp())
        prepared = _prepare_numeric_history(sensor_hist)
        if not prepared:
            return []
        best: dict = {}
        # The busiest actions only, which bounds the sensor by action work.
        busiest = sorted(actions.items(), key=lambda kv: len(kv[1]), reverse=True)[:20]
        for (target, state), times in busiest:
            area = areas.get(target)
            if len(times) < MIN_OCCURRENCES or not area:
                continue
            for sensor, (epochs, values) in prepared.items():
                if areas.get(sensor) != area:
                    continue
                pattern = self._threshold_from(sensor, epochs, values, target, state, times,
                                               scoring, area_presence)
                if pattern is None:
                    continue
                if (target, state) not in best or pattern.confidence > best[(target, state)].confidence:
                    best[(target, state)] = pattern
        return list(best.values())

    def _threshold_from(self, sensor: str, epochs: list, values: list, target: str,
                        state: str, times: list, scoring,
                        area_presence: AreaPresenceContext) -> Optional[DetectedPattern]:
        at_action = [v for v in (_numeric_value_at(epochs, values, t) for t in times)
                     if v is not None]
        if len(at_action) < MIN_OCCURRENCES:
            return None
        side = _numeric_trigger_from(at_action, values)
        if not side:
            return None
        (op, threshold), = side.items()
        series = list(zip(epochs, values))
        crossings = _threshold_crossings(series, op, threshold)
        followed_at = numeric_trigger_times(series, op, threshold, times,
                                            window_seconds=NUMERIC_TRIGGER_WINDOW)
        support = len(followed_at)
        days = len({datetime.fromtimestamp(t).date() for t in followed_at})
        score = scoring.score_sequence(
            scoring.SequenceEvidence(support=support, trigger_count=len(crossings),
                                     distinct_days=days),
            min_occurrences=MIN_OCCURRENCES)
        if not score.accepted or support / max(support, len(crossings), 1) < NUMERIC_MIN_CONDITIONAL:
            return None
        gate = presence_gate_condition(target, followed_at, area_presence, exclude=(sensor,))

        def words(sensor_name: str, target_name: str) -> str:
            text = (f"When {sensor_name} goes {op} {threshold:g}, {target_name} turns "
                    f"{state} ({support} times in 30 days)")
            if gate:
                text += f", only when presence is detected in {gate['area_name']}"
            return text
        details: dict[str, Any] = {
            "trigger_sensor": sensor, "op": op, "threshold": threshold,
            "action": {"entity": target, "state": state},
            "crossings": len(crossings), "followed": support,
            "action_count": len(times), "window_seconds": NUMERIC_TRIGGER_WINDOW,
            "distinct_days": days, "evidence": score.evidence(),
        }
        if gate:
            details["condition"] = [gate["condition"]]
            details["presence_gate"] = {k: gate[k] for k in ("entity_id", "area_id", "area_name")}
        return DetectedPattern(
            pattern_type="numeric_trigger",
            description=words(self._name(sensor), self._name(target)),
            legacy_description=words(sensor, target),
            entity_ids=[sensor, target],
            confidence=score.confidence,
            occurrences=support,
            details=details,
        )

    # ── reading Home Assistant (event loop) ──────────────────────────────────

    async def _fetch_entity_areas(self, hass, entity_ids) -> dict:
        """entity_id -> area (its own or its device's) for these entities.
        Never raises."""
        try:
            from ..audio_routing import entity_area
        except Exception:
            return {}
        found: dict = {}
        for entity_id in entity_ids:
            try:
                area = entity_area(hass, entity_id)
            except Exception:
                area = None
            if area:
                found[entity_id] = area
        return found

    async def _fetch_area_presence_context(self, hass) -> AreaPresenceContext:
        """A month of presence sensor history by area, read without crossing
        Home Assistant's threads. Bounded to 40 sensors."""
        try:
            from homeassistant.components.recorder import get_instance, history
            from homeassistant.helpers import area_registry as ar
            from homeassistant.util import dt as dt_util

            from ..audio_routing import entity_area
        except Exception:
            return EMPTY_CONTEXT
        sensors: list[tuple[str, str]] = []
        try:
            for state in hass.states.async_all("binary_sensor"):
                if state.attributes.get("device_class") not in ("occupancy", "presence"):
                    continue
                area = entity_area(hass, state.entity_id)
                if area:
                    sensors.append((state.entity_id, area))
        except Exception:
            return EMPTY_CONTEXT
        sensors = sorted(sensors)[:40]
        if not sensors:
            return EMPTY_CONTEXT
        entity_areas: dict[str, str] = dict(sensors)
        try:
            for domain in ("light", "switch", "fan", "input_boolean", "humidifier",
                           "siren", "lock", "cover", "scene"):
                for state in hass.states.async_all(domain):
                    area = entity_area(hass, state.entity_id)
                    if area:
                        entity_areas[state.entity_id] = area
        except Exception:
            return EMPTY_CONTEXT
        by_area: dict[str, list[str]] = {}
        for sensor, area in sensors:
            by_area.setdefault(area, []).append(sensor)
        try:
            registry = ar.async_get(hass)
            area_names = {area: getattr(registry.async_get_area(area), "name", None) or area
                          for area in by_area}
        except Exception:
            area_names = {area: area for area in by_area}
        end = dt_util.utcnow()
        start = end - timedelta(days=30)
        ids = [sensor for sensor, _area in sensors]

        def read():
            return history.get_significant_states(hass, start, end, ids,
                                                  minimal_response=True, no_attributes=True)
        try:
            raw = await get_instance(hass).async_add_executor_job(read)
        except Exception:
            return EMPTY_CONTEXT
        sensor_history: dict[str, tuple[tuple[float, bool], ...]] = {}
        for sensor, states in (raw or {}).items():
            series = []
            for item in states:
                try:
                    value, when = _history_point(item)
                    epoch = recorder_epoch(when)
                    if epoch is not None and value in ("on", "off"):
                        series.append((epoch, value == "on"))
                except Exception:
                    continue
            if series:
                sensor_history[sensor] = tuple(sorted(series))
        return AreaPresenceContext(
            sensor_history=sensor_history,
            entity_areas=entity_areas,
            area_sensors={area: tuple(ids) for area, ids in by_area.items()},
            area_names=area_names,
        )

    async def _fetch_numeric_sensor_history(self, hass) -> dict:
        """A month of readings from up to 30 temperature, humidity and light
        level sensors, as ``{sensor: [(epoch, value)]}``. {} on any failure."""
        try:
            from homeassistant.components.recorder import get_instance, history
            from homeassistant.util import dt as dt_util
        except Exception:
            return {}
        try:
            ids = [s.entity_id for s in hass.states.async_all("sensor")
                   if s.attributes.get("device_class") in ("temperature", "humidity", "illuminance")]
        except Exception:
            return {}
        ids = ids[:30]
        if not ids:
            return {}
        end = dt_util.utcnow()
        start = end - timedelta(days=30)

        def read():
            return history.get_significant_states(hass, start, end, ids,
                                                  minimal_response=True, no_attributes=True)
        try:
            raw = await get_instance(hass).async_add_executor_job(read)
        except Exception:
            return {}
        found: dict = {}
        for sensor, states in (raw or {}).items():
            series = []
            for item in states:
                try:
                    value, when = _history_point(item)
                    reading = float(value)
                    epoch = recorder_epoch(when)
                    if epoch is not None:
                        series.append((epoch, reading))
                except (TypeError, ValueError):
                    continue
            if len(series) >= 10:
                found[sensor] = series
        return found

    # ── owners ───────────────────────────────────────────────────────────────

    def _dominant_person(self, conn: sqlite3.Connection, table: str, *,
                         hour: int, entity: str | None = None,
                         state: str | None = None,
                         text: str | None = None) -> Optional[str]:
        """The one person behind most of a pattern in one hour, or None for
        the household. ``table`` is "state_changes" (matched on entity, state
        and hour) or "commands" (text and hour). Each change counts by how
        sure its attribution was; commands count fully, since the
        conversation path runs the full identity check. A database without
        the person columns falls back to the household."""
        try:
            if table == "state_changes":
                rows = conn.execute(f"""
                    SELECT person, COUNT(*) AS cnt,
                           SUM(COALESCE(NULLIF(person_confidence, 0), 0.5)) AS wt
                    FROM state_changes
                    WHERE entity_id = ? AND new_state = ? AND hour = ?
                      AND {_LOOKBACK_SQL} AND {_source_filter(conn)}
                    GROUP BY person
                """, (entity, state, hour)).fetchall()
            else:
                rows = conn.execute(f"""
                    SELECT person, COUNT(*) AS cnt, COUNT(*) * 1.0 AS wt FROM commands
                    WHERE text = ? AND hour = ? AND {_LOOKBACK_SQL}
                    GROUP BY person
                """, (text, hour)).fetchall()
        except Exception:
            if table != "state_changes":
                return None
            try:
                # An older table with no confidence column: plain counts.
                rows = conn.execute(f"""
                    SELECT person, COUNT(*) AS cnt, COUNT(*) * 1.0 AS wt
                    FROM state_changes
                    WHERE entity_id = ? AND new_state = ? AND hour = ?
                      AND {_LOOKBACK_SQL} AND {_source_filter(conn)}
                    GROUP BY person
                """, (entity, state, hour)).fetchall()
            except Exception:
                return None
        return _pick_owner({r["person"]: float(r["wt"] or 0.0) for r in rows},
                           {r["person"]: int(r["cnt"]) for r in rows})

    # ── knowledge and person routines ────────────────────────────────────────

    def _entity_label(self, entity_id: str) -> str:
        """A readable label from an entity_id alone."""
        tail = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        return tail.replace("_", " ").strip()

    def _fact_for(self, pattern: DetectedPattern, legacy: bool = False):
        """(subject, kind, key, value) for the observed fact a pattern states,
        or None when it is not worth stating. Stable, so a new analysis
        updates the same fact. ``legacy`` gives the key used before friendly
        names (the entity_id without its domain)."""
        details = pattern.details
        if pattern.pattern_type == "time_routine" and pattern.entity_ids:
            entity = pattern.entity_ids[0]
            label = (self._entity_label(entity) if legacy
                     else _name_for(entity, details.get("names") or {}))
            state = str(details.get("state", "")).strip()
            if details.get("hour") is None or not label:
                return None
            when = (f"around {int(details['hour']):02d}:"
                    f"{int(details.get('minute') or 0):02d} most days")
            verb = "turns" if state in ("on", "off") else "set to"
            return (self._subject_for_pattern(pattern), "fact", f"{label} {verb} {state}", when)
        if pattern.pattern_type == "repeated_command":
            said = str(details.get("command", "")).strip()
            hour = details.get("hour")
            if not said or hour is None:
                return None
            return (self._subject_for_pattern(pattern), "fact", f'asks "{said[:60]}"',
                    f"usually around {hour:02d}:00")
        return None

    def _subject_for_pattern(self, pattern: DetectedPattern) -> str:
        """Whose knowledge a fact is: the owner's when the pattern is
        clearly theirs alone, else the household's."""
        owner = pattern.details.get("person")
        if not owner:
            return "household"
        try:
            from .. import identity
            return identity.normalize(owner)
        except Exception:
            return "household"

    def _promote_to_knowledge(self, patterns: list) -> int:
        """Write the most reliable routines and commands as observed facts.
        Synchronous. Returns how many were written."""
        try:
            from .. import knowledge
        except Exception:
            return 0
        written = 0
        for pattern in patterns:
            if pattern.confidence < KNOWLEDGE_FACT_CONFIDENCE:
                continue
            fact = self._fact_for(pattern)
            if not fact:
                continue
            subject, kind, key, value = fact
            try:
                # A fact learned under the old wording moves to the new key
                # rather than being learned twice. Stated facts never move.
                old = self._fact_for(pattern, legacy=True)
                if old and old[2] != key:
                    knowledge.rename_observed(old[2], key, subject=subject)
                # Learned, not told: it waits for a person to confirm it.
                if knowledge.remember(key, value, subject=subject, kind=kind, source="observed",
                                      confidence=round(float(pattern.confidence), 3),
                                      salience=0.8, respect_stated=True, status="pending"):
                    written += 1
            except Exception as exc:
                _LOGGER.debug("knowledge promote failed for %r: %s", key, exc)
        return written

    def _store_person_pattern(self, pattern: DetectedPattern) -> bool:
        """Keep a routine owned by one person in their routine store, keyed on
        what the routine is so a new analysis updates one row. A daily routine
        is only a person's when it is something a person does (a light, a
        lock, the blinds), never a tracker, sensor or helper changing."""
        owner = pattern.details.get("person")
        if not owner:
            return False
        from ..cognitive import routines
        entity = pattern.entity_ids[0] if pattern.entity_ids else ""
        data = dict(pattern.details or {})
        if pattern.pattern_type == routines.TIME_ROUTINE:
            if not routines.is_person_routine(entity, str(data.get("state", ""))):
                return False
            data["entity_id"] = entity
        from .. import person_patterns
        return person_patterns.store(
            owner, pattern.pattern_type, pattern.description,
            data=data, confidence=pattern.confidence, occurrences=pattern.occurrences,
            db_path=self._db, key=routines.routine_key(pattern.pattern_type, entity, data))

    def get_person_patterns(self, person: Optional[str] = None) -> list[dict]:
        """Stored person routines, optionally for one (normalised) person."""
        from .. import person_patterns
        return person_patterns.read(person, db_path=self._db)

    # ── suggestions (SuggestionStore, in patterns.db) ────────────────────────

    def _suggestions(self) -> SuggestionStore:
        return SuggestionStore(self._db)

    def _installable(self, pattern: DetectedPattern) -> bool:
        """Whether the suggestion would be a real automation (something to
        drive, with a full trigger and action) rather than advice."""
        try:
            return bool(normalize_suggestion_automation(
                self._generate_automation(pattern)).get("installable"))
        except Exception:
            return False

    def _store_rejected(self, pattern: DetectedPattern) -> bool:
        """Keep a suggestion the AI review turned down, so it is not suggested
        again (the panel can restore it)."""
        return self._suggestions().store(pattern, generate=self._generate_automation,
                                         status=SUGGESTION_REJECTED)

    def _store_suggestion(self, pattern: DetectedPattern) -> bool:
        """Store a pattern as a suggestion; True when it is new."""
        return self._suggestions().store(pattern, generate=self._generate_automation)

    def _generate_automation(self, pattern: DetectedPattern) -> str:
        """The automation JSON for a pattern."""
        return generate_automation(pattern)

    def get_pending_suggestions(self) -> list[dict]:
        """The suggestions waiting for a person."""
        return self._suggestions().pending()

    def get_suggestion(self, suggestion_id: int) -> Optional[dict]:
        """One suggestion row by id, or None."""
        return self._suggestions().get(suggestion_id)

    def mark_installed(self, suggestion_id: int, automation_id: str) -> None:
        """Record that an approved suggestion became a live automation."""
        self._suggestions().mark_installed(suggestion_id, automation_id)

    def mark_covered(self, suggestion_id: int) -> None:
        """Retire a suggestion Home Assistant now has an equal automation for."""
        self._suggestions().mark_covered(suggestion_id)

    def get_rejected_suggestions(self) -> list[dict]:
        """Suggestions the AI review turned down."""
        return self._suggestions().rejected()

    def restore_suggestion(self, suggestion_id: int) -> bool:
        """Bring a suggestion the AI review rejected back to a person."""
        return self._suggestions().restore(suggestion_id)

    def approve_suggestion(self, suggestion_id: int) -> bool:
        """Mark a suggestion approved."""
        return self._suggestions().approve(suggestion_id)

    def dismiss_suggestion(self, suggestion_id: int) -> bool:
        """Mark a suggestion dismissed."""
        return self._suggestions().dismiss(suggestion_id)

    def get_stats(self) -> dict:
        """Counts for the panel."""
        return self._suggestions().stats()


def _history_point(item) -> tuple:
    """(state, when) from a recorder history item, which is a State object or,
    with minimal_response, sometimes a plain dict."""
    value = getattr(item, "state", None)
    when = getattr(item, "last_changed", None) or getattr(item, "last_updated", None)
    if value is None and isinstance(item, dict):
        value = item.get("state")
        when = item.get("last_changed") or item.get("last_updated")
    return value, when


_ANALYZER = PatternAnalyzer()


def get_analyzer() -> PatternAnalyzer:
    return _ANALYZER
