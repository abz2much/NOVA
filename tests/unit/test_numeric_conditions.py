"""numeric_state conditions on learned sequences ("...but only while it is below X").

The condition helper reuses the numeric trigger's scorer, so its guards
against coincidence apply here too. Conditions build up as a list that Home
Assistant ANDs, so a sun or time window and a numeric condition can both sit
on one suggestion; the generator and the normaliser must keep that list whole.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

STEP = 900                                   # one reading every 15 minutes
SUN_AFTER_DARK = {"condition": "sun", "after": "sunset", "before": "sunrise"}


@pytest.fixture
def pa(load):
    return load("pattern_analyzer")


def _twelve_days_ago():
    return datetime.now() - timedelta(days=12)


def _daily(origin, hour, days=8):
    """One epoch per day at ``hour`` (plus a stray 137 s), ``days`` days running."""
    return [(origin + timedelta(days=day, hours=hour, seconds=137)).timestamp()
            for day in range(days)]


def _mild_with_dips(origin, dip_value, dip_epochs, *, count=200, skip_before=None):
    """Readings around 72-76, plus a cold reading one second before each of
    ``dip_epochs``. With ``skip_before`` the regular readings start there."""
    first = origin.timestamp() - 3600
    readings = [(first + n * STEP, 72.0 + (n % 5)) for n in range(count)
                if skip_before is None or first + n * STEP >= skip_before]
    return readings + [(epoch - 1, dip_value) for epoch in dip_epochs]


# ── the helper on its own ────────────────────────────────────────────────────

def test_cold_at_every_action_gives_a_below_condition(pa):
    origin = _twelve_days_ago()
    actions = _daily(origin, 6)
    sensor = "sensor.lounge_temperature"
    found = pa._numeric_condition(actions, {sensor: _mild_with_dips(origin, 61.0, actions)})
    assert found is not None
    assert found == {"condition": "numeric_state", "entity_id": sensor, "below": 62.0}
    assert found["below"] <= 65.0


def test_sensor_wandering_on_its_own_gives_nothing(pa):
    origin = _twelve_days_ago()
    actions = _daily(origin, 6)
    wandering = [(origin.timestamp() + n * STEP, 60.0 + (n % 20)) for n in range(300)]
    assert pa._numeric_condition(actions, {"sensor.temp": wandering}) is None


@pytest.mark.parametrize("actions, history", [
    ([float(n) for n in range(8)], {}),                       # no sensors at all
    ([float(n) for n in range(4)], {"sensor.t": [(0.0, 1.0)] * 20}),   # too few actions
    ([float(n) for n in range(8)], {"sensor.t": [(0.0, 60.0)] * 9}),   # under ten readings
], ids=["no-history", "few-actions", "short-series"])
def test_not_enough_to_go_on_gives_nothing(pa, actions, history):
    assert pa._numeric_condition(actions, history) is None


def test_sensor_read_at_more_actions_wins(pa):
    origin = _twelve_days_ago()
    actions = _daily(origin, 6)
    # The landing sensor only starts reporting after the second action, so it
    # covers six of the eight; the lounge sensor covers all of them. The
    # partial one is listed first so the better one has to displace it.
    history = {
        "sensor.landing_temperature": _mild_with_dips(
            origin, 60.0, actions[2:], skip_before=actions[1] + 60),
        "sensor.lounge_temperature": _mild_with_dips(origin, 61.0, actions),
    }
    found = pa._numeric_condition(actions, history)
    assert found == {"condition": "numeric_state",
                     "entity_id": "sensor.lounge_temperature", "below": 62.0}


def test_non_numeric_readings_are_skipped(pa):
    origin = _twelve_days_ago()
    actions = _daily(origin, 6)
    readings = _mild_with_dips(origin, 61.0, actions)
    readings += [(epoch - 30, "unavailable") for epoch in actions]
    found = pa._numeric_condition(actions, {"sensor.lounge_temperature": readings})
    assert found == {"condition": "numeric_state",
                     "entity_id": "sensor.lounge_temperature", "below": 62.0}


# ── the sequence detector attaches it alongside a time or sun condition ─────

@pytest.fixture
def changes(tmp_path):
    db = sqlite3.connect(str(tmp_path / "seq.db"))
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE state_changes ("
               " id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,"
               " entity_id TEXT, domain TEXT, old_state TEXT, new_state TEXT,"
               " area_id TEXT, hour INTEGER, day_of_week INTEGER, person TEXT)")
    yield db
    db.close()


def _turned_on(db, entity_id, moment):
    db.execute("INSERT INTO state_changes (timestamp, entity_id, domain, old_state,"
               " new_state, hour, day_of_week, person) VALUES (?,?,?,?,?,?,?,?)",
               (moment.isoformat(), entity_id, entity_id.split(".", 1)[0], "off",
                "on", moment.hour, moment.weekday(), "unknown"))


def test_motion_then_heater_carries_numeric_and_time_conditions(pa, changes):
    origin = _twelve_days_ago()
    heater_on = []
    for day in range(8):
        motion = origin + timedelta(days=day, hours=20, seconds=137)   # 8pm, dark
        _turned_on(changes, "binary_sensor.hall_motion", motion)
        heater = motion + timedelta(seconds=30)
        _turned_on(changes, "switch.space_heater", heater)
        heater_on.append(heater.timestamp())
    changes.commit()
    history = {"sensor.hall_temperature": _mild_with_dips(origin, 60.0, heater_on)}

    found = pa.PatternAnalyzer()._find_sequence_patterns(changes, 40.7, -74.0, history)
    ours = [p for p in found
            if p.details["trigger"]["entity"] == "binary_sensor.hall_motion"]
    assert ours, "expected the motion then heater sequence"
    conditions = ours[0].details["condition"]
    assert isinstance(conditions, list)
    kinds = {c["condition"] for c in conditions}
    # Which time based condition is chosen (sun after dark, otherwise a time
    # window) depends on the host's time zone, so either is accepted.
    assert "numeric_state" in kinds
    assert kinds & {"sun", "time"}
    numeric = [c for c in conditions if c["condition"] == "numeric_state"]
    assert len(numeric) == 1
    assert numeric[0]["entity_id"] == "sensor.hall_temperature"
    assert numeric[0]["below"] == 61.0
    assert numeric[0]["below"] <= 62.0
    assert "below" in ours[0].description


# ── the condition list survives generation and normalising ──────────────────

HALL_COLD = {"condition": "numeric_state", "entity_id": "sensor.hall_temperature",
             "below": 62.0}


def test_generated_automation_keeps_both_conditions(pa):
    motion, heater = "binary_sensor.hall_motion", "switch.space_heater"
    details = {
        "trigger": {"entity": motion, "state": "on"},
        "action": {"entity": heater, "state": "on"},
        "delay_seconds": 30,
        "condition": [dict(SUN_AFTER_DARK), dict(HALL_COLD)],
    }
    learned = pa.DetectedPattern(pattern_type="sequence", description="x",
                                 entity_ids=[motion, heater], confidence=0.8,
                                 occurrences=9, details=details)
    automation = json.loads(pa.PatternAnalyzer()._generate_automation(learned))
    assert isinstance(automation["condition"], list)
    assert len(automation["condition"]) == 2
    assert {c["condition"] for c in automation["condition"]} == {"sun", "numeric_state"}
    assert automation["condition"] == [SUN_AFTER_DARK, HALL_COLD]
    assert automation["action"][0] == {"delay": "00:00:30"}


def test_normalising_a_stored_suggestion_keeps_the_list(pa):
    stored = {
        "alias": "x",
        "trigger": {"platform": "state", "entity_id": "binary_sensor.hall_motion",
                    "to": "on"},
        "condition": [dict(SUN_AFTER_DARK),
                      {"condition": "numeric_state", "entity_id": "sensor.t", "below": 62.0}],
        "action": [{"service": "switch.turn_on",
                    "target": {"entity_id": "switch.space_heater"}}],
    }
    normalised = pa.normalize_suggestion_automation(json.dumps(stored))
    assert len(normalised["condition"]) == 2
    assert {c["condition"] for c in normalised["condition"]} == {"sun", "numeric_state"}
