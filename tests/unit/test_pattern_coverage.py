"""Daily routines are judged by coverage, not by a bare count.

Coverage is the share of watched days on which a routine actually happened.
Two lights can each be seen six times, yet the one seen on every day deserves
more trust than the one seen twice a day on half the days. The days a routine
was missed are carried in its details and read back on the review screen.

Each scenario writes a small history into its own SQLite file. Every time is
built from local midnight, so a scenario means the same thing whenever it runs.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time as clock, timedelta

import pytest

# The few history columns the routine detector reads, plus the source column
# so automation-made rows can be told apart.
_COLUMNS = ("timestamp TEXT", "entity_id TEXT", "domain TEXT", "old_state TEXT",
            "new_state TEXT", "area_id TEXT", "hour INTEGER", "day_of_week INTEGER",
            "triggered_by TEXT DEFAULT 'system'", "person TEXT DEFAULT 'unknown'")


@pytest.fixture
def patterns(load, monkeypatch):
    """The analyzer module with its thresholds pinned to the shipped values,
    so a panel setting applied by an earlier test cannot leak in."""
    load("pattern_analyzer")
    mod = load("automation.patterns")
    monkeypatch.setattr(mod, "MIN_OCCURRENCES", 5)
    monkeypatch.setattr(mod, "CONFIDENCE_THRESHOLD", 0.65)
    return mod


@pytest.fixture
def suggestions(load):
    load("pattern_analyzer")
    return load("automation.suggestions")


class _Diary:
    """An in-file history: `seen` adds one change on a past day."""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(f"CREATE TABLE state_changes (id INTEGER PRIMARY KEY, {', '.join(_COLUMNS)})")

    def seen(self, entity_id, days_back, at, *, state="on", source="system"):
        hour, minute = at
        when = datetime.combine(date.today() - timedelta(days=days_back), clock(hour, minute))
        self.conn.execute(
            "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, new_state, "
            "area_id, hour, day_of_week, triggered_by) VALUES (?, ?, ?, 'off', ?, '', ?, ?, ?)",
            (when.isoformat(), entity_id, entity_id.partition(".")[0], state,
             when.hour, when.weekday(), source))

    def routines(self, patterns) -> dict:
        self.conn.commit()
        found = patterns.PatternAnalyzer()._find_time_routines(self.conn)
        return {p.entity_ids[0]: p for p in found}


@pytest.fixture
def diary(tmp_path):
    made = _Diary(tmp_path / "history.db")
    yield made
    made.conn.close()


def _every_day_and_half_the_days(diary):
    """light.desk on all six days at 18:00; light.shelf twice an evening on
    only three of them. Both are seen six times."""
    for back in range(1, 7):
        diary.seen("light.desk", back, (18, 0))
    for back in (1, 2, 3):
        diary.seen("light.shelf", back, (18, 0))
        diary.seen("light.shelf", back, (18, 30))


def test_the_watched_days_are_the_denominator_and_missed_days_are_counted(diary, patterns):
    _every_day_and_half_the_days(diary)
    found = diary.routines(patterns)
    assert set(found) == {"light.desk", "light.shelf"}

    expected = {
        # entity: (observed, opportunity, skipped, coverage)
        "light.desk": (6, 6, 0, 1.0),
        "light.shelf": (3, 6, 3, 0.5),
    }
    for entity_id, (observed, opportunity, skipped, share) in expected.items():
        details = found[entity_id].details
        assert (details["observed_days"], details["opportunity_days"],
                details["skipped_days"]) == (observed, opportunity, skipped), entity_id
        assert found[entity_id].coverage == share
        # The older field name carries the same figure for older readers.
        assert details["coverage"] == details["consistency"] == share
        assert (details["hour"], details["minute"], details["state"]) == (18, 0, "on")

    assert found["light.desk"].description.endswith("on 6 of 6 days")
    assert found["light.shelf"].description.endswith("on 3 of 6 days")


def test_equal_counts_but_more_days_gives_more_confidence(diary, patterns):
    _every_day_and_half_the_days(diary)
    found = diary.routines(patterns)
    desk, shelf = found["light.desk"], found["light.shelf"]
    assert desk.occurrences == shelf.occurrences == 6
    # Coverage times the small-sample discount: 1.0 x 1.0 against 0.5 x 3/5.
    assert desk.confidence == 1.0
    assert shelf.confidence == 0.3
    assert desk.confidence > shelf.confidence


def test_three_full_days_are_discounted_as_a_small_sample(diary, patterns):
    # Twice a morning on three days: six changes clear the count gate and
    # coverage is complete, but three days are fewer than MIN_OCCURRENCES.
    for back in (1, 2, 3):
        diary.seen("light.landing", back, (7, 0))
        diary.seen("light.landing", back, (7, 30))
    found = diary.routines(patterns)
    assert list(found) == ["light.landing"]
    routine = found["light.landing"]
    assert routine.coverage == 1.0
    assert routine.confidence < 1.0
    assert routine.confidence == pytest.approx(round(3 / patterns.MIN_OCCURRENCES, 3), abs=1e-6)
    assert routine.details["evidence"]["sample"] == 0.6
    assert routine.details["evidence"]["distinct_days"] == 3


def test_days_only_an_automation_was_active_do_not_widen_the_window(diary, patterns):
    for back in range(1, 7):
        diary.seen("light.desk", back, (18, 0))
    # An automation switching a light on four other days is not watching time.
    for back in range(10, 14):
        diary.seen("light.porch", back, (21, 0), source="automation")
    desk = diary.routines(patterns)["light.desk"]
    assert desk.details["opportunity_days"] == 6
    assert desk.details["skipped_days"] == 0


def test_a_routine_on_too_few_days_is_not_reported(diary, patterns):
    # Five changes on two days: enough repeats, too few days (MIN_DISTINCT_DAYS is 3).
    for minute in (0, 10, 20):
        diary.seen("light.hall", 1, (19, minute))
    for minute in (0, 10):
        diary.seen("light.hall", 2, (19, minute))
    assert diary.routines(patterns) == {}


# ── how the review screen explains it ───────────────────────────────────────

@pytest.mark.parametrize("details,count,wanted,unwanted", [
    pytest.param(
        {"hour": 7, "state": "on", "coverage": 0.7, "observed_days": 42, "opportunity_days": 60},
        50, ["Happened on 42 of 60 days (missed 18)", "Consistent on about 70% of days",
             "Observed turning on near 07:00"], ["50 times"], id="with-coverage"),
    pytest.param(
        {"hour": 6, "minute": 45, "state": "on", "coverage": 1.0, "observed_days": 9,
         "opportunity_days": 9},
        9, ["Happened on 9 of 9 days", "Observed turning on near 06:45"], ["missed"],
        id="never-missed"),
    pytest.param(
        # A suggestion stored before coverage was recorded.
        {"hour": 18, "state": "off", "consistency": 0.8},
        12, ["Happened 12 times in the last 30 days", "Consistent on about 80% of days"],
        ["missed", "Happened on"], id="older-row"),
])
def test_the_evidence_lines_for_a_daily_routine(suggestions, details, count, wanted, unwanted):
    out = suggestions.explain_suggestion("time_routine", details, count=count)
    assert out["headline"].startswith("A daily routine around ")
    lines = out["evidence"]
    for text in wanted:
        assert text in lines, (text, lines)
    joined = " | ".join(lines)
    for text in unwanted:
        assert text not in joined, (text, lines)


def test_the_headline_names_the_usual_time(suggestions):
    out = suggestions.explain_suggestion(
        "time_routine", {"hour": 22, "minute": 5, "state": "off"}, count=6)
    assert out["headline"] == "A daily routine around 22:05"
