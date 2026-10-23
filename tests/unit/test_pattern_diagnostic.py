"""PatternAnalyzer.pattern_diagnostic(): why no routine formed.

It lists the busiest sources (a flooding sensor stands out) and the strongest
hourly candidates with the days each was seen, so "0 found" comes with a
reason. It only reads the store, and must stay safe on an empty or broken one.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time as clock, timedelta

import pytest

_HISTORY = ("timestamp TEXT NOT NULL", "entity_id TEXT NOT NULL", "domain TEXT NOT NULL",
            "old_state TEXT", "new_state TEXT NOT NULL", "area_id TEXT", "hour INTEGER",
            "day_of_week INTEGER", "triggered_by TEXT DEFAULT 'system'",
            "person TEXT DEFAULT 'unknown'")


@pytest.fixture
def patterns(load, monkeypatch):
    load("pattern_analyzer")
    mod = load("automation.patterns")
    monkeypatch.setattr(mod, "MIN_OCCURRENCES", 5)
    return mod


@pytest.fixture
def store(tmp_path):
    """(path, add) for a fresh history file. add(entity, state, days_back,
    hour, source='system') writes one change at that local hour."""
    path = tmp_path / "history.db"
    conn = sqlite3.connect(str(path))
    conn.execute(f"CREATE TABLE state_changes (id INTEGER PRIMARY KEY, {', '.join(_HISTORY)})")

    def add(entity_id, state, days_back, hour, source="system"):
        when = datetime.combine(date.today() - timedelta(days=days_back), clock(hour, 0))
        conn.execute(
            "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, new_state, "
            "area_id, hour, day_of_week, triggered_by) VALUES (?, ?, ?, 'off', ?, '', ?, ?, ?)",
            (when.isoformat(), entity_id, entity_id.split(".", 1)[0], state, hour,
             when.weekday(), source))
        conn.commit()

    yield path, add
    conn.close()


def _diagnose(patterns, path) -> dict:
    analyzer = patterns.PatternAnalyzer()
    analyzer._db = str(path)
    return analyzer.pattern_diagnostic()


def _flood_and_a_porch_light(add):
    """A television flipping 60 times over twelve days at scattered hours,
    and the porch light on at 18:00 on five of those days."""
    for n in range(60):
        add("media_player.tv", "playing" if n % 2 else "paused", 1 + n % 12, n % 24)
    for back in range(1, 6):
        add("light.porch", "on", back, 18)


def test_the_flooding_source_and_the_near_routine_are_both_shown(patterns, store):
    path, add = store
    _flood_and_a_porch_light(add)
    diag = _diagnose(patterns, path)

    assert diag["top_sources"] == [
        {"entity_id": "media_player.tv", "changes": 60},
        {"entity_id": "light.porch", "changes": 5},
    ]
    # Five days beat the television's scattered single-day hours.
    assert diag["candidates"][0] == {
        "entity_id": "light.porch", "state": "on", "hour": 18,
        "occurrences": 5, "days": 5, "coverage": round(5 / 12, 2)}
    assert diag["total_days"] == 12
    # ceil(0.3 x 12): the days a routine needs to reach the coverage bar.
    assert diag["min_days"] == 4
    assert diag["min_occurrences"] == 5


def test_rows_an_automation_wrote_or_older_than_a_month_are_left_out(patterns, store):
    path, add = store
    for back in range(1, 4):
        add("light.porch", "on", back, 18)
        add("light.garden", "on", back, 21, source="automation")
        add("light.attic", "on", 40 + back, 9)
    diag = _diagnose(patterns, path)
    assert [s["entity_id"] for s in diag["top_sources"]] == ["light.porch"]
    assert [c["entity_id"] for c in diag["candidates"]] == ["light.porch"]
    assert diag["total_days"] == 3


def test_a_single_change_is_not_a_candidate(patterns, store):
    path, add = store
    add("light.study", "on", 2, 8)
    diag = _diagnose(patterns, path)
    assert diag["top_sources"] == [{"entity_id": "light.study", "changes": 1}]
    assert diag["candidates"] == []
    assert (diag["total_days"], diag["min_days"]) == (1, 1)


def test_an_empty_store_gives_empty_lists(patterns, store):
    path, _add = store
    diag = _diagnose(patterns, path)
    assert diag["top_sources"] == [] and diag["candidates"] == []
    assert (diag["total_days"], diag["min_days"]) == (0, 0)


@pytest.mark.parametrize("prepare", ["missing", "no_table"])
def test_a_missing_or_broken_store_gives_the_defaults(patterns, tmp_path, prepare):
    path = tmp_path / "absent.db"
    if prepare == "no_table":
        sqlite3.connect(str(path)).close()
    assert _diagnose(patterns, path) == {
        "top_sources": [], "candidates": [], "total_days": 0, "min_days": 0,
        "min_occurrences": 5}
