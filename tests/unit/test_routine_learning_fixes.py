"""The five routine learning fixes of 8.49.33. Each test fails on 8.49.32.

1. A routine runs at its usual minute, not on the hour.
2. A routine that drifts across the hour is one routine, not two halves.
3. The cap on routines keeps the most reliable, not the busiest.
4. A repeated command is scored against the same 30 days it was counted in.
5. A learned "after A, then B" waits the typical gap, not the average.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

_SCHEMA = """
CREATE TABLE state_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL, entity_id TEXT NOT NULL, domain TEXT NOT NULL,
    old_state TEXT, new_state TEXT NOT NULL, area_id TEXT,
    hour INTEGER, day_of_week INTEGER, triggered_by TEXT DEFAULT 'system',
    person TEXT DEFAULT 'unknown'
);
CREATE TABLE commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, text TEXT NOT NULL,
    handled_by TEXT DEFAULT 'agent', entity_ids TEXT DEFAULT '[]',
    person TEXT DEFAULT 'unknown', hour INTEGER, day_of_week INTEGER
);
"""


@pytest.fixture
def pa(load):
    return load("automation.patterns")


@pytest.fixture
def sugg(load):
    return load("automation.suggestions")


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:")
    db.executescript(_SCHEMA)
    db.row_factory = sqlite3.Row
    yield db
    db.close()


def _at(days_ago: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return (datetime.now() - timedelta(days=days_ago)).replace(
        hour=hour, minute=minute, second=second, microsecond=0)


def _change(conn, entity, state, when: datetime):
    conn.execute(
        "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, new_state, "
        "area_id, hour, day_of_week) VALUES (?,?,?,?,?,?,?,?)",
        (when.isoformat(), entity, entity.split(".")[0], "off", state, "", when.hour,
         when.weekday()))


def _said(conn, text, when: datetime):
    conn.execute("INSERT INTO commands (timestamp, text, hour, day_of_week) VALUES (?,?,?,?)",
                 (when.isoformat(), text, when.hour, when.weekday()))


def test_a_routine_runs_at_its_usual_minute(pa, sugg, conn):
    for day in range(1, 13):
        _change(conn, "light.landing", "off", _at(day, 22, 45))
    [routine] = pa.PatternAnalyzer()._find_time_routines(conn)
    assert (routine.details["hour"], routine.details["minute"]) == (22, 45)
    assert "around 22:45" in routine.description
    auto = json.loads(sugg.generate_automation(routine))
    assert auto["trigger"] == {"platform": "time", "at": "22:45:00"}
    assert auto["alias"].endswith("off at 22:45")


def test_the_usual_minute_is_the_median_rounded_to_five(pa, conn):
    for day, minute in zip(range(1, 10), (38, 40, 41, 42, 44, 45, 47, 50, 52)):
        _change(conn, "light.landing", "off", _at(day, 22, minute))
    [routine] = pa.PatternAnalyzer()._find_time_routines(conn)
    assert (routine.details["hour"], routine.details["minute"]) == (22, 45)


def test_an_old_suggestion_without_a_minute_still_reads_on_the_hour(pa, sugg, load):
    models = load("automation.models")
    old = models.DetectedPattern(
        pattern_type="time_routine", description="x", entity_ids=["light.a"],
        confidence=0.9, occurrences=9, details={"hour": 7, "state": "on"})
    assert json.loads(sugg.generate_automation(old))["trigger"]["at"] == "07:00:00"


def test_a_routine_across_the_hour_is_one_routine(pa, conn):
    for day in range(1, 13):
        _change(conn, "light.porch", "off", _at(day, 22, 55) if day % 2 else _at(day, 23, 5))
    found = pa.PatternAnalyzer()._find_time_routines(conn)
    assert len(found) == 1
    [routine] = found
    assert routine.details["observed_days"] == 12
    assert (routine.details["hour"], routine.details["minute"]) == (23, 0)


def test_a_routine_across_midnight_keeps_its_time(pa, conn):
    for day in range(1, 13):
        _change(conn, "lock.back_door", "locked",
                _at(day, 23, 55) if day % 2 else _at(day, 0, 5))
    [routine] = pa.PatternAnalyzer()._find_time_routines(conn)
    assert routine.details["observed_days"] == 12
    assert (routine.details["hour"], routine.details["minute"]) in ((23, 55), (0, 0), (0, 5))


def test_two_routines_for_one_device_are_both_found(pa, conn):
    for day in range(1, 13):
        _change(conn, "light.hall", "on", _at(day, 7, 0))
        _change(conn, "light.hall", "on", _at(day, 19, 0))
    found = pa.PatternAnalyzer()._find_time_routines(conn)
    assert sorted(p.details["hour"] for p in found) == [7, 19]


def test_the_cap_keeps_the_most_reliable_routines(pa, conn):
    # Twenty busy but patchy routines (three times a day, half the days)
    # and one reliable one (once a day, every day).
    for n in range(20):
        for day in range(2, 14, 2):
            for minute in (0, 10, 20):
                _change(conn, f"switch.busy_{n:02d}", "on", _at(day, 9, minute))
    for day in range(1, 14):
        _change(conn, "light.reliable", "on", _at(day, 18, 0))
    found = pa.PatternAnalyzer()._find_time_routines(conn)
    assert len(found) == 20
    assert found[0].entity_ids == ["light.reliable"]
    assert [p.confidence for p in found] == sorted((p.confidence for p in found), reverse=True)


def test_a_command_is_scored_against_the_same_30_days(pa, conn):
    for day in range(1, 6):
        _said(conn, "good morning", _at(day, 8))
    for day in range(60, 80):           # said a lot, but long ago
        _said(conn, "good morning", _at(day, 8))
    [command] = pa.PatternAnalyzer()._find_repeated_commands(conn)
    assert command.occurrences == 5
    assert command.confidence == pytest.approx(1.0)


def test_a_sequence_waits_the_typical_gap(pa, conn):
    gaps = [30, 30, 32, 28, 30, 31, 29, 540]     # one slow evening
    for day, gap in zip(range(1, 9), gaps):
        start = _at(day, 20, 0)
        _change(conn, "binary_sensor.front_door", "on", start)
        _change(conn, "light.hall", "on", start + timedelta(seconds=gap))
    found = [p for p in pa.PatternAnalyzer()._find_sequence_patterns(conn)
             if p.entity_ids == ["binary_sensor.front_door", "light.hall"]]
    assert [p.details["delay_seconds"] for p in found] == [30]
    assert "~30s later" in found[0].description
