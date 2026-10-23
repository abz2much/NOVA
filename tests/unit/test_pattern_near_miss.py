"""Near misses: patterns found but still under the bar for a suggestion.

The panel lists them as "building up", so a real habit that has not repeated
often enough yet reads differently from "nothing found". The scenario here is
a bay occupancy sensor followed a minute later by the garage door closing.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time as clock, timedelta

import pytest

_TABLES = {
    "state_changes": ("timestamp TEXT NOT NULL", "entity_id TEXT NOT NULL",
                      "domain TEXT NOT NULL", "old_state TEXT", "new_state TEXT NOT NULL",
                      "area_id TEXT", "hour INTEGER", "day_of_week INTEGER",
                      "triggered_by TEXT DEFAULT 'system'", "person TEXT DEFAULT 'unknown'"),
    "commands": ("timestamp TEXT", "command TEXT", "entity_id TEXT", "hour INTEGER",
                 "day_of_week INTEGER", "person TEXT DEFAULT 'unknown'"),
    "person_patterns": ("person TEXT", "pattern_type TEXT", "description TEXT",
                        "entity_ids TEXT", "confidence REAL", "details TEXT",
                        "created TEXT", "last_seen TEXT"),
}

_SENSOR, _DOOR = "binary_sensor.bay_occupancy", "cover.garage"


@pytest.fixture
def patterns(load, monkeypatch):
    load("pattern_analyzer")
    mod = load("automation.patterns")
    # The shipped thresholds, whatever an earlier test applied.
    monkeypatch.setattr(mod, "MIN_OCCURRENCES", 5)
    monkeypatch.setattr(mod, "CONFIDENCE_THRESHOLD", 0.65)
    monkeypatch.setattr(mod, "_learned_threshold_delta", lambda: 0.0)
    return mod


def _garage_evenings(path, repeats: int) -> str:
    """`repeats` evenings, ending two days ago, on which the bay sensor sees
    the car at 18:00 and the garage door closes a minute later."""
    with sqlite3.connect(str(path)) as conn:
        for table, columns in _TABLES.items():
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {', '.join(columns)})")
        for back in range(repeats + 1, 1, -1):
            evening = datetime.combine(date.today() - timedelta(days=back), clock(18, 0))
            for entity_id, state, delay in ((_SENSOR, "on", 0), (_DOOR, "closed", 60)):
                at = evening + timedelta(seconds=delay)
                conn.execute(
                    "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
                    "new_state, area_id, hour, day_of_week) VALUES (?, ?, ?, 'off', ?, '', ?, ?)",
                    (at.isoformat(), entity_id, entity_id.split(".")[0], state,
                     at.hour, at.weekday()))
    return str(path)


def _offline_analyzer(patterns, db, monkeypatch, *, accepted: bool):
    """A PatternAnalyzer on `db` whose stores keep everything in memory:
    filed suggestions go to the returned list."""
    analyzer = patterns.PatternAnalyzer()
    analyzer._db = db
    filed = []

    def file_one(pattern):
        filed.append(pattern)
        return accepted
    monkeypatch.setattr(analyzer, "_store_suggestion", file_one)
    monkeypatch.setattr(analyzer, "_store_person_pattern", lambda pattern: False)
    monkeypatch.setattr(analyzer, "_promote_to_knowledge", lambda found: 0)
    return analyzer, filed


def _about_the_bay(entries):
    return [e for e in entries if "bay occupancy" in (e.get("description") or "")]


async def test_six_evenings_is_reported_as_building_not_as_nothing(
        patterns, tmp_path, monkeypatch, fake_hass):
    # Six repeats score 6 / (5 x 3) = 0.4, under the 0.65 bar.
    db = _garage_evenings(tmp_path / "six.db", 6)
    analyzer, filed = _offline_analyzer(patterns, db, monkeypatch, accepted=False)
    await analyzer.analyze(fake_hass)

    result = analyzer._last_result
    misses = _about_the_bay(result["near_misses"])
    assert misses, f"no near miss for the six evenings: {result['near_misses']}"
    first = misses[0]
    assert first["type"] == "sequence"
    assert first["occurrences"] == 6
    assert first["needed"] == 10            # ceil(0.65 x 5 x 3)
    # Under the bar means never filed as a suggestion.
    assert not [p for p in filed if p.pattern_type == "sequence"]


async def test_twelve_evenings_is_stored_and_not_listed_as_building(
        patterns, tmp_path, monkeypatch, fake_hass):
    # Twelve repeats score 12 / 15 = 0.8, over the bar.
    db = _garage_evenings(tmp_path / "twelve.db", 12)
    analyzer, filed = _offline_analyzer(patterns, db, monkeypatch, accepted=True)
    await analyzer.analyze(fake_hass)

    assert analyzer._last_result["new_suggestions"] >= 1
    chains = [p for p in filed if p.pattern_type == "sequence"]
    assert chains and chains[0].occurrences == 12
    assert chains[0].entity_ids[0] == _SENSOR
    # Checked against the readable name the descriptions really use.
    assert _about_the_bay(analyzer._last_result["near_misses"]) == []


@pytest.mark.parametrize("kind,bar,needed", [
    ("sequence", 0.65, 10),     # 9.75 rounds up
    ("sequence", 0.8, 12),      # an exact 12.0 is not rounded up again
    ("sequence", 0.3, 5),       # 4.5 rounds up
    ("time_routine", 0.65, None),
    ("repeated_command", 0.65, None),
])
def test_how_many_repeats_a_near_miss_still_needs(patterns, kind, bar, needed):
    pattern = patterns.DetectedPattern(
        pattern_type=kind, description="something", entity_ids=["light.hall"],
        confidence=0.2, occurrences=6)
    assert patterns.PatternAnalyzer._near_miss(pattern, bar) == {
        "type": kind, "description": "something", "occurrences": 6, "needed": needed}


async def test_near_misses_need_enough_repeats_and_are_capped_at_eight(
        patterns, tmp_path, monkeypatch, fake_hass):
    """Only patterns with at least MIN_OCCURRENCES count, in the order the
    detectors found them, and no more than eight are listed."""
    made = [patterns.DetectedPattern(
        pattern_type="time_routine", description=f"weak {n}", entity_ids=[f"light.l{n}"],
        confidence=0.5, occurrences=5 + n) for n in range(10)]
    rare = patterns.DetectedPattern(
        pattern_type="time_routine", description="too rare", entity_ids=["light.rare"],
        confidence=0.5, occurrences=4)
    analyzer, filed = _offline_analyzer(patterns, str(tmp_path / "unused.db"), monkeypatch,
                                        accepted=True)
    monkeypatch.setattr(analyzer, "_detect_patterns", lambda *context: [rare, *made])
    await analyzer.analyze(fake_hass)

    result = analyzer._last_result
    assert [m["description"] for m in result["near_misses"]] == [f"weak {n}" for n in range(8)]
    assert result["patterns_found"] == 11
    assert result["new_suggestions"] == 0 and filed == []
