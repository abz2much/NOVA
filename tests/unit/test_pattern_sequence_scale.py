"""Sequence learning ("after A, B follows"): what it finds, the ten minute
window, the delay it measures and emits, the conditions it attaches, and
that it stays fast on a large history.

The detector makes a single pass with a sliding window. An earlier SQL self
join never finished on a big store, which stalled the whole analysis so no
suggestion was ever stored; the timing checks below guard against that.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, time as clock, timedelta

import pytest

_COLUMNS = ("timestamp", "entity_id", "domain", "old_state", "new_state",
            "area_id", "hour", "day_of_week", "person")


@pytest.fixture
def mod(load):
    return load("pattern_analyzer")


def _start(days_back: int = 12) -> datetime:
    """Local midnight ``days_back`` days ago, so hours below are exact."""
    return datetime.combine(date.today(), clock()) - timedelta(days=days_back)


class _Store:
    """An in-memory history with the logger's columns and a time index."""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        cols = ", ".join(f"{c} {'INTEGER' if c in ('hour', 'day_of_week') else 'TEXT'}"
                         for c in _COLUMNS)
        self.db.executescript(
            f"CREATE TABLE state_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, {cols},"
            " triggered_by TEXT DEFAULT 'system');"
            "CREATE INDEX sc_by_time ON state_changes(timestamp);")
        self.rows = 0

    def add(self, *events):
        """Each event is (entity_id, new_state, when)."""
        self.db.executemany(
            f"INSERT INTO state_changes ({', '.join(_COLUMNS)}) VALUES "
            f"({', '.join('?' * len(_COLUMNS))})",
            [(when.isoformat(), entity, entity.split(".")[0], "off", state, "",
              when.hour, when.weekday(), "unknown") for entity, state, when in events])
        self.rows += len(events)
        return self

    def daily_pair(self, first, then, *, hour, gap_s, days=8, first_state="on",
                   then_state="on"):
        """``first`` reaches its state at ``hour`` each day, ``then`` follows
        ``gap_s`` seconds later."""
        for n in range(days):
            at = _start() + timedelta(days=n, hours=hour)
            self.add((first, first_state, at),
                     (then, then_state, at + timedelta(seconds=gap_s)))
        return self

    def busy_days(self, days=15):
        """A thousand changes a day across thirty switches, one a minute."""
        for n in range(days):
            day = _start(days) + timedelta(days=n)
            self.add(*[(f"switch.n{i % 30}", "on" if i % 2 else "off",
                        day + timedelta(minutes=i)) for i in range(1000)])
        return self


def _pair(patterns, first, then):
    return [p for p in patterns
            if p.details.get("trigger", {}).get("entity") == first
            and p.details.get("action", {}).get("entity") == then]


def _sequence(mod, *, delay, condition=None, first=("switch.a", "on"),
              then=("light.b", "on")):
    return mod.DetectedPattern(
        pattern_type="sequence", description="learned chain",
        entity_ids=[first[0], then[0]], confidence=0.7, occurrences=8,
        details={"trigger": {"entity": first[0], "state": first[1]},
                 "action": {"entity": then[0], "state": then[1]},
                 "delay_seconds": delay, "condition": condition})


# ── what is found ───────────────────────────────────────────────────────────

def test_a_follow_on_inside_the_window_is_found_with_its_count_and_wording(mod):
    store = _Store().daily_pair("light.a", "light.b", hour=18, gap_s=120)

    (seq,) = _pair(mod.PatternAnalyzer()._find_sequence_patterns(store.db),
                   "light.a", "light.b")

    assert seq.pattern_type == "sequence"
    assert seq.occurrences == 8
    assert seq.details["delay_seconds"] == 120
    assert seq.description.startswith(
        "When a turns on, b turns on shortly after (8 times in 30 days, ~120s later)")


@pytest.mark.parametrize("gap_minutes, learned", [
    (9, True),
    (10, True),     # the edge of the window still counts
    (11, False),
    (30, False),
])
def test_only_follow_ons_within_ten_minutes_count(mod, gap_minutes, learned):
    store = _Store().daily_pair("light.a", "light.b", hour=18, gap_s=gap_minutes * 60)

    found = _pair(mod.PatternAnalyzer()._find_sequence_patterns(store.db),
                  "light.a", "light.b")

    assert bool(found) is learned


def test_a_switch_can_drive_a_light_and_the_real_gap_is_measured(mod):
    store = _Store().daily_pair("switch.trigger", "light.follower", hour=18, gap_s=90)

    (seq,) = _pair(mod.PatternAnalyzer()._find_sequence_patterns(store.db),
                   "switch.trigger", "light.follower")

    assert seq.details["delay_seconds"] == 90
    assert "~90s later" in seq.description


def test_a_camera_detection_can_be_the_trigger(mod):
    # Someone at the front door, then the porch light: an uncontested history.
    store = _Store().daily_pair("camera_event.front_door", "light.porch", hour=19,
                                gap_s=20, first_state="person")

    (seq,) = _pair(mod.PatternAnalyzer()._find_sequence_patterns(store.db),
                   "camera_event.front_door", "light.porch")

    assert seq.occurrences >= 5
    assert seq.description.startswith("When front door turns person, porch turns on")


def test_a_camera_detection_is_never_learned_as_something_to_do(mod):
    # camera_event.* is not a real entity, so it has no service to call.
    store = _Store().daily_pair("light.porch", "camera_event.front_door", hour=19,
                                gap_s=20, then_state="person")

    found = mod.PatternAnalyzer()._find_sequence_patterns(store.db)

    assert _pair(found, "light.porch", "camera_event.front_door") == []


# ── conditions attached to a learned sequence ───────────────────────────────

def test_dark_evenings_give_a_sun_condition_rather_than_a_clock_window(mod, monkeypatch):
    monkeypatch.setattr(mod, "_is_dark_at", lambda epoch, lat, lon: True)
    store = _Store().daily_pair("binary_sensor.hall_motion", "light.hall", hour=20, gap_s=30)

    (seq,) = _pair(mod.PatternAnalyzer()._find_sequence_patterns(store.db, 40.7, -74.0),
                   "binary_sensor.hall_motion", "light.hall")

    assert seq.details["condition"] == [
        {"condition": "sun", "after": "sunset", "before": "sunrise"}]
    assert "after dark" in seq.description


def test_without_a_location_the_clock_window_is_used(mod):
    store = _Store().daily_pair("binary_sensor.hall_motion", "light.hall", hour=20, gap_s=30)

    (seq,) = _pair(mod.PatternAnalyzer()._find_sequence_patterns(store.db, None, None),
                   "binary_sensor.hall_motion", "light.hall")

    assert seq.details["condition"] == [
        {"condition": "time", "after": "19:00:00", "before": "21:00:00"}]
    assert "mostly between 19:00 and 21:00" in seq.description


# ── the automation a sequence becomes ───────────────────────────────────────

@pytest.mark.parametrize("measured, wait", [
    (0, None),
    (5, None),              # near immediate: no awkward little wait
    (14, None),
    (15, "00:00:15"),
    (90, "00:01:30"),       # the measured gap, not a fixed minute
    (92, "00:01:30"),       # rounded to five seconds
    (600, "00:10:00"),
])
def test_the_emitted_wait_follows_the_measured_gap(mod, measured, wait):
    auto = json.loads(mod.PatternAnalyzer()._generate_automation(
        _sequence(mod, delay=measured)))

    waits = [step["delay"] for step in auto["action"]
             if isinstance(step, dict) and "delay" in step]
    assert auto["trigger"] == {"platform": "state", "entity_id": "switch.a", "to": "on"}
    assert waits == ([] if wait is None else [wait])
    assert auto["action"][-1] == {"service": "light.turn_on", "entity_id": "light.b"}


def test_an_action_without_a_service_becomes_advice(mod):
    pattern = _sequence(mod, delay=30, then=("climate.lounge", "heat"))

    advice = json.loads(mod.PatternAnalyzer()._generate_automation(pattern))

    assert advice["type"] == "manual_review"
    assert "trigger" not in advice


# ── scale ───────────────────────────────────────────────────────────────────

def _timed(mod, conn):
    started = time.perf_counter()
    result = mod.PatternAnalyzer()._find_sequence_patterns(conn)
    return result, time.perf_counter() - started


def test_fifteen_thousand_changes_are_paired_quickly(mod):
    store = _Store().busy_days()
    assert store.rows >= 15000

    result, took = _timed(mod, store.db)

    assert isinstance(result, list)
    assert took < 10.0, f"sequence pass took {took:.1f}s on {store.rows} rows"


def test_camera_detections_mixed_into_a_large_history_stay_quick(mod):
    # Camera rows share the table and the detector with everything else, with
    # no special handling; mixing them in must not slow the pass down. Whether
    # a given pair survives the candidate cut-off here is not the point; the
    # clean dataset above covers that.
    store = _Store().busy_days()
    cameras = (("front_door", "person", 7), ("driveway", "vehicle", 12),
               ("front_door", "person", 19), ("backyard", "animal", 22))
    for n in range(15):
        day = _start(15) + timedelta(days=n)
        store.add(*[(f"camera_event.{where}", label, day + timedelta(hours=at))
                    for where, label, at in cameras])
    assert store.rows >= 15000

    result, took = _timed(mod, store.db)

    assert isinstance(result, list)
    assert took < 10.0, f"sequence pass took {took:.1f}s with camera rows mixed in"
