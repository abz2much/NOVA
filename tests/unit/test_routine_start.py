"""Prompts for a person's learned time routine as its usual start arrives.

cognition.predict_routine_start reads the per person routine store and says
"You usually turn the kitchen light on around now." once a day, in the half
hour after the usual start, and only while the routine's owner is home.

The clock is pinned to a fixed local date, so nothing here depends on the hour
the suite happens to run at.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

NOON_PAST_FIVE = dt.datetime(2026, 6, 15, 12, 5)   # a Monday, local time
LAMP = "light.kitchen_light"


def _ts(when: dt.datetime) -> float:
    return when.timestamp()


def _stored_routine(hour=12, *, person="alex", confidence=0.8, entity=LAMP,
                    state="on", kind="time_routine", data=None):
    """One row as the routine analyser stores it in person_patterns."""
    details = data if data is not None else {
        "hour": hour, "state": state, "person": person, "entity_id": entity}
    return {
        "id": 3, "person": person, "pattern_type": kind, "occurrences": 8,
        "confidence": confidence,
        "description": f"{entity} turns {state} around {hour:02d}:00 on 8 of 9 "
                       f"days when {person} is home",
        "data": details if isinstance(details, str) else json.dumps(details),
    }


class Household:
    """Everything the predictor reads, held in one place a test can change."""

    def __init__(self, cognition, hass):
        self.cognition = cognition
        self.hass = hass
        self.rows: list = []
        self.home = {"alex"}
        self.settings = {"routine_alerts_enabled": True}
        self.decisions: list = []

    def predict(self, when: dt.datetime = NOON_PAST_FIVE) -> list:
        return self.cognition.predict_routine_start(self.hass, _ts(when))


@pytest.fixture
def house(load, monkeypatch, fake_hass):
    cognition = load("cognition")
    # A fresh once-a-day ledger for each test, put back afterwards.
    monkeypatch.setattr(cognition, "_RECUR_ALERTED", {})
    h = Household(cognition, fake_hass)

    def store_read(person=None, db_path=None):
        return h.rows

    def record(kind, **fields):
        h.decisions.append((kind, fields))
        return f"decision-{len(h.decisions)}"

    monkeypatch.setattr(load("person_patterns"), "read", store_read)
    monkeypatch.setattr(load("identity"), "_home_people", lambda hass: sorted(h.home))
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: h.settings.get(key, default))
    monkeypatch.setattr(load("decision_record"), "record", record)
    return h


# ── speaking up ──────────────────────────────────────────────────────────────

def test_prompt_carries_message_keys_and_decision(house):
    house.rows = [_stored_routine()]

    (prompt,) = house.predict()

    assert prompt == {
        "type": "anticipation_routine", "urgency": "low",
        "message": "You usually turn the kitchen light on around now.",
        "pattern_key": f"routine:alex:{LAMP}|on|12", "offer": False,
        "habit_key": f"routine:alex:{LAMP}:on", "entity_id": LAMP,
        "decision_id": "decision-1",
    }
    kind, fields = house.decisions[0]
    assert kind == "anticipation_routine"
    assert fields["ref"] == f"entity:{LAMP}"
    assert fields["observation"] == {"person": "alex", "routine": f"{LAMP}|on|12"}


def test_current_friendly_name_is_used(house):
    house.hass.states.set(LAMP, "off", friendly_name="Kitchen Lamp")
    house.rows = [_stored_routine()]
    assert [p["message"] for p in house.predict()] == [
        "You usually turn the Kitchen Lamp on around now."]


def test_owner_is_named_when_someone_else_is_home(house):
    house.home = {"alex", "sam"}
    house.rows = [_stored_routine()]
    assert [p["message"] for p in house.predict()] == [
        "Alex, you usually turn the kitchen light on around now."]


def test_entity_falls_back_to_description_for_older_rows(house):
    # Rows from before entity_id was kept only name the entity in words.
    house.rows = [_stored_routine(data={"hour": 12, "state": "on"})]
    (prompt,) = house.predict()
    assert prompt["entity_id"] == LAMP
    assert prompt["message"] == "You usually turn the kitchen light on around now."


# ── the half hour after the usual start ──────────────────────────────────────

@pytest.mark.parametrize("hour, minute, expected", [
    (12, 0, 1),     # right on time
    (12, 30, 1),    # the last minute of the tolerance
    (12, 31, 0),    # one minute too late
    (11, 59, 0),    # before it usually starts
    (15, 5, 0),     # a different hour altogether
])
def test_tolerance_window_edges(house, hour, minute, expected):
    house.rows = [_stored_routine(hour=12)]
    when = NOON_PAST_FIVE.replace(hour=hour, minute=minute)
    assert len(house.predict(when)) == expected


# ── staying quiet ────────────────────────────────────────────────────────────

def _away(h):
    h.home = set()


def _someone_else_home(h):
    h.home = {"sam"}


def _switched_off(h):
    h.settings["routine_alerts_enabled"] = False


def _routine(**kw):
    def put(h):
        h.rows = [_stored_routine(**kw)]
    return put


@pytest.mark.parametrize("arrange", [
    _away,
    _someone_else_home,
    _switched_off,
    _routine(confidence=0.4),
    _routine(confidence=0.64),
    _routine(data={}),                                   # no hour learned
    _routine(data="{not json"),                          # unreadable details
    _routine(kind="repeated_command"),
    _routine(entity="device_tracker.alex_phone", state="home"),
    _routine(state="unavailable"),
    _routine(person=""),
], ids=["away", "other_person_home", "switched_off", "low_confidence",
        "just_under_bar", "no_hour", "bad_json", "voice_command",
        "tracker_not_a_habit", "unavailable_state", "no_owner"])
def test_no_prompt(house, arrange):
    house.rows = [_stored_routine()]
    arrange(house)
    assert house.predict() == []
    assert house.decisions == []


def test_confidence_exactly_at_bar_still_prompts(house):
    house.rows = [_stored_routine(confidence=0.65)]
    assert len(house.predict()) == 1


def test_empty_store_says_nothing(house):
    assert house.predict() == []


def test_store_failure_is_swallowed(house, monkeypatch, load):
    def broken(person=None, db_path=None):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(load("person_patterns"), "read", broken)
    assert house.predict() == []


# ── once a day ───────────────────────────────────────────────────────────────

def test_once_a_day_then_again_tomorrow(house):
    house.rows = [_stored_routine()]
    assert len(house.predict()) == 1
    # Later in the same window: already said today.
    assert house.predict(NOON_PAST_FIVE.replace(minute=20)) == []
    # Same time tomorrow it is due again.
    assert len(house.predict(NOON_PAST_FIVE + dt.timedelta(days=1))) == 1
    assert len(house.decisions) == 2
