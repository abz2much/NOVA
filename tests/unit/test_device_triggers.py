"""Buttons and remotes as learned triggers: "press, then a scene".

Home Assistant shows a button or remote press as an event.* entity whose
event_type attribute names the press. When the user opts in, the logger keeps
those with the press type as the state, so the ordinary sequence detector
finds "press, then action" without a detector of its own. On the way out a
press becomes a bare state trigger on the entity plus a template condition
for the press type. Scenes are logged too, so "button, then scene" resolves
to scene.turn_on.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import date, datetime, time as clock, timedelta

import pytest


@pytest.fixture
def mod(load):
    return load("pattern_analyzer")


@pytest.fixture
def core(load):
    return load("cognitive_core")


@pytest.fixture
def state_log(core, tmp_path):
    """A real StateLogger writing to its own file."""
    return core.StateLogger(db_path=str(tmp_path / "learning.db"))


def _logged(state_log, domain):
    with sqlite3.connect(state_log._db_path) as conn:
        return [row[0] for row in conn.execute(
            "SELECT new_state FROM state_changes WHERE domain = ? ORDER BY id", (domain,))]


@pytest.fixture
def settings(core, monkeypatch):
    """Stand-in nova_config for the opt-in checks; tests fill ``values``.

    Patched in sys.modules only: the shared set-up makes the package read
    its members from there."""
    values: dict = {}
    fake = types.SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    package = core.__name__.rsplit(".", 1)[0]
    monkeypatch.setitem(sys.modules, f"{package}.nova_config", fake)
    return values


# ── what the logger keeps ───────────────────────────────────────────────────

def test_a_button_press_is_kept_only_when_opted_in(state_log):
    state_log.log_state_change("event.remote", "", "single", force_include=False)
    assert _logged(state_log, "event") == []

    state_log.log_state_change("event.remote", "", "single", force_include=True)
    assert _logged(state_log, "event") == ["single"]   # the press type is the value


@pytest.mark.parametrize("entity", ["automation.porch", "script.bedtime",
                                    "input_boolean.guest_mode"])
def test_nova_plumbing_is_never_kept_even_when_forced(state_log, entity):
    state_log.log_state_change(entity, "off", "on", force_include=True)

    assert _logged(state_log, entity.split(".")[0]) == []


def test_a_scene_being_activated_is_kept_as_an_action(state_log):
    state_log.log_state_change("scene.movie_night", "", "activated")

    assert _logged(state_log, "scene") == ["activated"]


@pytest.mark.parametrize("values, entity, expected", [
    ({"pattern_learn_buttons": True}, "event.remote", True),
    ({"pattern_learn_buttons": False}, "event.remote", False),
    ({}, "event.remote", False),                                  # off by default
    ({"pattern_include_entities": ["event.remote"]}, "event.remote", True),
    ({"pattern_learn_buttons": True}, "sensor.lux", False),       # buttons only
])
def test_the_buttons_opt_in(core, settings, values, entity, expected):
    settings.update(values)

    assert core._pattern_opted_in(entity, "") is expected


# ── helpers that shape a press trigger ──────────────────────────────────────

@pytest.mark.parametrize("entity, state, trigger", [
    ("event.remote", "double", {"platform": "state", "entity_id": "event.remote"}),
    ("scene.movie", "activated", {"platform": "state", "entity_id": "scene.movie"}),
    ("light.hall", "on", {"platform": "state", "entity_id": "light.hall", "to": "on"}),
])
def test_trigger_shape(mod, entity, state, trigger):
    assert mod._trigger_for(entity, state) == trigger


def test_only_a_press_needs_an_extra_condition(mod):
    (press,) = mod._trigger_extra_conditions("event.remote", "double")

    assert press == {"condition": "template", "value_template":
                     "{{ trigger.to_state.attributes.event_type == 'double' }}"}
    assert mod._trigger_extra_conditions("light.hall", "on") == []
    assert mod._trigger_extra_conditions("scene.movie", "activated") == []


@pytest.mark.parametrize("entity, state, words", [
    ("event.remote", "double", "When remote is pressed (double)"),
    ("scene.movie", "activated", "When movie is activated"),
    ("light.hall", "on", "When hall turns on"),
])
def test_trigger_wording(mod, entity, state, words):
    assert mod._trigger_phrase(entity, state) == words


@pytest.mark.parametrize("entity, state, call", [
    ("scene.movie_night", "activated",
     {"service": "scene.turn_on", "entity_id": "scene.movie_night"}),
    ("lock.front", "locked", {"service": "lock.lock", "entity_id": "lock.front"}),
    ("camera_event.front_door", "person", None),     # never something to do
    ("event.remote", "single", None),                # a press is not an action
])
def test_service_for_actions(mod, entity, state, call):
    assert mod.service_for(entity, state) == call


# ── the automation a press becomes ──────────────────────────────────────────

def _press_then_scene(mod, press="single", scene="scene.movie_night"):
    return mod.DetectedPattern(
        pattern_type="sequence", description="press chain",
        entity_ids=["event.living_remote", scene], confidence=0.8, occurrences=9,
        details={"trigger": {"entity": "event.living_remote", "state": press},
                 "action": {"entity": scene, "state": "activated"},
                 "delay_seconds": 0, "condition": None})


def test_a_press_becomes_a_state_trigger_with_a_press_template(mod):
    auto = json.loads(mod.PatternAnalyzer()._generate_automation(_press_then_scene(mod)))

    # Any change of the entity fires it; the template picks out the press.
    assert auto["trigger"] == {"platform": "state", "entity_id": "event.living_remote"}
    templates = [c["value_template"] for c in auto["condition"]
                 if c["condition"] == "template"]
    assert len(templates) == 1 and "event_type == 'single'" in templates[0]
    assert auto["action"] == [{"service": "scene.turn_on",
                               "entity_id": "scene.movie_night"}]
    assert "press" in auto["alias"]


def test_a_press_automation_passes_normalisation(mod):
    stored = mod.PatternAnalyzer()._generate_automation(_press_then_scene(mod))

    result = mod.normalize_suggestion_automation(stored)

    assert result["installable"] is True
    assert result["trigger"] == [{"trigger": "state", "entity_id": "event.living_remote"}]
    assert result["condition"][0]["condition"] == "template"
    assert result["action"][-1]["action"] == "scene.turn_on"


def test_a_hand_written_press_payload_also_normalises(mod):
    result = mod.normalize_suggestion_automation(json.dumps({
        "alias": "remote to film scene",
        "trigger": {"platform": "state", "entity_id": "event.living_remote"},
        "condition": [{"condition": "template", "value_template":
                       "{{ trigger.to_state.attributes.event_type == 'single' }}"}],
        "action": [{"service": "scene.turn_on",
                    "target": {"entity_id": "scene.movie_night"}}],
    }))

    assert result["installable"] is True
    assert result["trigger"][0]["trigger"] == "state"
    assert result["condition"][0]["condition"] == "template"


# ── learned end to end from history ─────────────────────────────────────────

def _history_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE state_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,"
        " entity_id TEXT, domain TEXT, old_state TEXT, new_state TEXT, area_id TEXT,"
        " hour INTEGER, day_of_week INTEGER, person TEXT)")
    return conn


def _record(conn, entity, state, when):
    conn.execute(
        "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, new_state,"
        " hour, day_of_week, person) VALUES (?, ?, ?, '', ?, ?, ?, 'unknown')",
        (when.isoformat(), entity, entity.partition(".")[0], state,
         when.hour, when.weekday()))


def _evenings(days=8):
    first = datetime.combine(date.today(), clock()) - timedelta(days=12)
    return [first + timedelta(days=n, hours=20, seconds=137) for n in range(days)]


def _found(mod, conn, first, then):
    return [p for p in mod.PatternAnalyzer()._find_sequence_patterns(conn, None, None)
            if p.details["trigger"]["entity"] == first
            and p.details["action"]["entity"] == then]


def test_a_press_then_a_scene_is_learned_and_emitted(mod):
    conn = _history_db()
    for at in _evenings():
        _record(conn, "event.living_remote", "single", at)
        _record(conn, "scene.movie_night", "activated", at + timedelta(seconds=5))

    (seq,) = _found(mod, conn, "event.living_remote", "scene.movie_night")
    assert seq.details["trigger"]["state"] == "single"
    assert "is pressed (single)" in seq.description

    auto = json.loads(mod.PatternAnalyzer()._generate_automation(seq))
    assert auto["trigger"] == {"platform": "state", "entity_id": "event.living_remote"}
    assert auto["action"][-1]["service"] == "scene.turn_on"
    assert any(c["condition"] == "template" for c in auto["condition"])


def test_different_presses_on_one_remote_learn_different_scenes(mod):
    conn = _history_db()
    for at in _evenings():
        _record(conn, "event.living_remote", "single", at)
        _record(conn, "scene.movie_night", "activated", at + timedelta(seconds=5))
        later = at + timedelta(hours=2)
        _record(conn, "event.living_remote", "double", later)
        _record(conn, "scene.bedtime", "activated", later + timedelta(seconds=5))

    (film,) = _found(mod, conn, "event.living_remote", "scene.movie_night")
    (bed,) = _found(mod, conn, "event.living_remote", "scene.bedtime")

    assert film.details["trigger"]["state"] == "single"
    assert bed.details["trigger"]["state"] == "double"
    bed_auto = json.loads(mod.PatternAnalyzer()._generate_automation(bed))
    assert "event_type == 'double'" in bed_auto["condition"][-1]["value_template"]
