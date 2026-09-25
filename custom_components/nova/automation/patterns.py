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

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .models import DetectedPattern
from .suggestions import (  # DB_PATH / MIN_DAYS are shared with stats
    DB_PATH,
    MIN_DAYS,
    SuggestionStore,
    _trigger_phrase,
    generate_automation,
    normalize_suggestion_automation,
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
            uw = r.get("unwelcome_rate") or 0.0
            if uw >= 0.5:
                delta = 0.15        # mostly unwelcome → much more selective
            elif uw >= 0.3:
                delta = 0.07        # somewhat unwelcome → more selective
            elif uw <= 0.1:
                delta = -0.07       # almost all welcome → a little more generous
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


def _condition_phrase(cond) -> str:
    """Human tail for a pattern description given its learned condition(s).
    Accepts a single condition dict, a list of them (ANDed), or None."""
    if isinstance(cond, list):
        return "".join(_condition_phrase(c) for c in cond)
    if not isinstance(cond, dict):
        return ""
    kind = cond.get("condition")
    if kind == "sun":
        return ", mostly after dark"
    if kind == "time":
        return f", mostly between {cond.get('after', '')[:5]} and {cond.get('before', '')[:5]}"
    if kind == "numeric_state":
        ent = cond.get("entity_id", "")
        if "below" in cond:
            return f", mostly while {ent} is below {cond['below']:g}"
        if "above" in cond:
            return f", mostly while {ent} is above {cond['above']:g}"
    if kind == "state":
        ent = cond.get("entity_id", "")
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


def _numeric_condition(times: list, sensor_hist: dict) -> Optional[dict]:
    """Best numeric_state *condition* for an action whose occurrences (``times``)
    consistently coincide with a sensor sitting on one side of a threshold — e.g.
    "…and only while the temperature is below 62". Returns a self-describing HA
    condition dict, or None. Reuses the same scorer as the numeric trigger, so it
    shares the anti-spurious guards. ``sensor_hist`` maps sensor_id -> ``[(epoch,
    float)]``."""
    if not sensor_hist or len(times) < MIN_OCCURRENCES:
        return None
    best = None
    best_cover = 0
    for s_ent, events in sensor_hist.items():
        ev = sorted((e, v) for e, v in events if isinstance(v, (int, float)))
        if len(ev) < 10:
            continue
        epochs = [e for e, _ in ev]
        values = [v for _, v in ev]
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
        self._db = DB_PATH

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
        out = {"top_sources": [], "candidates": [], "total_days": 0,
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

    async def analyze(self, hass: HomeAssistant) -> list[DetectedPattern]:
        """Run full pattern analysis. Returns detected patterns."""
        self._last_analysis = time.time()
        patterns = []

        conn = self._connect()
        if not conn:
            return patterns

        try:
            person_map = self._person_entity_map(hass)
            patterns.extend(await hass.async_add_executor_job(
                self._find_time_routines, conn, person_map))
            patterns.extend(await hass.async_add_executor_job(
                self._find_repeated_commands, conn))
            try:
                _sensor_hist = await self._fetch_numeric_sensor_history(hass)
            except Exception:
                _sensor_hist = {}
            _lat = getattr(hass.config, "latitude", None)
            _lon = getattr(hass.config, "longitude", None)
            patterns.extend(await hass.async_add_executor_job(
                self._find_sequence_patterns, conn, _lat, _lon, _sensor_hist))
            patterns.extend(await hass.async_add_executor_job(
                self._find_numeric_triggers, conn, _sensor_hist))
            patterns.extend(await hass.async_add_executor_job(
                self._find_presence_patterns, conn))
        except Exception as exc:
            _LOGGER.warning("Pattern analysis error: %s", exc)
        finally:
            conn.close()

        # Store high-confidence patterns as suggestions
        new_suggestions = 0
        already_automated = 0
        new_person_patterns = 0
        _eff_threshold = _effective_threshold()
        near_misses: list = []
        # A sequence stores when count/(MIN_OCCURRENCES*3) >= threshold; surface
        # how many recurrences a not-yet-stored one still needs.
        _seq_needed = int(_eff_threshold * MIN_OCCURRENCES * 3)
        if _eff_threshold * MIN_OCCURRENCES * 3 > _seq_needed:
            _seq_needed += 1
        for p in patterns:
            if p.confidence >= _eff_threshold:
                match = self._automation_match(hass, p)
                p.details["automation_match"] = match
                if match.get("status") == "already_automated":
                    already_automated += 1
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
        self._last_result = {
            "ts": time.time(),
            "patterns_found": len(patterns),
            "new_suggestions": new_suggestions,
            "already_automated": already_automated,
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
            }
            from .inventory import get_inventory
            inventory = get_inventory(hass)
            if inventory is None:
                return {"status": "inventory_unavailable", "matches": [],
                        "reason": "automation inventory unavailable"}
            from .matching import classify
            return classify(candidate, inventory.records())
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
            def normalize(n):
                return "_".join((n or "").strip().lower().split())
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

    def _find_time_routines(self, conn: sqlite3.Connection,
                            person_map: dict = None) -> list[DetectedPattern]:
        """Find entities that change state at similar times each day."""
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

        for row in rows:
            entity = row["entity_id"]
            state = row["new_state"]
            hour = row["hour"]
            count = row["cnt"]

            # Positive days: distinct days this routine ACTUALLY happened. Using
            # distinct days (not raw event count) so several same-hour events on
            # one day count once — the honest "on N of M days" numerator.
            positive_days = conn.execute(f"""
                SELECT COUNT(DISTINCT date(timestamp)) FROM state_changes
                WHERE entity_id = ? AND new_state = ? AND hour = ?
                  AND timestamp > datetime('now', '-30 days')
                  AND {source_filter}
            """, (entity, state, hour)).fetchone()[0] or 0

            # Coverage weighs the negative evidence: a routine on 42 of 45 days
            # (0.93) is far stronger than one on 42 of 120 days (0.35), even
            # though both were "seen 42 times".
            coverage = positive_days / total_days if total_days else 0.0
            negative_days = max(0, total_days - positive_days)
            if coverage < 0.3:
                continue

            # Confidence = coverage, discounted for a small sample so a 3-of-3
            # (1.0) can't outrank a 40-of-45 (0.89) on three data points.
            sample_factor = min(1.0, positive_days / MIN_OCCURRENCES)
            confidence = round(coverage * sample_factor, 3)

            time_str = f"{hour:02d}:00"
            details = {
                "hour": hour, "state": state,
                "coverage": round(coverage, 2),
                "consistency": round(coverage, 2),   # back-compat key
                "observed_days": positive_days,
                "opportunity_days": total_days,
                "skipped_days": negative_days,
            }

            # v6.41.0: a single sole-occupant person can own this routine
            # outright; otherwise it stays household-wide, unchanged.
            person = self._dominant_person(conn, "state_changes", entity=entity,
                                            state=state, hour=hour)
            days_str = f"on {positive_days} of {total_days} days"
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
                desc = (f"{entity} turns {state} around {time_str} {days_str} "
                        f"when {person} is home")
            elif state in ("on", "off"):
                desc = f"{entity} turns {state} around {time_str} {days_str}"
            else:
                desc = f"{entity} changes to '{state}' around {time_str} {days_str}"

            patterns.append(DetectedPattern(
                pattern_type="time_routine",
                description=desc,
                entity_ids=[entity],
                confidence=confidence,
                occurrences=count,
                coverage=round(coverage, 3),
                details=details,
            ))

        return patterns[:20]  # Cap at 20

    def _find_repeated_commands(self, conn: sqlite3.Connection) -> list[DetectedPattern]:
        """Find voice commands that repeat at similar times."""
        patterns = []

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
            if person:
                details["person"] = person
                desc = (f"{person} says '{text}' around {hour:02d}:00 regularly "
                        f"({count} times)")
            else:
                desc = f"'{text}' is said around {hour:02d}:00 regularly ({count} times)"

            patterns.append(DetectedPattern(
                pattern_type="repeated_command",
                description=desc,
                entity_ids=[],
                confidence=confidence,
                occurrences=count,
                details=details,
            ))

        return patterns

    def _find_sequence_patterns(self, conn: sqlite3.Connection,
                                lat=None, lon=None,
                                sensor_hist=None) -> list[DetectedPattern]:
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

        for r in rows:
            try:
                epoch = datetime.fromisoformat(r["timestamp"]).timestamp()
            except (ValueError, TypeError):
                continue
            ent = r["entity_id"]
            dom = r["domain"]
            st = r["new_state"]
            cutoff = epoch - window_s
            while win and win[0][0] < cutoff:
                win.popleft()
            for a_epoch, a_ent, a_dom, a_st in win:
                if a_ent != ent:                       # cross-domain allowed
                    key = (a_ent, a_st, ent, st)
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
            win.append((epoch, ent, dom, st))
            if len(win) > window_cap:
                win.popleft()

        for (ea, sa, eb, sb), count in pair_counts.most_common(15):
            if count < MIN_OCCURRENCES:
                break
            slot = pair_lag.get((ea, sa, eb, sb), [0.0, 1])
            mean_lag = int(round(slot[0] / max(1, slot[1])))
            times = pair_times.get((ea, sa, eb, sb), [])
            # Accumulate every discriminator that consistently holds; HA ANDs a
            # list of conditions. Prefer a sun condition ("after dark") over a
            # fixed time window (it tracks the season), then add a numeric-state
            # condition ("…while it's below/above X") when a sensor consistently
            # sits on one side at the action times.
            conds: list = []
            tw = _sun_condition(times, lat, lon) or _time_window_condition(times)
            if tw:
                conds.append(tw)
            nc = _numeric_condition(times, sensor_hist or {})
            if nc:
                conds.append(nc)
            cond = conds if conds else None
            desc = (f"{_trigger_phrase(ea, sa)}, {eb} turns {sb} shortly after "
                    f"({count} times in 30 days, ~{mean_lag}s later)"
                    + _condition_phrase(cond))
            patterns.append(DetectedPattern(
                pattern_type="sequence",
                description=desc,
                entity_ids=[ea, eb],
                confidence=min(1.0, count / (MIN_OCCURRENCES * 3)),
                occurrences=count,
                details={"trigger": {"entity": ea, "state": sa},
                         "action": {"entity": eb, "state": sb},
                         "delay_seconds": mean_lag,
                         "condition": cond},
            ))

        return patterns

    def _find_numeric_triggers(self, conn: sqlite3.Connection,
                               sensor_hist: dict) -> list[DetectedPattern]:
        """Learn "when a sensor crosses a threshold, an action happens" from
        history. ``sensor_hist`` maps sensor_id -> chronological ``[(epoch,
        float)]`` (fetched from the recorder by the caller and passed in, so this
        stays unit-testable without the recorder). Bounded: the most active
        actions only, few numeric sensors, strong consistency in the scorer.
        """
        patterns: list = []
        if not sensor_hist:
            return patterns
        _ACT = ("light", "switch", "cover", "lock", "climate", "fan",
                "media_player", "humidifier", "water_heater", "valve")
        try:
            source_filter = _source_filter(conn)
            rows = conn.execute(
                "SELECT entity_id, new_state, timestamp FROM state_changes "
                "WHERE timestamp > datetime('now', '-30 days') AND domain IN ({}) "
                "AND {} ORDER BY timestamp".format(
                    ",".join("'%s'" % d for d in _ACT), source_filter)
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

        prepared: dict = {}
        for s_ent, events in sensor_hist.items():
            ev = [(e, v) for e, v in events if isinstance(v, (int, float))]
            if len(ev) >= 10:
                ev.sort()
                prepared[s_ent] = ([e for e, _ in ev], [v for _, v in ev])
        if not prepared:
            return patterns

        # Most active actions only — bounds the sensor×action correlation work.
        ranked = sorted(action_times.items(), key=lambda kv: len(kv[1]),
                        reverse=True)[:20]
        for (a_ent, a_st), times in ranked:
            if len(times) < MIN_OCCURRENCES:
                continue
            for s_ent, (epochs, values) in prepared.items():
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
                patterns.append(DetectedPattern(
                    pattern_type="numeric_trigger",
                    description=(f"When {s_ent} goes {op} {T:g}, {a_ent} turns "
                                 f"{a_st} ({len(occ)} times in 30 days)"),
                    entity_ids=[s_ent, a_ent],
                    confidence=min(1.0, len(occ) / (MIN_OCCURRENCES * 3)),
                    occurrences=len(occ),
                    details={"trigger_sensor": s_ent, "op": op, "threshold": T,
                             "action": {"entity": a_ent, "state": a_st}},
                ))
        return patterns

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
                    val = getattr(s, "state", None)
                    when = (getattr(s, "last_changed", None)
                            or getattr(s, "last_updated", None))
                    if val is None and isinstance(s, dict):
                        val = s.get("state")
                        when = s.get("last_changed") or s.get("last_updated")
                    fv = float(val)
                    ep = when.timestamp() if hasattr(when, "timestamp") else None
                    if ep is not None:
                        series.append((ep, fv))
                except (TypeError, ValueError):
                    continue
            if len(series) >= 10:
                out[eid] = series
        return out

    def _find_presence_patterns(self, conn: sqlite3.Connection) -> list[DetectedPattern]:
        """Find state changes correlated with person arrivals/departures."""
        patterns = []

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

    def _fact_for(self, pattern: "DetectedPattern"):
        """
        Map a detected pattern to an observed knowledge fact, or None if it's not
        the kind of thing worth stating as butler-knowledge. Returns
        (subject, kind, key, value). Deterministic so re-analysis upserts in place.
        """
        if pattern.pattern_type == "time_routine" and pattern.entity_ids:
            label = self._entity_label(pattern.entity_ids[0])
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
        owned by the person_patterns module). Deterministic key
        (person, pattern_type, description) so re-analysis refreshes in place."""
        person = pattern.details.get("person")
        if not person:
            return False
        from .. import person_patterns
        return person_patterns.store(
            person, pattern.pattern_type, pattern.description,
            data=pattern.details, confidence=pattern.confidence,
            occurrences=pattern.occurrences, db_path=self._db,
        )

    def get_person_patterns(self, person: Optional[str] = None) -> list[dict]:
        """Read stored per-person routines (person_patterns module), optionally
        filtered to one person (matched on the already-normalized id)."""
        from .. import person_patterns
        return person_patterns.read(person, db_path=self._db)

    # ── Suggestions (stored in patterns.db by SuggestionStore) ──────────────
    def _suggestions(self) -> SuggestionStore:
        return SuggestionStore(self._db)

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
