"""Learned arrivals and departures become zone triggers.

When a person or a tracker going home <-> away keeps being followed by the same
action, the suggestion fires on Home Assistant's own zone trigger (enter or
leave zone.home) instead of a raw state change. Other entities, and people
moving between named zones, keep a plain state trigger.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

ALEX = "person.alex"
GARAGE = "cover.garage"


@pytest.fixture
def pa(load):
    return load("pattern_analyzer")


def _zone(entity, event):
    return {"platform": "zone", "entity_id": entity, "zone": "zone.home", "event": event}


# ── choosing the trigger ─────────────────────────────────────────────────────

@pytest.mark.parametrize("entity, state, trigger", [
    (ALEX, "not_home", _zone(ALEX, "leave")),
    (ALEX, "home", _zone(ALEX, "enter")),
    ("device_tracker.alex_phone", "not_home", _zone("device_tracker.alex_phone", "leave")),
    ("device_tracker.alex_phone", "home", _zone("device_tracker.alex_phone", "enter")),
    # A named zone is not home or away, so it stays a state trigger.
    (ALEX, "work", {"platform": "state", "entity_id": ALEX, "to": "work"}),
    ("binary_sensor.hall_motion", "on",
     {"platform": "state", "entity_id": "binary_sensor.hall_motion", "to": "on"}),
    # Presses and scenes change on every activation: no fixed target state.
    ("event.hall_button", "short_press", {"platform": "state", "entity_id": "event.hall_button"}),
    ("scene.evening", "2026-01-01T18:00:00", {"platform": "state", "entity_id": "scene.evening"}),
])
def test_trigger_for(pa, entity, state, trigger):
    assert pa._trigger_for(entity, state) == trigger


@pytest.mark.parametrize("entity, state, names, phrase", [
    (ALEX, "not_home", None, "When alex leaves home"),
    (ALEX, "home", None, "When alex arrives home"),
    (ALEX, "home", {ALEX: "Alex"}, "When Alex arrives home"),
    ("device_tracker.alex_phone", "not_home", None, "When alex phone leaves home"),
    ("light.hall", "on", None, "When hall turns on"),
    ("event.hall_button", "short_press", None, "When hall button is pressed (short_press)"),
    ("scene.evening", "x", {"scene.evening": "Evening"}, "When Evening is activated"),
])
def test_trigger_phrase(pa, entity, state, names, phrase):
    assert pa._trigger_phrase(entity, state, names) == phrase


# ── the automation a presence sequence becomes ───────────────────────────────

def _presence_sequence(pa, state, delay):
    return pa.DetectedPattern(
        pattern_type="sequence", description="garage after alex", confidence=0.8,
        occurrences=9, entity_ids=[ALEX, GARAGE],
        details={"trigger": {"entity": ALEX, "state": state},
                 "action": {"entity": GARAGE, "state": "closed"},
                 "delay_seconds": delay, "condition": None})


def test_departure_becomes_leave_trigger(pa):
    auto = json.loads(pa.PatternAnalyzer()._generate_automation(
        _presence_sequence(pa, "not_home", 30)))
    assert auto == {
        "alias": "Nova Learned: garage when alex leaves home",
        "trigger": _zone(ALEX, "leave"),
        "action": [{"delay": "00:00:30"},
                   {"service": "cover.close_cover", "entity_id": GARAGE}],
    }


def test_arrival_becomes_enter_trigger_without_short_wait(pa):
    auto = json.loads(pa.PatternAnalyzer()._generate_automation(
        _presence_sequence(pa, "home", 5)))
    assert auto["alias"] == "Nova Learned: garage when alex arrives home"
    assert auto["trigger"] == _zone(ALEX, "enter")
    # Under fifteen seconds the action follows straight away.
    assert auto["action"] == [{"service": "cover.close_cover", "entity_id": GARAGE}]
    assert "condition" not in auto


def test_zone_trigger_survives_install_normalisation(pa):
    stored = json.dumps({
        "alias": "garage when alex leaves",
        "trigger": _zone(ALEX, "leave"),
        "action": [{"service": "cover.close_cover", "target": {"entity_id": GARAGE}}],
    })
    result = pa.normalize_suggestion_automation(stored)
    assert result["installable"] is True
    # Home Assistant's newer spelling: "platform" becomes "trigger".
    (trigger,) = result["trigger"]
    assert trigger == {"trigger": "zone", "entity_id": ALEX,
                       "zone": "zone.home", "event": "leave"}


# ── detection from history ───────────────────────────────────────────────────

@pytest.fixture
def history(load, tmp_path):
    """A patterns.db built from Nova's own schema."""
    conn = sqlite3.connect(str(tmp_path / "patterns.db"))
    for statement in load("persistence.schema").COMPONENTS["pattern_log"].statements:
        conn.execute(statement)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _log_pairs(conn, first, then, *, days=8, gap_s=30):
    """`first` then, gap_s later, `then`, once a morning for `days` days."""
    start = datetime.now().replace(hour=8, minute=2, second=17, microsecond=0)
    start -= timedelta(days=days + 4)
    rows = []
    for day in range(days):
        at = start + timedelta(days=day)
        for (entity, old, new), when in ((first, at), (then, at + timedelta(seconds=gap_s))):
            rows.append((when.isoformat(), entity, entity.split(".")[0], old, new,
                         when.hour, when.weekday()))
    conn.executemany(
        "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
        "new_state, hour, day_of_week) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()


def _sequence_for(pa, conn, trigger_entity, action_entity):
    found = [p for p in pa.PatternAnalyzer()._find_sequence_patterns(conn, None, None)
             if p.details["trigger"]["entity"] == trigger_entity
             and p.details["action"]["entity"] == action_entity]
    assert found, "expected the sequence to be learned"
    return found[0]


def test_departure_history_is_learned_and_emitted_as_zone(pa, history):
    _log_pairs(history, (ALEX, "home", "not_home"), (GARAGE, "open", "closed"))
    learned = _sequence_for(pa, history, ALEX, GARAGE)
    assert learned.details["trigger"]["state"] == "not_home"
    assert learned.details["action"]["state"] == "closed"
    assert "leaves home" in learned.description
    auto = json.loads(pa.PatternAnalyzer()._generate_automation(learned))
    assert auto["trigger"] == _zone(ALEX, "leave")


def test_too_few_departures_are_not_learned(pa, history):
    _log_pairs(history, (ALEX, "home", "not_home"), (GARAGE, "open", "closed"), days=3)
    assert [p for p in pa.PatternAnalyzer()._find_sequence_patterns(history, None, None)
            if p.details["trigger"]["entity"] == ALEX] == []
