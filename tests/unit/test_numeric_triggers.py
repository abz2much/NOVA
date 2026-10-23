"""Learning "when a sensor crosses a threshold, do this" (numeric_state triggers).

The scorer has to find a real below/above threshold when an action keeps
happening on one side of it and the sensor genuinely crosses it, and refuse
scattered, flat or never-crossing sensors. The detector is fed a throwaway
SQLite log of state changes plus a synthetic sensor series, so no recorder is
needed.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

HEATER = "switch.space_heater"
LOUNGE_TEMP = "sensor.lounge_temperature"
QUARTER_HOUR = 900


@pytest.fixture
def pa(load):
    return load("pattern_analyzer")


@pytest.fixture
def log_db(tmp_path):
    """An empty state_changes table shaped like Nova's own."""
    db = sqlite3.connect(str(tmp_path / "changes.db"))
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE state_changes ("
               " id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,"
               " entity_id TEXT, domain TEXT, old_state TEXT, new_state TEXT,"
               " area_id TEXT, hour INTEGER, day_of_week INTEGER, person TEXT)")
    yield db
    db.close()


def _record_daily(db, entity_id, *, days=8, hour=6):
    """Log ``entity_id`` turning on once a day at the same time; returns the
    epochs it happened at. The start sits twelve days back so every entry is
    inside the analyser's look-back."""
    start = datetime.now() - timedelta(days=12)
    epochs = []
    for day in range(days):
        moment = start + timedelta(days=day, hours=hour, seconds=137)
        db.execute("INSERT INTO state_changes (timestamp, entity_id, domain, old_state,"
                   " new_state, hour, day_of_week, person) VALUES (?,?,?,?,?,?,?,?)",
                   (moment.isoformat(), entity_id, entity_id.partition(".")[0],
                    "off", "on", moment.hour, moment.weekday(), "unknown"))
        epochs.append(moment.timestamp())
    db.commit()
    return start, epochs


def _warm_series(start, readings, *, base=72.0, cycle=5):
    first = (start - timedelta(hours=1)).timestamp()
    return [(first + n * QUARTER_HOUR, base + (n % cycle)) for n in range(readings)]


# ── the pure helpers ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("at, expected", [
    (50.0, None),       # before the first reading
    (100.0, 10.0),      # exactly on a reading
    (250.0, 20.0),      # between readings: the earlier one is in force
    (999.0, 30.0),      # after the last
])
def test_value_in_force_at_a_moment(pa, at, expected):
    assert pa._numeric_value_at([100.0, 200.0, 300.0], [10.0, 20.0, 30.0], at) == expected


def test_value_in_force_without_readings(pa):
    assert pa._numeric_value_at([], [], 1.0) is None


@pytest.mark.parametrize("seen, op, threshold", [
    (64.0, "below", 65.0),   # "below 65" still includes 64
    (63.2, "below", 64.0),
    (72.0, "above", 71.0),   # "above 71" still includes 72
    (72.8, "above", 72.0),
])
def test_rounded_threshold_keeps_the_observed_side(pa, seen, op, threshold):
    assert pa._nice_threshold(seen, op) == threshold


SPREAD = list(range(55, 81)) * 2            # a sensor ranging over 55..80


@pytest.mark.parametrize("at_action, baseline, expected", [
    ([60, 61, 62, 63, 64], SPREAD, {"below": 65.0}),        # acts when cold
    ([72, 73, 74, 75, 76], SPREAD, {"above": 71.0}),        # acts when hot
    ([58, 64, 68, 72, 78], SPREAD, None),                   # no clear side
    ([70] * 5, [70] * 12, None),                            # a flat sensor
    # Always low with nothing high to cross from: no invented "below".
    ([58, 59, 60, 58, 59], [58, 59, 60, 61, 62] * 3, None),
    # The sensor spends almost no time past the would-be threshold, so a
    # "below 61" or "above 61" would hardly ever be crossed.
    ([60] * 5, [55, 55, 60] + [61] * 7, None),
    ([62] * 5, [61] * 7 + [62, 67, 67], None),
    ([60, 61, 62, 63], SPREAD, None),                       # too few occurrences
    ([60, 61, 62, 63, 64], list(range(55, 64)), None),      # too little history
], ids=["cold", "hot", "scattered", "flat", "no-crossing", "rarely-above",
        "rarely-below", "few", "short-history"])
def test_threshold_from_readings(pa, at_action, baseline, expected):
    assert pa._numeric_trigger_from(at_action, baseline) == expected


def test_crossings_are_counted_in_one_direction(load):
    patterns = load("automation.patterns")
    readings = [(4, 80.0), (1, 70.0), (3, 70.0), (2, 60.0)]   # unsorted on purpose
    assert patterns._threshold_crossings(readings, "below", 65.0) == [2]
    assert patterns._threshold_crossings(readings, "above", 75.0) == [4]


# ── the detector ─────────────────────────────────────────────────────────────

def _cold_before_each(start, epochs):
    """Warm every quarter hour over the whole span, dipping to 61 two
    minutes before each action: a real crossing every time (v7.126.0 needs
    the crossing, not just a low reading)."""
    return _warm_series(start, 13 * 96) + [(ep - 120, 61.0) for ep in epochs]


def test_learns_heater_when_the_room_turns_cold(pa, log_db):
    start, epochs = _record_daily(log_db, HEATER)
    history = {LOUNGE_TEMP: _cold_before_each(start, epochs)}
    # v7.126.0: a sensor explains only a device in its own area.
    areas = {LOUNGE_TEMP: "lounge", HEATER: "lounge"}
    found = pa.PatternAnalyzer()._find_numeric_triggers(log_db, history, entity_areas=areas)
    heater = [p for p in found if p.details["action"]["entity"] == HEATER]
    assert heater, "expected a numeric trigger for the heater when cold"
    learned = heater[0]
    assert learned.pattern_type == "numeric_trigger"
    assert learned.details["trigger_sensor"] == LOUNGE_TEMP
    assert learned.details["op"] == "below"
    assert learned.details["threshold"] == 62.0
    assert learned.details["threshold"] <= 65.0
    assert (learned.details["crossings"], learned.details["followed"]) == (8, 8)
    assert "below" in learned.description
    assert learned.description.startswith("When lounge temperature goes below 62")


def test_sensor_in_another_room_explains_nothing(pa, log_db):
    start, epochs = _record_daily(log_db, HEATER)
    history = {LOUNGE_TEMP: _cold_before_each(start, epochs)}
    areas = {LOUNGE_TEMP: "lounge", HEATER: "bedroom"}
    assert pa.PatternAnalyzer()._find_numeric_triggers(
        log_db, history, entity_areas=areas) == []


def test_unrelated_sensor_gives_no_trigger(pa, log_db):
    start, _ = _record_daily(log_db, "light.kitchen")
    # The temperature wanders on its own cycle, unrelated to the light.
    wandering = [(start.timestamp() + n * QUARTER_HOUR, 60.0 + (n % 20)) for n in range(300)]
    for areas in (None, {"sensor.kitchen_temp": "kitchen", "light.kitchen": "kitchen"}):
        found = pa.PatternAnalyzer()._find_numeric_triggers(
            log_db, {"sensor.kitchen_temp": wandering}, entity_areas=areas)
        assert not [p for p in found if p.details["action"]["entity"] == "light.kitchen"]


def test_no_sensor_history_means_no_triggers(pa, log_db):
    _record_daily(log_db, HEATER)
    assert pa.PatternAnalyzer()._find_numeric_triggers(log_db, {}) == []


# ── turning a learned threshold into an automation ──────────────────────────

def test_numeric_trigger_becomes_a_numeric_state_automation(pa):
    learned = pa.DetectedPattern(
        pattern_type="numeric_trigger", description="x",
        entity_ids=[LOUNGE_TEMP, HEATER], confidence=0.8, occurrences=9,
        details={"trigger_sensor": LOUNGE_TEMP, "op": "below", "threshold": 65.0,
                 "action": {"entity": HEATER, "state": "on"}})
    automation = json.loads(pa.PatternAnalyzer()._generate_automation(learned))
    assert automation["trigger"] == {"platform": "numeric_state",
                                     "entity_id": LOUNGE_TEMP, "below": 65.0}
    assert automation["action"] == [{"service": "switch.turn_on", "entity_id": HEATER}]
    assert "condition" not in automation
