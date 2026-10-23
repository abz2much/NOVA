"""A learned time routine is gated on its owner being home.

A clock trigger knows nothing about who is in, so when one person clearly owns
a daily routine the analyser attaches a Home Assistant state condition on that
person's person.* entity. The owner is matched to the entity through a map of
every way the person may have been recorded (friendly name, normalised name,
entity id). With no owner, or no entity for the owner, the routine is still
learned but stays ungated.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

import pytest

PORCH = "light.porch"
HOME_GATE = {"condition": "state", "entity_id": "person.alex", "state": "home"}


@pytest.fixture
def analyser(load):
    return load("pattern_analyzer").PatternAnalyzer()


@pytest.fixture
def pa(load):
    return load("pattern_analyzer")


@pytest.fixture
def history(load, tmp_path):
    """A patterns.db built from Nova's own schema, closed after the test."""
    component = load("persistence.schema").COMPONENTS["pattern_log"]
    conn = sqlite3.connect(str(tmp_path / "patterns.db"))
    for statement in component.statements:
        conn.execute(statement)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _evenings(conn, owners, *, entity=PORCH, sure=0.9):
    """One 19:00 switch-on per day for the last len(owners) days (ending
    three days ago), each credited to the matching owner."""
    first = datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)
    first -= timedelta(days=len(owners) + 3)
    rows = []
    for offset, who in enumerate(owners):
        at = first + timedelta(days=offset)
        rows.append((at.isoformat(), entity, entity.split(".")[0], "off", "on",
                     at.hour, at.weekday(), who, sure))
    conn.executemany(
        "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
        "new_state, hour, day_of_week, person, person_confidence) "
        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def _porch_routine(analyser, conn, person_map):
    found = [p for p in analyser._find_time_routines(conn, person_map)
             if p.entity_ids == [PORCH]]
    assert len(found) == 1, found
    return found[0]


# ── detection ────────────────────────────────────────────────────────────────

def test_owned_routine_is_gated_on_owner_home(analyser, history):
    _evenings(history, ["alex"] * 15)
    routine = _porch_routine(analyser, history, {"alex": "person.alex"})
    assert routine.details["person"] == "alex"
    assert routine.details["condition"] == HOME_GATE
    assert routine.details["hour"] == 19 and routine.details["state"] == "on"
    assert routine.description.endswith("when alex is home")


def test_owner_without_person_entity_stays_ungated(analyser, history):
    _evenings(history, ["alex"] * 15)
    routine = _porch_routine(analyser, history, {})
    # Still learned and still credited, but there is nothing to gate on.
    assert routine.details["person"] == "alex"
    assert "condition" not in routine.details


def test_shared_routine_has_no_owner_and_no_gate(analyser, history):
    # Eight evenings by one person, seven by another: nobody dominates.
    _evenings(history, ["alex", "sam"] * 7 + ["alex"])
    routine = _porch_routine(analyser, history,
                             {"alex": "person.alex", "sam": "person.sam"})
    assert "person" not in routine.details
    assert "condition" not in routine.details
    assert "is home" not in routine.description


# ── the automation it becomes ────────────────────────────────────────────────

def _routine_pattern(pa, condition):
    details = {"hour": 19, "minute": 0, "state": "on"}
    if condition is not None:
        details["condition"] = condition
    return pa.DetectedPattern(pattern_type="time_routine", description="porch at seven",
                              entity_ids=[PORCH], confidence=0.9, occurrences=15,
                              details=details)


def test_gate_is_carried_into_the_automation(pa, analyser):
    auto = json.loads(analyser._generate_automation(_routine_pattern(pa, HOME_GATE)))
    assert auto["trigger"] == {"platform": "time", "at": "19:00:00"}
    assert auto["action"] == {"service": "light.turn_on", "entity_id": PORCH}
    assert auto["condition"] == [HOME_GATE]


@pytest.mark.parametrize("condition", [None, {}, {"entity_id": "person.alex"}])
def test_no_condition_block_without_a_real_gate(pa, analyser, condition):
    auto = json.loads(analyser._generate_automation(_routine_pattern(pa, condition)))
    assert auto["trigger"]["platform"] == "time"
    assert "condition" not in auto


# ── how the gate reads in a description ──────────────────────────────────────

@pytest.mark.parametrize("condition, names, phrase", [
    (HOME_GATE, None, ", only when alex is home"),
    (HOME_GATE, {"person.alex": "Alex"}, ", only when Alex is home"),
    ({"condition": "state", "entity_id": "person.alex", "state": "not_home"},
     {"person.alex": "Alex"}, ", only when Alex is not_home"),
    ([HOME_GATE, {"condition": "sun", "after": "sunset"}], {"person.alex": "Alex"},
     ", only when Alex is home, mostly after dark"),
    ({"condition": "zone"}, None, ""),
    (None, None, ""),
])
def test_condition_phrase(pa, condition, names, phrase):
    assert pa._condition_phrase(condition, names) == phrase


# ── resolving a recorded owner to a person entity ────────────────────────────

def test_person_map_covers_every_recorded_form(analyser, fake_hass):
    fake_hass.states.set("person.alex_smith", "home", friendly_name="Alex Smith")
    fake_hass.states.set("person.sam", "not_home")            # no friendly name
    fake_hass.states.set("light.porch", "on", friendly_name="Porch")
    assert analyser._person_entity_map(fake_hass) == {
        "Alex Smith": "person.alex_smith",
        "alex_smith": "person.alex_smith",
        "person.alex_smith": "person.alex_smith",
        "sam": "person.sam",
        "person.sam": "person.sam",
    }


def test_person_map_is_empty_when_states_fail(analyser):
    class Broken:
        class states:
            @staticmethod
            def async_all(domain=None):
                raise RuntimeError("not ready")
    assert analyser._person_entity_map(Broken()) == {}
