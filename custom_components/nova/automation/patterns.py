"""Nova pattern learning: detect repeating household behaviour.

Reads ``state_changes`` and ``commands`` from patterns.db, identifies
repeating patterns and hands the confident ones to the suggestion store. Runs
every 6 hours once enough data exists (7+ days), and on demand.

Pattern types detected:
  1. Time-based routines: "Lights turned off every night around 10:30 PM"
  2. Sequence patterns: "Front door locks 5 min after garage closes"
  3. Repeated commands: "Turn off kitchen lights" said 3x/day at similar times
  4. Numeric triggers: an action that follows a sensor crossing a threshold
  5. Presence-triggered: lights on when arriving, off when leaving

Rows attributed to an automation (``triggered_by`` automation or
nova_automation) never train a pattern. Each pattern gets a confidence score
(0-1); patterns at or above the effective threshold become suggestions.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
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
from .suggestions import (  # DB_PATH / MIN_DAYS are shared with stats
    DB_PATH,
    _db_path,
    MIN_DAYS,
    SuggestionStore,
    _name_for,
    _trigger_phrase,
    generate_automation,
    normalize_suggestion_automation,
    service_for,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MIN_OCCURRENCES = 5    # Pattern must repeat this many times
CONFIDENCE_THRESHOLD = 0.65  # Minimum to create a suggestion
ANALYSIS_INTERVAL = 21600    # 6 hours between analyses
KNOWLEDGE_FACT_CONFIDENCE = 0.75  # routines/commands above this also become observed facts
PERSON_DOMINANCE_RATIO = 0.8      # a person must account for this share of a
                                   # pattern's occurrences to own it, vs. household
_AUTOMATED_SOURCE_SQL = "('automation','nova_automation')"


def _source_filter(conn: sqlite3.Connection, alias: str = "") -> str:
    """SQL predicate excluding known automation-produced rows.

    ``patterns.db`` is migrated by StateLogger, but keeping the analyzer
    tolerant of a legacy/minimal table makes recovery and isolated tests fail
    open: rows without a provenance column are old/unknown, not automated.
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(state_changes)")}
        if "triggered_by" in cols:
            prefix = f"{alias}." if alias else ""
            return (f"COALESCE({prefix}triggered_by, 'system') NOT IN "
                    f"{_AUTOMATED_SOURCE_SQL}")
    except Exception:
        pass
    return "1=1"


def _days_since(timestamp, now: datetime) -> float:
    """Whole calendar days from a stored (local, naive) ISO timestamp to
    now, so the answer does not depend on the time of day the analysis runs;
    0.0 when it cannot be read, which never makes a routine look stale."""
    try:
        ts = datetime.fromisoformat(str(timestamp))
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        return float(max(0, (now.date() - ts.date()).days))
    except (TypeError, ValueError):
        return 0.0


def _near_hours(hour: int) -> tuple:
    return ((hour - 1) % 24, hour, (hour + 1) % 24)


def set_thresholds(min_occurrences: int | None = None,
                   confidence: float | None = None) -> None:
    """Loosen/tighten the pattern engine at runtime (panel-configurable). The
    cognitive tick calls this with the user's settings before each analysis."""
    global MIN_OCCURRENCES, CONFIDENCE_THRESHOLD
    if min_occurrences is not None:
        try:
            MIN_OCCURRENCES = max(2, int(min_occurrences))
        except Exception:
            pass
    if confidence is not None:
        try:
            CONFIDENCE_THRESHOLD = min(0.95, max(0.3, float(confidence)))
        except Exception:
            pass


# Adaptive suggestion threshold (opt-in): when enabled, how welcome recent
# suggestions were nudges the confidence bar for creating new ones — mostly
# dismissed as unneeded → stricter, almost all acted on → slightly looser. The
# delta is bounded and the result is clamped, so it can never run away, and it
# is derived ONLY from "suggestion" outcomes — it never touches intrusion,
# lockdown, or any security/safety decision.
_ADAPT_CACHE = {"ts": 0.0, "delta": 0.0}
_ADAPT_MIN_JUDGED = 5           # need this many judged suggestions before moving
_ADAPT_WINDOW_S = 30 * 86400.0  # look back a month


def _learned_threshold_delta() -> float:
    """Bounded adjustment (in [-0.07, +0.15]) to the suggestion confidence bar,
    learned from how recent suggestions were received. Returns 0.0 when the
    opt-in is off, on any error, or with too little evidence. Cached 5 min."""
    try:
        from .. import nova_config
        if not nova_config.get("adaptive_suggestion_threshold", False):
            return 0.0
    except Exception:
        return 0.0
    now = time.time()
    if now - _ADAPT_CACHE["ts"] < 300.0:
        return _ADAPT_CACHE["delta"]
    delta = 0.0
    try:
        from .. import decision_record
        r = decision_record.outcome_rate("suggestion", window_s=_ADAPT_WINDOW_S)
        if int(r.get("judged", 0)) >= _ADAPT_MIN_JUDGED:
            delta = decision_record.threshold_delta_from_rate(
                r.get("unwelcome_rate"))
    except Exception:
        delta = 0.0
    _ADAPT_CACHE.update(ts=now, delta=delta)
    return delta


def _effective_threshold() -> float:
    """CONFIDENCE_THRESHOLD adjusted by the learned delta, clamped [0.3, 0.95]."""
    return min(0.95, max(0.3, CONFIDENCE_THRESHOLD + _learned_threshold_delta()))


def _time_window_condition(epochs: list) -> Optional[dict]:
    """If the action times cluster into a clear daily window — a contiguous span
    with a quiet period of at least 8 hours around it — return a Home Assistant
    time condition ``{"condition": "time", "after": "HH:MM:SS", "before": ...}``;
    otherwise None. Uses local clock hours from the stored timestamps. Pure.

    This is the tractable "And if": a motion→light pair that only ever happens in
    the evening gets a time window so the suggested automation won't fire the
    light at noon. Handles overnight windows (the HA time condition wraps).
    """
    if len(epochs) < MIN_OCCURRENCES:
        return None
    hours = sorted({datetime.fromtimestamp(e).hour for e in epochs})
    if len(hours) == 1:
        h = hours[0]
        return {"condition": "time",
                "after": f"{(h - 1) % 24:02d}:00:00",
                "before": f"{(h + 1) % 24:02d}:00:00"}
    # Largest circular gap between consecutive occurrence hours = the quiet period;
    # the active window is its complement.
    largest_gap = 0
    gap_start = gap_end = hours[0]
    for i in range(len(hours)):
        cur = hours[i]
        nxt = hours[(i + 1) % len(hours)]
        gap = (nxt - cur) % 24
        if gap > largest_gap:
            largest_gap, gap_start, gap_end = gap, cur, nxt
    if largest_gap < 8:
        return None                       # spread across the day: no clear window
    return {"condition": "time",
            "after": f"{gap_end:02d}:00:00",
            "before": f"{(gap_start + 1) % 24:02d}:00:00"}


def _is_dark_at(epoch: float, lat: float, lon: float) -> Optional[bool]:
    """True if the sun was below the horizon at ``epoch`` for the given location,
    False if above, None if it can't be determined (astral missing/failure).
    Thin wrapper around astral; the decision logic lives in _sun_condition so it
    stays testable without astral."""
    try:
        from astral import LocationInfo
        from astral.sun import sun as astral_sun
        from datetime import timezone
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
        loc = LocationInfo(latitude=float(lat), longitude=float(lon))
        s = astral_sun(loc.observer, date=dt.date(), tzinfo=timezone.utc)
        return dt < s["sunrise"] or dt > s["sunset"]
    except Exception:
        return None


def _sun_condition(epochs: list, lat, lon) -> Optional[dict]:
    """If the action consistently happens after dark, return an HA sun condition
    ``{"condition": "sun", "after": "sunset", "before": "sunrise"}``; else None.
    More precise than a fixed time window for "when it's dark" patterns because
    it tracks the seasonal sunrise/sunset instead of a fixed clock time.
    """
    if lat is None or lon is None or len(epochs) < MIN_OCCURRENCES:
        return None
    dark = 0
    total = 0
    for e in epochs:
        d = _is_dark_at(e, lat, lon)
        if d is None:
            continue
        total += 1
        if d:
            dark += 1
    if total < MIN_OCCURRENCES:
        return None
    if dark / total >= 0.8:               # consistently after dark
        return {"condition": "sun", "after": "sunset", "before": "sunrise"}
    return None


class _IdNames(dict):
    """A names map that names every entity by its entity_id (the wording
    Nova used before v7.125)."""

    def get(self, key, default=None):
        return key


_AS_IDS = _IdNames()


def _condition_phrase(cond, names: Optional[dict] = None) -> str:
    """Human tail for a pattern description given its learned condition(s).
    Accepts a single condition dict, a list of them (ANDed), or None.
    Entities are named with `names` (entity_id -> friendly name); pass
    {entity_id: entity_id} for the pre-v7.125 wording."""
    if isinstance(cond, list):
        return "".join(_condition_phrase(c, names) for c in cond)
    if not isinstance(cond, dict):
        return ""
    kind = cond.get("condition")
    if kind == "sun":
        return ", mostly after dark"
    if kind == "time":
        return f", mostly between {cond.get('after', '')[:5]} and {cond.get('before', '')[:5]}"
    if kind == "numeric_state":
        ent = _name_for(cond.get("entity_id", ""), names)
        if "below" in cond:
            return f", mostly while {ent} is below {cond['below']:g}"
        if "above" in cond:
            return f", mostly while {ent} is above {cond['above']:g}"
    if kind == "state":
        ent = _name_for(cond.get("entity_id", ""), names)
        st = cond.get("state", "")
        return f", only when {ent} is {st}"
    return ""


def _numeric_value_at(epochs: list, values: list, t: float):
    """Value of a numeric series (sorted epochs + parallel values) at/just before
    time ``t``; None if ``t`` precedes the first reading. Pure, bisect-based."""
    import bisect
    if not epochs:
        return None
    i = bisect.bisect_right(epochs, t) - 1
    if i < 0:
        return None
    return values[i]


def _nice_threshold(v: float, op: str) -> float:
    """Round a raw boundary to a whole-number threshold that still *includes* the
    observed side: for 'below', one above the floor; for 'above', one below the
    ceil."""
    import math
    return float(math.floor(v) + 1) if op == "below" else float(math.ceil(v) - 1)


def _numeric_trigger_from(occ: list, baseline: list) -> Optional[dict]:
    """Given a sensor's values AT an action's occurrences (``occ``) and its
    overall values (``baseline``), decide whether the action consistently fires
    on one side of a threshold. Returns ``{"below": T}`` / ``{"above": T}`` / None.

    Guards against spurious correlation: the occurrence values must sit clearly
    in the low (or high) part of the sensor's range AND the sensor must spend
    real time on the *other* side of the threshold (a genuine crossing) — so a
    sensor that is simply always low never yields a bogus "below" trigger.
    """
    import statistics
    if len(occ) < MIN_OCCURRENCES or len(baseline) < 10:
        return None
    occ_s = sorted(occ)
    base_s = sorted(baseline)

    def pct(a, p):
        return a[min(len(a) - 1, max(0, int(round(p / 100.0 * (len(a) - 1)))))]

    base_med = statistics.median(base_s)
    occ_med = statistics.median(occ_s)
    base_p10, base_p90 = pct(base_s, 10), pct(base_s, 90)
    rng = base_p90 - base_p10
    if rng <= 0:
        return None                                   # flat/constant sensor

    # BELOW: occurrences concentrated low; sensor clearly rises above the bound.
    occ_p90 = pct(occ_s, 90)
    if occ_med < base_med and occ_p90 < base_med:
        T = _nice_threshold(occ_p90, "below")
        if base_p90 > T + 0.1 * rng:
            return {"below": T}

    # ABOVE: mirror image.
    occ_p10 = pct(occ_s, 10)
    if occ_med > base_med and occ_p10 > base_med:
        T = _nice_threshold(occ_p10, "above")
        if base_p10 < T - 0.1 * rng:
            return {"above": T}

    return None


# Devices a numeric threshold may drive, and how soon after the crossing the
# action has to follow for the sensor to count as its trigger (v7.126.0).
NUMERIC_ACTION_DOMAINS = ("light", "switch", "cover", "lock", "climate", "fan",
                          "media_player", "humidifier", "water_heater", "valve")
NUMERIC_TRIGGER_WINDOW = 600.0
# A threshold automation acts on every crossing, so a crossing must be
# followed by the action at least this often (sequences use 0.3).
NUMERIC_MIN_CONDITIONAL = 0.5
# Sequence rules (v7.126.1). A sequence becomes an automation that acts on
# every trigger, so the trigger must be followed by the action at least this
# often; with no area to compare, the bar is higher.
SEQUENCE_MIN_CONDITIONAL = 0.5
SEQUENCE_NO_AREA_CONDITIONAL = 0.7
# Two different areas: only a link that holds almost every time (the hall
# door and the kitchen light, every evening) is believable.
SEQUENCE_CROSS_AREA_CONDITIONAL = 0.8
SEQUENCE_CANDIDATES = 60
# Triggers that are someone arriving or leaving: no area of their own.
PRESENCE_TRIGGER_DOMAINS = ("person", "device_tracker")


def _sequence_link_ok(trigger: str, action: str, conditional: float,
                      areas: dict) -> bool:
    """Whether a learned "after A, B" is a believable automation. Pure."""
    if conditional < SEQUENCE_MIN_CONDITIONAL:
        return False
    if trigger.split(".", 1)[0] in PRESENCE_TRIGGER_DOMAINS:
        return True
    t_area, a_area = areas.get(trigger), areas.get(action)
    if t_area and a_area:
        return t_area == a_area or conditional >= SEQUENCE_CROSS_AREA_CONDITIONAL
    return conditional >= SEQUENCE_NO_AREA_CONDITIONAL


# Most new suggestions the AI review judges in one analysis pass; the rest
# wait for the next pass (bounds cost and time).
REVIEW_MAX_PER_PASS = 10


def _threshold_crossings(series: list, op: str, threshold: float) -> list:
    """Epochs at which a numeric series crosses ``threshold`` in the
    direction of ``op`` ("below" or "above"). Pure."""
    events = sorted(series)
    out: list = []
    for (_t0, before), (t1, after) in zip(events, events[1:]):
        if op == "below" and before >= threshold > after:
            out.append(t1)
        elif op == "above" and before <= threshold < after:
            out.append(t1)
    return out


def _prepare_numeric_history(sensor_hist: dict) -> dict:
    """``{sensor_id: (epochs, values)}`` sorted by time, numeric readings only,
    for sensors with at least 10 of them. Built once per analysis so each
    learned pattern does not re-filter and re-sort every sensor's series."""
    prepared: dict = {}
    for s_ent, events in (sensor_hist or {}).items():
        ev = sorted((e, v) for e, v in events if isinstance(v, (int, float)))
        if len(ev) >= 10:
            prepared[s_ent] = ([e for e, _ in ev], [v for _, v in ev])
    return prepared


def _numeric_condition(times: list, sensor_hist: dict,
                       prepared: Optional[dict] = None) -> Optional[dict]:
    """Best numeric_state *condition* for an action whose occurrences (``times``)
    consistently coincide with a sensor sitting on one side of a threshold — e.g.
    "…and only while the temperature is below 62". Returns a self-describing HA
    condition dict, or None. Reuses the same scorer as the numeric trigger, so it
    shares the anti-spurious guards. ``sensor_hist`` maps sensor_id -> ``[(epoch,
    float)]``."""
    if not sensor_hist or len(times) < MIN_OCCURRENCES:
        return None
    if prepared is None:
        prepared = _prepare_numeric_history(sensor_hist)
    best = None
    best_cover = 0
    for s_ent, (epochs, values) in prepared.items():
        occ = [v for v in (_numeric_value_at(epochs, values, t) for t in times)
               if v is not None]
        if len(occ) < MIN_OCCURRENCES:
            continue
        trig = _numeric_trigger_from(occ, values)
        if not trig:
            continue
        op, T = next(iter(trig.items()))
        # Prefer the sensor whose readings cover the most occurrences.
        if len(occ) > best_cover:
            best_cover = len(occ)
            best = {"condition": "numeric_state", "entity_id": s_ent, op: T}
    return best


class PatternAnalyzer:
    """Analyzes accumulated state change data for behavioral patterns."""

    def __init__(self):
        self._last_analysis: float = 0.0
        self._last_result: dict = {}
        self._last_patterns: list[DetectedPattern] = []
        self._db = _db_path()
        # entity_id -> friendly name, read on the event loop at the start of
        # each analysis (the detectors run in an executor, away from states).
        self._names: dict = {}
        self._numeric_ran = False
        self._completed_types: set = set()
        # Single flight: a manual run and the scheduled run never overlap.
        self._analysis_lock = asyncio.Lock()

    @property
    def analysis_running(self) -> bool:
        return self._analysis_lock.locked()

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            if not Path(self._db).exists():
                return None
            conn = sqlite3.connect(self._db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.row_factory = sqlite3.Row
            return conn
        except Exception:
            return None

    def should_analyze(self) -> bool:
        """Check if enough data and time has passed for analysis."""
        if (time.time() - self._last_analysis) < ANALYSIS_INTERVAL:
            return False
        conn = self._connect()
        if not conn:
            return False
        try:
            source_filter = _source_filter(conn)
            oldest = conn.execute(
                f"SELECT MIN(timestamp) FROM state_changes WHERE {source_filter}"
            ).fetchone()[0]
            if not oldest:
                return False
            days = (datetime.now() - datetime.fromisoformat(oldest)).days
            count = conn.execute(
                f"SELECT COUNT(*) FROM state_changes WHERE {source_filter}"
            ).fetchone()[0]
            conn.close()
            return days >= MIN_DAYS and count >= 50
        except Exception:
            return False

    def pattern_diagnostic(self) -> dict:
        """Explain why routines may not be forming: the busiest sources (flood
        check) and the strongest routine CANDIDATES with their distinct-day
        coverage — so a near-miss (seen on almost enough days, or split across
        adjacent hours) is visible instead of just "0 found". Pure DB read.
        """
        out: dict[str, Any] = {"top_sources": [], "candidates": [], "total_days": 0,
               "min_days": 0, "min_occurrences": MIN_OCCURRENCES}
        conn = self._connect()
        if not conn:
            return out
        try:
            source_filter = _source_filter(conn)
            total_days = int((conn.execute(
                "SELECT COUNT(DISTINCT date(timestamp)) FROM state_changes "
                "WHERE timestamp > datetime('now', '-30 days') AND "
                + source_filter).fetchone()[0]) or 0)
            out["total_days"] = total_days
            # coverage >= 0.3 -> need ceil(0.3 * total_days) distinct days
            out["min_days"] = (total_days * 3 + 9) // 10 if total_days else 0
            for e, c in conn.execute(
                    "SELECT entity_id, COUNT(*) c FROM state_changes "
                    "WHERE timestamp > datetime('now', '-30 days') "
                    "AND " + source_filter + " "
                    "GROUP BY entity_id ORDER BY c DESC LIMIT 10"):
                out["top_sources"].append({"entity_id": e, "changes": int(c)})
            for e, s, h, cnt, days in conn.execute(
                    "SELECT entity_id, new_state, hour, COUNT(*) cnt, "
                    "COUNT(DISTINCT date(timestamp)) days FROM state_changes "
                    "WHERE timestamp > datetime('now', '-30 days') "
                    "AND " + source_filter + " "
                    "GROUP BY entity_id, new_state, hour HAVING cnt >= 2 "
                    "ORDER BY days DESC, cnt DESC LIMIT 12"):
                out["candidates"].append({
                    "entity_id": e, "state": s, "hour": int(h),
                    "occurrences": int(cnt), "days": int(days),
                    "coverage": round(int(days) / total_days, 2) if total_days else 0.0,
                })
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return out

    async def analyze(self, hass: HomeAssistant,
                      reviewer=None) -> list[DetectedPattern]:
        """Run full pattern analysis. Returns detected patterns.

        ``reviewer`` (v7.126.0) is an optional ``async (hass, pattern) ->
        verdict | None`` that judges each NEW suggestion before it is stored
        (the AI suggestion review, see suggestion_review.py). It can only
        reject: a rejected suggestion is stored as rejected and never
        suggested again; one it cannot judge is not stored this pass.

        Single flight: a caller that arrives while an analysis is running
        waits for it and gets its result instead of starting a second one."""
        if self._analysis_lock.locked():
            async with self._analysis_lock:
                return list(self._last_patterns)
        async with self._analysis_lock:
            # Without a reviewer, call exactly as before (a patch point).
            patterns = await (self._analyze_once(hass, reviewer=reviewer)
                              if reviewer is not None else self._analyze_once(hass))
            self._last_patterns = list(patterns)
            return patterns

    async def _analyze_once(self, hass: HomeAssistant,
                            reviewer=None) -> list[DetectedPattern]:
        self._last_analysis = time.time()

        # Everything that needs the event loop (the state machine, the
        # recorder's own executor) is gathered first; every SQLite read then
        # runs in ONE executor job that opens, uses and closes its own
        # connection, so a connection never crosses threads.
        person_map = self._person_entity_map(hass)
        try:
            from ..cognitive.naming import names_from_states
            self._names = names_from_states(hass.states.async_all())
        except Exception:
            self._names = {}
        self._person_map = person_map
        try:
            _sensor_hist = await self._fetch_numeric_sensor_history(hass)
        except Exception:
            _sensor_hist = {}
        try:
            _area_presence = await self._fetch_area_presence_context(hass)
        except Exception:
            _area_presence = EMPTY_CONTEXT
        # Areas of every entity (directly or via its device): the threshold
        # and sequence detectors only link things in the same area.
        try:
            ids = list(_sensor_hist) + [st.entity_id for st in hass.states.async_all()]
            _entity_areas = await self._fetch_entity_areas(hass, dict.fromkeys(ids))
        except Exception:
            _entity_areas = {}
        _lat = getattr(hass.config, "latitude", None)
        _lon = getattr(hass.config, "longitude", None)
        self._numeric_ran = False
        self._completed_types = set()
        patterns = await hass.async_add_executor_job(
            self._detect_patterns, person_map, _lat, _lon, _sensor_hist,
            _area_presence, _entity_areas)
        if patterns is None:          # no patterns.db yet
            return []

        # Store high-confidence patterns as suggestions
        new_suggestions = 0
        already_automated = 0
        new_person_patterns = 0
        # The adaptive delta reads decisions.db, so resolve it off the loop.
        _eff_threshold = await hass.async_add_executor_job(_effective_threshold)
        near_misses: list = []
        # A sequence stores when count/(MIN_OCCURRENCES*3) >= threshold; surface
        # how many recurrences a not-yet-stored one still needs.
        _seq_needed = int(_eff_threshold * MIN_OCCURRENCES * 3)
        if _eff_threshold * MIN_OCCURRENCES * 3 > _seq_needed:
            _seq_needed += 1
        reviewed = rejected = deferred = 0
        kept: list = []
        for p in patterns:
            if p.confidence >= _eff_threshold:
                if not self._installable(p):
                    # Not something Nova can turn into an automation (a
                    # read-only device, a voice command): not a suggestion.
                    # A person's routine is still recorded below.
                    if p.details.get("person"):
                        if await hass.async_add_executor_job(
                                self._store_person_pattern, p):
                            new_person_patterns += 1
                    continue
                kept.append(p)
                match = self._automation_match(hass, p)
                p.details["automation_match"] = match
                if match.get("status") == "already_automated":
                    # An automation already does this (exactly, or the same
                    # action on the same device from another trigger).
                    already_automated += 1
                    continue
                verdict = None
                if reviewer is not None:
                    found = await hass.async_add_executor_job(
                        self._suggestions().lookup, p)
                    needs = found is None or (
                        found["status"] == SUGGESTION_PENDING
                        and not found["reviewed"])
                    if needs:
                        if reviewed >= REVIEW_MAX_PER_PASS:
                            deferred += 1
                            if found is None:
                                continue   # reviewed on a later pass
                        else:
                            reviewed += 1
                            try:
                                verdict = await reviewer(hass, p)
                            except Exception:
                                verdict = None
                            if verdict is None:
                                deferred += 1
                                if found is None:
                                    continue   # not judged: not shown yet
                            else:
                                p.details["review"] = verdict
                                if verdict.get("verdict") == "reject":
                                    rejected += 1
                                    if found is None:
                                        await hass.async_add_executor_job(
                                            self._store_rejected, p)
                                    else:
                                        await hass.async_add_executor_job(
                                            self._suggestions().reject,
                                            found["id"], verdict)
                                    continue
                stored = await hass.async_add_executor_job(
                    self._store_suggestion, p)
                if stored:
                    new_suggestions += 1
                # v6.41.0: patterns confidently owned by one person also land
                # in person_patterns — the dedicated per-person routine store
                # (independent of the household suggestions/automations flow).
                if p.details.get("person"):
                    if await hass.async_add_executor_job(
                            self._store_person_pattern, p):
                        new_person_patterns += 1
            elif p.occurrences >= MIN_OCCURRENCES and len(near_misses) < 8:
                # Detected but below the store bar — show it building so "not
                # enough data yet" is distinguishable from "nothing detected".
                near_misses.append({
                    "type": p.pattern_type,
                    "description": p.description,
                    "occurrences": p.occurrences,
                    "needed": _seq_needed if p.pattern_type == "sequence" else None,
                })

        # Promote the most reliable routines/commands into the curated knowledge
        # store as *observed* facts, so they surface in the Memory tab (marked ~)
        # and inject into conversation. Sequences/presence stay as automations only.
        # v6.41.0: a pattern confidently owned by one person is attributed to
        # that person's knowledge subject rather than "household".
        promoted = await hass.async_add_executor_job(
            self._promote_to_knowledge, patterns)

        # Record the outcome of this pass so the panel can show "last analysis:
        # ran at T, N found, M stored" — the difference between "never ran" and
        # "ran, found nothing worth surfacing".
        # Pending suggestions this pass no longer supports (not detected, below
        # the bar, or not automatable) are retired: hidden, and pending again
        # if detected later. Only for detectors that completed this pass.
        retired = 0
        for ptype in sorted(self._completed_types):
            retired += await hass.async_add_executor_job(
                self._suggestions().retire_missing, ptype,
                [p for p in kept if p.pattern_type == ptype])

        self._last_result = {
            "ts": time.time(),
            "patterns_found": len(patterns),
            "new_suggestions": new_suggestions,
            "already_automated": already_automated,
            "reviewed": reviewed,
            "rejected_by_review": rejected,
            "review_deferred": deferred,
            "retired": retired,
            "person_routines": new_person_patterns,
            "facts": promoted,
            "near_misses": near_misses,
        }

        if patterns:
            _LOGGER.info(
                "Pattern analysis: %d patterns found, %d new suggestions, "
                "%d facts learned, %d person routines (threshold=%.0f%%)",
                len(patterns), new_suggestions, promoted, new_person_patterns,
                _eff_threshold * 100,
            )

        return patterns

    def _detect_patterns(
        self, person_map: dict, lat, lon, sensor_hist: dict,
        area_presence: AreaPresenceContext = EMPTY_CONTEXT,
        entity_areas: Optional[dict] = None,
    ) -> Optional[list[DetectedPattern]]:
        """Run every detector over one connection. SYNC — executor only.

        The connection is created, used and closed in this one thread
        (sqlite3 refuses a connection used from another thread). Returns None
        when patterns.db does not exist yet. A detector error keeps the
        patterns found before it, as analysis always has."""
        conn = self._connect()
        if not conn:
            return None
        patterns: list[DetectedPattern] = []
        try:
            done = self._completed_types
            patterns.extend(self._find_time_routines(conn, person_map))
            done.add("time_routine")
            patterns.extend(self._find_repeated_commands(conn))
            done.add("repeated_command")
            patterns.extend(self._find_sequence_patterns(
                conn, lat, lon, sensor_hist, area_presence, entity_areas))
            done.add("sequence")
            patterns.extend(self._find_numeric_triggers(
                conn, sensor_hist, area_presence, entity_areas))
            self._numeric_ran = bool(sensor_hist)
            if self._numeric_ran:
                done.add("numeric_trigger")
            # The old "presence" detector (v7.126.1: no longer run) paired
            # every change within 5 minutes of an arrival, with no check of
            # how often an arrival is followed by it, and duplicated what the
            # sequence detector finds for person and tracker triggers.
            done.add("presence")
        except Exception as exc:
            _LOGGER.warning("Pattern analysis error: %s", exc)
        finally:
            conn.close()
        for p in patterns:
            names = self._names_for(p)
            if names:
                p.details["names"] = names
        return patterns

    def _automation_match(self, hass, pattern: DetectedPattern) -> dict:
        """Compare one generated automation with HA's cached live inventory."""
        try:
            norm = normalize_suggestion_automation(self._generate_automation(pattern))
            if not norm.get("installable"):
                return {"status": "advisory", "matches": [],
                        "reason": norm.get("reason", "not installable")}
            candidate = {
                "triggers": norm["trigger"],
                "conditions": norm.get("condition") or [],
                "actions": norm["action"],
                "mode": norm.get("mode", "single"),
            }
            from .inventory import get_inventory
            inventory = get_inventory(hass)
            if inventory is None:
                return {"status": "inventory_unavailable", "matches": [],
                        "reason": "automation inventory unavailable"}
            from .matching import classify_result
            result = classify_result(candidate, inventory.records())
            if result.status == MATCH_OVERLAP and result.same_effect:
                # Another automation already performs this action on this
                # device from a different trigger: not a new automation.
                return {"status": MATCH_EXACT,
                        "matches": [m.to_dict() for m in result.matches],
                        "reason": "a loaded automation already performs this "
                                  "action on the same device"}
            return result.to_dict()
        except Exception:
            return {"status": "inventory_unavailable", "matches": [],
                    "reason": "automation comparison failed"}

    def _person_entity_map(self, hass) -> dict:
        """Map every way a person might be recorded (friendly name, normalized
        name, or entity_id) -> that person's entity_id, so a routine's learned
        owner can be resolved to a conditionable ``person.*`` entity."""
        out: dict = {}
        try:
            from ..identity import normalize
        except Exception:
            def normalize(name: str) -> str:
                return "_".join((name or "").strip().lower().split())
        try:
            for st in hass.states.async_all("person"):
                ent = st.entity_id
                fn = st.attributes.get("friendly_name") or ent.split(".", 1)[-1]
                for k in (fn, normalize(fn), ent):
                    if k:
                        out[k] = ent
        except Exception:
            return {}
        return out

    def _name(self, entity_id: str) -> str:
        """How a person reads this entity (friendly name, else readable id)."""
        return _name_for(entity_id, getattr(self, "_names", None))

    def _person_label(self, person: str) -> str:
        """A recorded person ("abi") as their person entity's friendly name."""
        ent = (getattr(self, "_person_map", None) or {}).get(person)
        name = (getattr(self, "_names", None) or {}).get(ent) if ent else None
        return str(name or person)

    def _names_for(self, pattern: DetectedPattern) -> dict:
        """The friendly names of every entity a pattern mentions, carried in
        its details so wording built later (evidence, automation names) can
        use them."""
        names = getattr(self, "_names", None) or {}
        found: dict = {}

        def walk(value):
            if isinstance(value, str):
                if value in names:
                    found[value] = names[value]
            elif isinstance(value, dict):
                for v in value.values():
                    walk(v)
            elif isinstance(value, (list, tuple)):
                for v in value:
                    walk(v)
        walk(pattern.entity_ids)
        walk(pattern.details)
        return found

    def _find_time_routines(self, conn: sqlite3.Connection,
                            person_map: Optional[dict] = None) -> list[DetectedPattern]:
        """Find entities that change state at similar times each day."""
        from ..cognitive import patterns as scoring
        patterns = []

        # Group state changes by entity + action, look for time clustering
        source_filter = _source_filter(conn)
        rows = conn.execute(f"""
            SELECT entity_id, new_state, hour, day_of_week, COUNT(*) as cnt
            FROM state_changes
            WHERE timestamp > datetime('now', '-30 days')
              AND {source_filter}
            GROUP BY entity_id, new_state, hour
            HAVING cnt >= ?
            ORDER BY cnt DESC
        """, (MIN_OCCURRENCES,)).fetchall()

        # Opportunity days: distinct days we were observing at all (constant
        # across rows — computed once, was previously re-run per row and made a
        # large history crawl).
        total_days = conn.execute(f"""
            SELECT COUNT(DISTINCT date(timestamp)) FROM state_changes
            WHERE timestamp > datetime('now', '-30 days')
              AND {source_filter}
        """).fetchone()[0] or 1

        automated_sql = ("COALESCE(triggered_by, 'system') IN " + _AUTOMATED_SOURCE_SQL
                         if source_filter != "1=1" else "0")
        now = datetime.now()

        for row in rows:
            entity = row["entity_id"]
            state = row["new_state"]
            hour = row["hour"]
            count = row["cnt"]

            # Positive days: distinct days this routine ACTUALLY happened. Using
            # distinct days (not raw event count) so several same-hour events on
            # one day count once — the honest "on N of M days" numerator.
            positive_days, last_ts = conn.execute(f"""
                SELECT COUNT(DISTINCT date(timestamp)), MAX(timestamp) FROM state_changes
                WHERE entity_id = ? AND new_state = ? AND hour = ?
                  AND timestamp > datetime('now', '-30 days')
                  AND {source_filter}
            """, (entity, state, hour)).fetchone()
            positive_days = positive_days or 0

            # Coverage weighs the negative evidence: a routine on 42 of 45 days
            # (0.93) is far stronger than one on 42 of 120 days (0.35), even
            # though both were "seen 42 times". Cheap rejections first, so the
            # extra evidence below is only read for plausible routines.
            coverage = positive_days / total_days if total_days else 0.0
            negative_days = max(0, total_days - positive_days)
            if coverage < scoring.MIN_COVERAGE or positive_days < scoring.MIN_DISTINCT_DAYS:
                continue

            # Time concentration: days this behaviour happened within an hour
            # either side. Provenance: how much of it automations caused.
            window_days = conn.execute(f"""
                SELECT COUNT(DISTINCT date(timestamp)) FROM state_changes
                WHERE entity_id = ? AND new_state = ? AND hour IN (?, ?, ?)
                  AND timestamp > datetime('now', '-30 days')
                  AND {source_filter}
            """, (entity, state, *_near_hours(hour))).fetchone()[0] or 0
            automated = conn.execute(f"""
                SELECT COUNT(*) FROM state_changes
                WHERE entity_id = ? AND new_state = ? AND hour = ?
                  AND timestamp > datetime('now', '-30 days')
                  AND {automated_sql}
            """, (entity, state, hour)).fetchone()[0] or 0

            # Confidence = coverage, discounted for a small sample so a 3-of-3
            # (1.0) can't outrank a 40-of-45 (0.89) on three data points, then
            # for spread-out timing and for a routine that has stopped
            # (cognitive/patterns.py explains each factor).
            score = scoring.score_time_routine(
                scoring.RoutineEvidence(
                    observations=count, positive_days=positive_days,
                    eligible_days=total_days, window_days=window_days,
                    days_since_last=_days_since(last_ts, now),
                    automated_observations=automated),
                min_occurrences=MIN_OCCURRENCES)
            if not score.accepted:
                continue
            confidence = score.confidence

            time_str = f"{hour:02d}:00"
            details = {
                "hour": hour, "state": state,
                "coverage": round(coverage, 2),
                "consistency": round(coverage, 2),   # back-compat key
                "observed_days": positive_days,
                "opportunity_days": total_days,
                "skipped_days": negative_days,
                "evidence": score.evidence(),
            }

            # v6.41.0: a single sole-occupant person can own this routine
            # outright; otherwise it stays household-wide, unchanged.
            person = self._dominant_person(conn, "state_changes", entity=entity,
                                            state=state, hour=hour)
            days_str = f"on {positive_days} of {total_days} days"

            def _desc(name, who):
                if person:
                    return (f"{name} turns {state} around {time_str} {days_str} "
                            f"when {who} is home")
                if state in ("on", "off"):
                    return f"{name} turns {state} around {time_str} {days_str}"
                return f"{name} changes to '{state}' around {time_str} {days_str}"
            if person:
                details["person"] = person
                # If the owner resolves to a person entity, gate the routine on
                # their presence — a time trigger has no inherent presence, so
                # "only when home" is a real guard (and the user still approves
                # it, so a deliberately away-running routine can be declined).
                ent = (person_map or {}).get(person)
                if ent:
                    details["condition"] = {"condition": "state",
                                            "entity_id": ent, "state": "home"}
            desc = _desc(self._name(entity), self._person_label(person or ""))
            legacy = _desc(entity, person)

            patterns.append(DetectedPattern(
                pattern_type="time_routine",
                description=desc,
                entity_ids=[entity],
                confidence=confidence,
                occurrences=count,
                coverage=round(coverage, 3),
                details=details,
                legacy_description=legacy,
            ))

        return patterns[:20]  # Cap at 20

    def _find_repeated_commands(self, conn: sqlite3.Connection) -> list[DetectedPattern]:
        """Find voice commands that repeat at similar times."""
        patterns: list[DetectedPattern] = []

        try:
            rows = conn.execute("""
                SELECT text, hour, COUNT(*) as cnt
                FROM commands
                WHERE timestamp > datetime('now', '-30 days')
                GROUP BY text, hour
                HAVING cnt >= ?
                ORDER BY cnt DESC
                LIMIT 20
            """, (MIN_OCCURRENCES,)).fetchall()
        except Exception:
            return patterns

        for row in rows:
            text = row["text"]
            hour = row["hour"]
            count = row["cnt"]

            total_same_cmd = conn.execute(
                "SELECT COUNT(*) FROM commands WHERE text = ?", (text,)
            ).fetchone()[0]

            confidence = min(1.0, (count / total_same_cmd) * 0.8 + 0.2)
            details = {"command": text, "hour": hour}

            person = self._dominant_person(conn, "commands", text=text, hour=hour)
            tail = f"says '{text}' around {hour:02d}:00 regularly ({count} times)"
            if person:
                details["person"] = person
                desc = f"{self._person_label(person)} {tail}"
                legacy = f"{person} {tail}"
            else:
                desc = legacy = (f"'{text}' is said around {hour:02d}:00 regularly "
                                 f"({count} times)")

            patterns.append(DetectedPattern(
                pattern_type="repeated_command",
                description=desc,
                entity_ids=[],
                confidence=confidence,
                occurrences=count,
                details=details,
                legacy_description=legacy,
            ))

        return patterns

    def _find_sequence_patterns(self, conn: sqlite3.Connection,
                                lat=None, lon=None,
                                sensor_hist=None,
                                area_presence: AreaPresenceContext = EMPTY_CONTEXT,
                                entity_areas: Optional[dict] = None,
                                ) -> list[DetectedPattern]:
        """Find state changes that consistently follow each other within 10 min.

        Single-pass sliding window. This replaced an O(N^2) SQL self-join whose
        datetime()-wrapped comparison also defeated the timestamp index — on a
        large history (100k+ rows) it never finished, stalling the whole
        analyze() pass so no suggestions were ever stored. This reads rows in
        indexed time order and counts cross-entity pairs inside the window, with
        a hard window cap so an activity burst can't blow up the pairing.

        Pairs are cross-DOMAIN (a switch can trigger a light, a cover a fan): a
        real "when X, do Y" automation rarely stays within one domain. The
        typical lag between trigger and action is measured so the suggested
        automation carries the real delay instead of a fixed guess.
        """
        from collections import deque, Counter
        from ..cognitive import patterns as scoring
        patterns: list = []
        try:
            source_filter = _source_filter(conn)
            rows = conn.execute(
                "SELECT timestamp, entity_id, domain, new_state FROM state_changes "
                "WHERE timestamp > datetime('now', '-30 days') AND "
                + source_filter + " ORDER BY timestamp"
            ).fetchall()
        except Exception:
            return patterns

        window_s = 600.0        # pairs within 10 minutes
        window_cap = 200        # bound pairing work during activity bursts
        win: deque = deque()    # (epoch, entity, domain, state)
        pair_counts: Counter = Counter()
        pair_lag: dict = {}     # (ea,sa,eb,sb) -> [sum_seconds, count] for mean lag
        pair_times: dict = {}   # (ea,sa,eb,sb) -> [action epochs] (capped) for time window
        pair_trigger_times: dict = {}  # pair -> unique trigger epochs (capped)
        off_times: dict[str, list[float]] = {}
        trigger_counts: Counter = Counter()   # (entity, state) -> occurrences

        for r in rows:
            try:
                epoch = datetime.fromisoformat(r["timestamp"]).timestamp()
            except (ValueError, TypeError):
                continue
            ent = r["entity_id"]
            dom = r["domain"]
            st = r["new_state"]
            if st == "off":
                off_times.setdefault(ent, []).append(epoch)
            cutoff = epoch - window_s
            while win and win[0][0] < cutoff:
                win.popleft()
            trigger_counts[(ent, st)] += 1
            counted: set = set()
            # Newest first, so the lag measured is from the closest trigger.
            for a_epoch, a_ent, a_dom, a_st in reversed(win):
                if a_ent != ent:                       # cross-domain allowed
                    key = (a_ent, a_st, ent, st)
                    # One action is one follow-up, however many times the
                    # trigger fired before it: a chattering sensor must not
                    # multiply its apparent support.
                    if key in counted:
                        continue
                    counted.add(key)
                    pair_counts[key] += 1
                    slot = pair_lag.get(key)
                    lag = epoch - a_epoch
                    if slot is None:
                        pair_lag[key] = [lag, 1]
                    else:
                        slot[0] += lag
                        slot[1] += 1
                    tl = pair_times.get(key)
                    if tl is None:
                        pair_times[key] = [epoch]
                    elif len(tl) < 40:
                        tl.append(epoch)
                    trigger_slot = pair_trigger_times.setdefault(key, [])
                    if a_epoch not in trigger_slot and len(trigger_slot) < 40:
                        trigger_slot.append(a_epoch)
            win.append((epoch, ent, dom, st))
            if len(win) > window_cap:
                win.popleft()

        numeric_prepared = _prepare_numeric_history(sensor_hist or {})
        areas = entity_areas or {}
        best: dict = {}   # (action entity, state) -> (confidence, pattern)
        # More candidates than are kept: the rules below reject many, and a
        # chatty but meaningless pair must not crowd out a real one.
        for (ea, sa, eb, sb), count in pair_counts.most_common(SEQUENCE_CANDIDATES):
            if count < MIN_OCCURRENCES:
                break
            # Only an action Nova can actually automate (v7.126.1).
            if service_for(eb, sb) is None:
                continue
            slot = pair_lag.get((ea, sa, eb, sb), [0.0, 1])
            mean_lag = int(round(slot[0] / max(1, slot[1])))
            times = pair_times.get((ea, sa, eb, sb), [])
            trigger_times = pair_trigger_times.get((ea, sa, eb, sb), [])
            # Association, not cause: how often the trigger is followed at
            # all, and on how many different days (counted over the capped
            # sample of action times).
            score = scoring.score_sequence(
                scoring.SequenceEvidence(
                    support=count, trigger_count=trigger_counts.get((ea, sa), count),
                    distinct_days=len({datetime.fromtimestamp(t).date() for t in times})),
                min_occurrences=MIN_OCCURRENCES)
            if not score.accepted:
                continue
            # v7.126.1: an automation acts every time the trigger fires, so the
            # trigger must be followed by the action most of the time, and the
            # two must plausibly be linked: the same area, or almost always
            # together, unless the trigger is someone arriving or leaving.
            conditional = count / max(count, trigger_counts.get((ea, sa), count), 1)
            if not _sequence_link_ok(ea, eb, conditional, areas):
                continue
            # Accumulate every discriminator that consistently holds; HA ANDs a
            # list of conditions. Prefer a sun condition ("after dark") over a
            # fixed time window (it tracks the season), then add a numeric-state
            # condition ("…while it's below/above X") when a sensor consistently
            # sits on one side at the action times.
            conds: list = []
            tw = _sun_condition(times, lat, lon) or _time_window_condition(times)
            if tw:
                conds.append(tw)
            nc = _numeric_condition(times, sensor_hist or {}, numeric_prepared)
            if nc:
                conds.append(nc)
            display_conds = list(conds)
            gate = presence_gate_condition(
                eb, trigger_times, area_presence, exclude=(ea,))
            if gate:
                conds.append(gate["condition"])
            cond = conds if conds else None
            release = None
            if gate and str(sb).lower() == "on":
                release = presence_release(
                    eb, off_times.get(eb, ()), area_presence, gate)
            def _desc(names):
                text = (f"{_trigger_phrase(ea, sa, names)}, {_name_for(eb, names)} "
                        f"turns {sb} shortly after "
                        f"({count} times in 30 days, ~{mean_lag}s later)"
                        + _condition_phrase(display_conds, names))
                if gate:
                    text += f", only when presence is detected in {gate['area_name']}"
                return text
            desc = _desc(self._names)
            legacy = _desc(_AS_IDS)
            pattern = (DetectedPattern(
                pattern_type="sequence",
                description=desc,
                legacy_description=legacy,
                entity_ids=[ea, eb],
                confidence=score.confidence,
                occurrences=count,
                details={"trigger": {"entity": ea, "state": sa},
                         "action": {"entity": eb, "state": sb},
                         "delay_seconds": mean_lag,
                         "condition": cond,
                         **({"presence_gate": {
                             name: gate[name] for name in
                             ("entity_id", "area_id", "area_name")}}
                            if gate else {}),
                         **({"presence_release": release} if release else {}),
                         "conditional": round(conditional, 3),
                         "trigger_count": trigger_counts.get((ea, sa), count),
                         "evidence": score.evidence()},
            ))
            # One suggestion per device action: its strongest trigger.
            best_key = (eb, sb)
            if best_key not in best or score.confidence > best[best_key][0]:
                best[best_key] = (score.confidence, pattern)

        patterns.extend(p for _c, p in best.values())
        return patterns

    def _find_numeric_triggers(self, conn: sqlite3.Connection,
                               sensor_hist: dict,
                               area_presence: AreaPresenceContext = EMPTY_CONTEXT,
                               entity_areas: Optional[dict] = None,
                               ) -> list[DetectedPattern]:
        """Learn "when a sensor crosses a threshold, an action happens" from
        history. ``sensor_hist`` maps sensor_id -> chronological ``[(epoch,
        float)]`` (fetched from the recorder by the caller and passed in, so this
        stays unit-testable without the recorder).

        A sensor only explains an action when (v7.126.0):
          * both are in the same Home Assistant area (``entity_areas``); a
            light level in the landing says nothing about the kitchen light,
            and every room gets dark in the evening;
          * the sensor actually CROSSES the threshold shortly before the
            action (within NUMERIC_TRIGGER_WINDOW), not merely reads low at
            the time;
          * a crossing is followed by the action often enough to automate
            (scored like a sequence, with a higher bar: NUMERIC_MIN_CONDITIONAL).
        Only the strongest sensor is kept for each action, so one light is
        never suggested once per sensor in the house.
        """
        from ..cognitive import patterns as scoring
        patterns: list = []
        if not sensor_hist:
            return patterns
        areas = entity_areas or {}
        try:
            source_filter = _source_filter(conn)
            rows = conn.execute(
                "SELECT entity_id, new_state, timestamp FROM state_changes "
                "WHERE timestamp > datetime('now', '-30 days') AND domain IN ({}) "
                "AND {} ORDER BY timestamp".format(
                    ",".join("'%s'" % d for d in NUMERIC_ACTION_DOMAINS),
                    source_filter)
            ).fetchall()
        except Exception:
            return patterns

        action_times: dict = {}
        for r in rows:
            st = r["new_state"]
            if st in ("unavailable", "unknown"):
                continue
            try:
                ep = datetime.fromisoformat(r["timestamp"]).timestamp()
            except (ValueError, TypeError):
                continue
            action_times.setdefault((r["entity_id"], st), []).append(ep)

        prepared = _prepare_numeric_history(sensor_hist)
        if not prepared:
            return patterns

        best: dict = {}   # (action entity, state) -> (confidence, pattern)
        # Most active actions only — bounds the sensor×action correlation work.
        ranked = sorted(action_times.items(), key=lambda kv: len(kv[1]),
                        reverse=True)[:20]
        for (a_ent, a_st), times in ranked:
            if len(times) < MIN_OCCURRENCES:
                continue
            a_area = areas.get(a_ent)
            if not a_area:
                continue
            for s_ent, (epochs, values) in prepared.items():
                if areas.get(s_ent) != a_area:
                    continue
                occ = []
                for t in times:
                    v = _numeric_value_at(epochs, values, t)
                    if v is not None:
                        occ.append(v)
                if len(occ) < MIN_OCCURRENCES:
                    continue
                trig = _numeric_trigger_from(occ, values)
                if not trig:
                    continue
                op, T = next(iter(trig.items()))
                series = list(zip(epochs, values))
                crossings = _threshold_crossings(series, op, T)
                crossing_times = numeric_trigger_times(
                    series, op, T, times, window_seconds=NUMERIC_TRIGGER_WINDOW)
                support = len(crossing_times)
                days = len({datetime.fromtimestamp(t).date() for t in crossing_times})
                score = scoring.score_sequence(
                    scoring.SequenceEvidence(support=support,
                                             trigger_count=len(crossings),
                                             distinct_days=days),
                    min_occurrences=MIN_OCCURRENCES)
                conditional = support / max(support, len(crossings), 1)
                if not score.accepted or conditional < NUMERIC_MIN_CONDITIONAL:
                    continue
                gate = presence_gate_condition(
                    a_ent, crossing_times, area_presence, exclude=(s_ent,))
                gate_condition = gate["condition"] if gate else None

                def _desc(s_name, a_name, _op=op, _t=T, _gate=gate,
                          _support=support):
                    text = (f"When {s_name} goes {_op} {_t:g}, {a_name} turns "
                            f"{a_st} ({_support} times in 30 days)")
                    if _gate:
                        text += (f", only when presence is detected in "
                                 f"{_gate['area_name']}")
                    return text
                pattern = DetectedPattern(
                    pattern_type="numeric_trigger",
                    description=_desc(self._name(s_ent), self._name(a_ent)),
                    legacy_description=_desc(s_ent, a_ent),
                    entity_ids=[s_ent, a_ent],
                    confidence=score.confidence,
                    occurrences=support,
                    details={"trigger_sensor": s_ent, "op": op, "threshold": T,
                             "action": {"entity": a_ent, "state": a_st},
                             "crossings": len(crossings),
                             "followed": support,
                             "action_count": len(times),
                             "window_seconds": NUMERIC_TRIGGER_WINDOW,
                             "distinct_days": days,
                             "evidence": score.evidence(),
                             **({"condition": [gate_condition],
                                 "presence_gate": {
                                     name: gate[name] for name in
                                     ("entity_id", "area_id", "area_name")}}
                                if gate else {})},
                )
                key = (a_ent, a_st)
                if key not in best or score.confidence > best[key][0]:
                    best[key] = (score.confidence, pattern)
        patterns.extend(p for _c, p in best.values())
        return patterns

    async def _fetch_entity_areas(self, hass, entity_ids) -> dict:
        """entity_id -> area_id (directly or via its device) for the given
        entities, read on the event loop. Never raises."""
        out: dict = {}
        try:
            from ..audio_routing import entity_area
        except Exception:
            return out
        for entity_id in entity_ids:
            try:
                area = entity_area(hass, entity_id)
            except Exception:
                area = None
            if area:
                out[entity_id] = area
        return out

    async def _fetch_area_presence_context(self, hass) -> AreaPresenceContext:
        """Collect bounded area-presence history without crossing HA threads."""
        try:
            from homeassistant.components.recorder import get_instance, history
            from homeassistant.helpers import area_registry as ar
            from homeassistant.util import dt as dt_util
            from ..audio_routing import entity_area
        except Exception:
            return EMPTY_CONTEXT

        candidates: list[tuple[str, str]] = []
        try:
            for state in hass.states.async_all("binary_sensor"):
                device_class = state.attributes.get("device_class")
                if device_class not in ("occupancy", "presence"):
                    continue
                area_id = entity_area(hass, state.entity_id)
                if area_id:
                    candidates.append((state.entity_id, area_id))
        except Exception:
            return EMPTY_CONTEXT
        candidates = sorted(candidates)[:40]
        if not candidates:
            return EMPTY_CONTEXT

        entity_areas: dict[str, str] = dict(candidates)
        action_domains = (
            "light", "switch", "fan", "input_boolean", "humidifier",
            "siren", "lock", "cover", "scene",
        )
        try:
            for domain in action_domains:
                for state in hass.states.async_all(domain):
                    area_id = entity_area(hass, state.entity_id)
                    if area_id:
                        entity_areas[state.entity_id] = area_id
        except Exception:
            return EMPTY_CONTEXT

        area_sensors: dict[str, list[str]] = {}
        for sensor_id, area_id in candidates:
            area_sensors.setdefault(area_id, []).append(sensor_id)
        area_names: dict[str, str] = {}
        try:
            registry = ar.async_get(hass)
            for area_id in area_sensors:
                area = registry.async_get_area(area_id)
                area_names[area_id] = getattr(area, "name", None) or area_id
        except Exception:
            area_names = {area_id: area_id for area_id in area_sensors}

        end = dt_util.utcnow()
        start = end - timedelta(days=30)
        sensor_ids = [sensor_id for sensor_id, _area_id in candidates]

        def _fetch():
            return history.get_significant_states(
                hass, start, end, sensor_ids,
                minimal_response=True, no_attributes=True)

        try:
            raw = await get_instance(hass).async_add_executor_job(_fetch)
        except Exception:
            return EMPTY_CONTEXT
        sensor_history: dict[str, tuple[tuple[float, bool], ...]] = {}
        for sensor_id, states in (raw or {}).items():
            series: list[tuple[float, bool]] = []
            for state in states:
                try:
                    value = getattr(state, "state", None)
                    changed = (getattr(state, "last_changed", None)
                               or getattr(state, "last_updated", None))
                    if value is None and isinstance(state, dict):
                        value = state.get("state")
                        changed = state.get("last_changed") or state.get("last_updated")
                    epoch = recorder_epoch(changed)
                    if epoch is not None and value in ("on", "off"):
                        series.append((epoch, value == "on"))
                except Exception:
                    continue
            if series:
                sensor_history[sensor_id] = tuple(sorted(series))
        return AreaPresenceContext(
            sensor_history=sensor_history,
            entity_areas=entity_areas,
            area_sensors={key: tuple(value) for key, value in area_sensors.items()},
            area_names=area_names,
        )

    async def _fetch_numeric_sensor_history(self, hass) -> dict:
        """Fetch recent recorder history for numeric sensors likely to drive
        automations (temperature / humidity / illuminance). Returns
        {sensor_id: [(epoch, float)]}. Bounded and failure-tolerant (→ {})."""
        out: dict = {}
        try:
            from homeassistant.components.recorder import get_instance, history
            from homeassistant.util import dt as dt_util
        except Exception:
            return out
        wanted = ("temperature", "humidity", "illuminance")
        ids: list = []
        try:
            for st in hass.states.async_all("sensor"):
                if st.attributes.get("device_class") in wanted:
                    ids.append(st.entity_id)
        except Exception:
            return out
        ids = ids[:30]
        if not ids:
            return out
        end = dt_util.utcnow()
        start = end - timedelta(days=30)

        def _fetch():
            return history.get_significant_states(
                hass, start, end, ids, minimal_response=True, no_attributes=True)

        try:
            raw = await get_instance(hass).async_add_executor_job(_fetch)
        except Exception:
            return out
        for eid, states in (raw or {}).items():
            series: list = []
            for s in states:
                try:
                    val: Any = getattr(s, "state", None)
                    when: Any = (getattr(s, "last_changed", None)
                            or getattr(s, "last_updated", None))
                    if val is None and isinstance(s, dict):
                        val = s.get("state")
                        when = s.get("last_changed") or s.get("last_updated")
                    fv = float(val)
                    ep = recorder_epoch(when)
                    if ep is not None:
                        series.append((ep, fv))
                except (TypeError, ValueError):
                    continue
            if len(series) >= 10:
                out[eid] = series
        return out

    def _find_presence_patterns(self, conn: sqlite3.Connection) -> list[DetectedPattern]:
        """Find state changes correlated with person arrivals/departures."""
        patterns: list[DetectedPattern] = []

        # Look for state changes that happen within 5 min of person state changes
        try:
            action_filter = _source_filter(conn, "b")
            rows = conn.execute(f"""
                SELECT
                    a.entity_id as person_entity,
                    a.new_state as person_state,
                    b.entity_id as device_entity,
                    b.new_state as device_state,
                    COUNT(*) as cnt
                FROM state_changes a
                JOIN state_changes b ON
                    b.timestamp > a.timestamp AND
                    b.timestamp <= datetime(a.timestamp, '+5 minutes') AND
                    a.entity_id != b.entity_id
                WHERE a.timestamp > datetime('now', '-30 days')
                    AND a.domain = 'person'
                    AND {action_filter}
                GROUP BY a.entity_id, a.new_state, b.entity_id, b.new_state
                HAVING cnt >= ?
                ORDER BY cnt DESC
                LIMIT 10
            """, (max(3, MIN_OCCURRENCES // 2),)).fetchall()
        except Exception:
            return patterns

        for row in rows:
            person = row["person_entity"]
            p_state = row["person_state"]
            device = row["device_entity"]
            d_state = row["device_state"]
            count = row["cnt"]

            action_word = "arrives" if p_state == "home" else "leaves"
            confidence = min(1.0, count / MIN_OCCURRENCES * 0.7)

            patterns.append(DetectedPattern(
                pattern_type="presence",
                description=(
                    f"When {self._name(person)} {action_word}, {self._name(device)} "
                    f"turns {d_state} ({count} times)"
                ),
                legacy_description=(
                    f"When {person} {action_word}, {device} turns {d_state} "
                    f"({count} times)"
                ),
                entity_ids=[person, device],
                confidence=confidence,
                occurrences=count,
                details={"trigger_person": person, "trigger_state": p_state,
                         "action_entity": device, "action_state": d_state},
            ))

        return patterns

    def _dominant_person(self, conn: sqlite3.Connection, table: str, *,
                         hour: int, entity: str | None = None,
                         state: str | None = None,
                         text: str | None = None) -> Optional[str]:
        """
        If one known person accounts for most of a pattern's occurrences,
        return them; else None, meaning the pattern stays household-wide.
        `table` is "state_changes" (match on entity+state+hour) or
        "commands" (match on text+hour). Defensive: an unmigrated DB
        missing the `person` column just falls back to household (None).
        """
        # v6.77.0: weight each event by how CONFIDENT the attribution was, so a
        # room-scoped "probably Username3 (0.62)" contributes proportionally instead
        # of being thrown away. Commands keep full weight — the conversation path
        # runs the full identity resolver, so those attributions are strong.
        try:
            if table == "state_changes":
                source_filter = _source_filter(conn)
                rows = conn.execute(f"""
                    SELECT person, COUNT(*) as cnt,
                           SUM(COALESCE(NULLIF(person_confidence, 0), 0.5)) as wt
                    FROM state_changes
                    WHERE entity_id = ? AND new_state = ? AND hour = ?
                        AND timestamp > datetime('now', '-30 days')
                        AND {source_filter}
                    GROUP BY person ORDER BY wt DESC
                """, (entity, state, hour)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT person, COUNT(*) as cnt, COUNT(*) * 1.0 as wt
                    FROM commands
                    WHERE text = ? AND hour = ?
                        AND timestamp > datetime('now', '-30 days')
                    GROUP BY person ORDER BY wt DESC
                """, (text, hour)).fetchall()
        except Exception:
            # older DB without the confidence column — fall back to raw counts
            try:
                if table == "state_changes":
                    source_filter = _source_filter(conn)
                    rows = conn.execute(f"""
                        SELECT person, COUNT(*) as cnt, COUNT(*) * 1.0 as wt
                        FROM state_changes
                        WHERE entity_id = ? AND new_state = ? AND hour = ?
                            AND timestamp > datetime('now', '-30 days')
                            AND {source_filter}
                        GROUP BY person ORDER BY wt DESC
                    """, (entity, state, hour)).fetchall()
                else:
                    return None
            except Exception:
                return None

        if not rows:
            return None
        # Ignore the unknown bucket rather than aborting on it: previously a
        # dominant 'unknown' killed the whole pattern, so multi-occupant houses
        # (where sole-occupancy rarely holds) never produced per-person routines.
        named = [r for r in rows if r["person"] and r["person"] != "unknown"]
        if not named:
            return None
        total = sum(float(r["wt"] or 0.0) for r in named)
        top = named[0]
        top_wt = float(top["wt"] or 0.0)
        if total <= 0:
            return None
        if (top_wt / total >= PERSON_DOMINANCE_RATIO
                and top["cnt"] >= MIN_OCCURRENCES):
            return top["person"]
        return None

    def _entity_label(self, entity_id: str) -> str:
        """Readable label from an entity_id (no friendly name available here)."""
        name = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        return name.replace("_", " ").strip()

    def _fact_for(self, pattern: "DetectedPattern", legacy: bool = False):
        """
        Map a detected pattern to an observed knowledge fact, or None if it's not
        the kind of thing worth stating as butler-knowledge. Returns
        (subject, kind, key, value). Deterministic so re-analysis upserts in
        place. The entity is named as a person knows it; `legacy` gives the
        key Nova used before v7.125 (the entity_id without its domain).
        """
        if pattern.pattern_type == "time_routine" and pattern.entity_ids:
            entity = pattern.entity_ids[0]
            if legacy:
                label = self._entity_label(entity)
            else:
                label = _name_for(entity, pattern.details.get("names") or {})
            state = str(pattern.details.get("state", "")).strip()
            hour = pattern.details.get("hour")
            if hour is None or not label:
                return None
            when = f"around {hour:02d}:00 most days"
            subject = self._subject_for_pattern(pattern)
            if state in ("on", "off"):
                return (subject, "fact", f"{label} turns {state}", when)
            return (subject, "fact", f"{label} set to {state}", when)
        if pattern.pattern_type == "repeated_command":
            text = str(pattern.details.get("command", "")).strip()
            hour = pattern.details.get("hour")
            if not text or hour is None:
                return None
            subject = self._subject_for_pattern(pattern)
            return (subject, "fact", f'asks "{text[:60]}"',
                    f"usually around {hour:02d}:00")
        return None

    def _subject_for_pattern(self, pattern: "DetectedPattern") -> str:
        """
        The knowledge subject to attribute a promoted fact to: a specific
        person's subject when the pattern is confidently theirs alone
        (v6.41.0), else "household" — identical to pre-6.41 behavior.
        """
        person = pattern.details.get("person")
        if not person:
            return "household"
        try:
            from .. import identity
            return identity.normalize(person)
        except Exception:
            return "household"

    def _promote_to_knowledge(self, patterns: list) -> int:
        """Write the most reliable routines/commands as observed facts. SYNC."""
        try:
            from .. import knowledge
        except Exception:
            return 0
        written = 0
        for p in patterns:
            if p.confidence < KNOWLEDGE_FACT_CONFIDENCE:
                continue
            mapped = self._fact_for(p)
            if not mapped:
                continue
            subject, kind, key, value = mapped
            try:
                # A fact learned under the old wording moves to the new one
                # instead of being learned twice. Stated facts never move.
                old = self._fact_for(p, legacy=True)
                if old and old[2] != key:
                    knowledge.rename_observed(old[2], key, subject=subject)
                stored = knowledge.remember(
                    key, value, subject=subject, kind=kind, source="observed",
                    confidence=round(float(p.confidence), 3), salience=0.8,
                    respect_stated=True,
                )
                if stored:
                    written += 1
            except Exception as exc:
                _LOGGER.debug("knowledge promote failed for %r: %s", key, exc)
        return written

    def _store_person_pattern(self, pattern: DetectedPattern) -> bool:
        """Upsert a person-owned routine into the person_patterns store (now
        owned by the person_patterns module), keyed on what the routine is
        (cognitive.routines.routine_key) so re-analysis refreshes one row
        however its counts drift. A time routine is only a person's when it is
        something a person does (a light, a lock, the blinds), never a
        tracker, sensor or helper changing state."""
        person = pattern.details.get("person")
        if not person:
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
            person, pattern.pattern_type, pattern.description,
            data=data, confidence=pattern.confidence,
            occurrences=pattern.occurrences, db_path=self._db,
            key=routines.routine_key(pattern.pattern_type, entity, data),
        )

    def get_person_patterns(self, person: Optional[str] = None) -> list[dict]:
        """Read stored per-person routines (person_patterns module), optionally
        filtered to one person (matched on the already-normalized id)."""
        from .. import person_patterns
        return person_patterns.read(person, db_path=self._db)

    # ── Suggestions (stored in patterns.db by SuggestionStore) ──────────────
    def _suggestions(self) -> SuggestionStore:
        return SuggestionStore(self._db)

    def _installable(self, pattern: DetectedPattern) -> bool:
        """Whether the suggestion would be a real automation (a controllable
        target and a complete trigger and action), not advice."""
        try:
            norm = normalize_suggestion_automation(self._generate_automation(pattern))
            return bool(norm.get("installable"))
        except Exception:
            return False

    def _store_rejected(self, pattern: DetectedPattern) -> bool:
        """Store a suggestion the AI review turned down, so it is never
        suggested again (the panel can restore it)."""
        return self._suggestions().store(
            pattern, generate=self._generate_automation,
            status=SUGGESTION_REJECTED)

    def _store_suggestion(self, pattern: DetectedPattern) -> bool:
        """Store a pattern as a suggestion in the DB. Returns True if new."""
        return self._suggestions().store(pattern, generate=self._generate_automation)

    def _generate_automation(self, pattern: DetectedPattern) -> str:
        """Generate HA automation YAML from a detected pattern."""
        return generate_automation(pattern)

    def get_pending_suggestions(self) -> list[dict]:
        """Get all pending suggestions for the user to review."""
        return self._suggestions().pending()

    def get_suggestion(self, suggestion_id: int) -> Optional[dict]:
        """One suggestion row by id, or None."""
        return self._suggestions().get(suggestion_id)

    def mark_installed(self, suggestion_id: int, automation_id: str) -> None:
        """Record that an approved suggestion became a live automation."""
        self._suggestions().mark_installed(suggestion_id, automation_id)

    def mark_covered(self, suggestion_id: int) -> None:
        """Retire a stale suggestion when HA now has an equivalent automation."""
        self._suggestions().mark_covered(suggestion_id)

    def get_rejected_suggestions(self) -> list[dict]:
        """Suggestions the AI review turned down (v7.126.0)."""
        return self._suggestions().rejected()

    def restore_suggestion(self, suggestion_id: int) -> bool:
        """Bring an AI-rejected suggestion back to pending for a person."""
        return self._suggestions().restore(suggestion_id)

    def approve_suggestion(self, suggestion_id: int) -> bool:
        """Mark a suggestion as approved."""
        return self._suggestions().approve(suggestion_id)

    def dismiss_suggestion(self, suggestion_id: int) -> bool:
        """Mark a suggestion as dismissed."""
        return self._suggestions().dismiss(suggestion_id)

    def get_stats(self) -> dict:
        """Return analysis statistics."""
        return self._suggestions().stats()


# ── Singleton ───────────────────────────────────────────────────────────────

_ANALYZER = PatternAnalyzer()


def get_analyzer() -> PatternAnalyzer:
    return _ANALYZER
