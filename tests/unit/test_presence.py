"""Fresh behaviour tests for presence.py: who is home (person.* entities),
which rooms are occupied (occupancy binary sensors), and the one-line
prompt summary.

Written from the behaviour spec. Home Assistant is the fake core from
tests/fakes.py; the entity filter is either the real one (with a Nova
runtime installed for exclusions) or a recording stand-in."""
from __future__ import annotations

import builtins

import pytest

from conftest import _install_nova_runtime
from fakes import FakeHass


@pytest.fixture
def pr(load):
    return load("presence")


@pytest.fixture
def hass():
    return FakeHass()


def _person(hass, slug, state, **attrs):
    hass.states.set(f"person.{slug}", state, **attrs)


def _occ(hass, slug, state="on", device_class="occupancy", **attrs):
    hass.states.set(f"binary_sensor.{slug}", state, device_class=device_class, **attrs)


@pytest.fixture
def filt(load, monkeypatch):
    """Replace the entity filter's two predicates with recording stand-ins."""
    ef = load("entity_filter")
    rec = {"excluded": set(), "objects": set(), "excl_calls": [], "obj_calls": []}

    def is_excluded(hass, entity_id, *a, **k):
        rec["excl_calls"].append(entity_id)
        return entity_id in rec["excluded"]

    def is_object_sensor(entity_id, friendly_name=None):
        rec["obj_calls"].append((entity_id, friendly_name))
        return entity_id in rec["objects"]
    monkeypatch.setattr(ef, "is_excluded", is_excluded)
    monkeypatch.setattr(ef, "is_object_sensor", is_object_sensor)
    return rec


# ── person_name / _person_state ──────────────────────────────────────────────

def test_person_name_is_the_friendly_name(pr, hass):
    _person(hass, "alex", "home", friendly_name="Alex Doe")
    assert pr.person_name(hass, "person.alex") == "Alex Doe"


def test_person_name_falls_back_to_the_object_id(pr, hass):
    _person(hass, "sam_two", "home")
    assert pr.person_name(hass, "person.sam_two") == "sam_two"


def test_person_name_of_a_missing_entity_is_none(pr, hass):
    assert pr.person_name(hass, "person.nobody") is None


def test_person_name_keeps_a_present_but_empty_friendly_name(pr, hass):
    _person(hass, "blank", "home", friendly_name="")
    _person(hass, "null", "home", friendly_name=None)
    assert pr.person_name(hass, "person.blank") == ""
    assert pr.person_name(hass, "person.null") is None


def test_person_name_without_a_dot_uses_the_whole_id(pr, hass):
    hass.states.set("odd", "home")
    assert pr.person_name(hass, "odd") == "odd"


def test_person_state_shape(pr, hass):
    _person(hass, "alex", "Work", friendly_name="Alex", latitude=1.5, longitude=-2.25,
            gps_accuracy=10)
    out = pr._person_state(hass, "person.alex")
    assert out == {"name": "Alex", "state": "Work", "latitude": 1.5, "longitude": -2.25}
    assert list(out) == ["name", "state", "latitude", "longitude"]


def test_person_state_without_coordinates(pr, hass):
    _person(hass, "alex", "home")
    assert pr._person_state(hass, "person.alex") == {
        "name": "alex", "state": "home", "latitude": None, "longitude": None}


def test_person_state_of_a_missing_entity_is_empty(pr, hass):
    assert pr._person_state(hass, "person.ghost") == {}


# ── get_presence_summary: people ─────────────────────────────────────────────

def test_empty_house(pr, hass):
    out = pr.get_presence_summary(hass)
    assert out == {"total_people": 0, "home_count": 0, "away_count": 0,
                   "people": [], "rooms": {}, "anyone_home": False}
    assert list(out) == ["total_people", "home_count", "away_count", "people",
                         "rooms", "anyone_home"]


def test_counts_people_home_and_away(pr, hass):
    _person(hass, "alex", "home", friendly_name="Alex")
    _person(hass, "sam", "not_home", friendly_name="Sam")
    _person(hass, "kim", "Work", friendly_name="Kim")
    _person(hass, "lee", "unknown", friendly_name="Lee")
    out = pr.get_presence_summary(hass)
    assert (out["total_people"], out["home_count"], out["away_count"]) == (4, 1, 3)
    assert out["anyone_home"] is True
    assert [p["name"] for p in out["people"]] == ["Alex", "Sam", "Kim", "Lee"]
    assert out["people"][0] == {"name": "Alex", "state": "home",
                                "latitude": None, "longitude": None}


def test_home_is_case_sensitive(pr, hass):
    _person(hass, "alex", "Home")
    out = pr.get_presence_summary(hass)
    assert out["home_count"] == 0 and out["away_count"] == 1
    assert out["anyone_home"] is False


def test_only_person_entities_count(pr, hass):
    hass.states.set("device_tracker.phone", "home")
    hass.states.set("sensor.people_home", "3")
    assert pr.get_presence_summary(hass)["total_people"] == 0


def test_a_person_whose_lookup_vanishes_is_skipped(pr, hass, monkeypatch):
    _person(hass, "alex", "home")
    _person(hass, "sam", "home")
    real_get = hass.states.get
    monkeypatch.setattr(hass.states, "get",
                        lambda eid: None if eid == "person.sam" else real_get(eid))
    out = pr.get_presence_summary(hass)
    assert out["total_people"] == 1 and [p["name"] for p in out["people"]] == ["alex"]


# ── get_presence_summary: rooms ──────────────────────────────────────────────

def test_rooms_from_occupancy_sensor_names(pr, hass, filt):
    _occ(hass, "kitchen_presence", friendly_name="Kitchen Presence")
    _occ(hass, "office_occ", friendly_name="Office Occupancy")
    _occ(hass, "den_mmwave", friendly_name="Den mmWave")
    rooms = pr.get_presence_summary(hass)["rooms"]
    assert rooms == {"kitchen": [], "office": [], "den mmwave": []}
    assert list(rooms) == ["kitchen", "office", "den mmwave"]


def test_only_on_occupancy_sensors_count(pr, hass, filt):
    _occ(hass, "a", state="off", friendly_name="Attic Occupancy")
    _occ(hass, "b", device_class="presence", friendly_name="Bedroom Presence")
    _occ(hass, "c", device_class="motion", friendly_name="Cellar Motion")
    hass.states.set("binary_sensor.d", "on", friendly_name="Dining Occupancy")
    hass.states.set("sensor.e_presence", "on", device_class="occupancy",
                    friendly_name="Entry Presence")
    _occ(hass, "f", state="unavailable", friendly_name="Foyer Occupancy")
    assert pr.get_presence_summary(hass)["rooms"] == {}


def test_every_presence_and_occupancy_word_is_removed(pr, hass, filt):
    _occ(hass, "one", friendly_name="Presence Room Sensor")
    _occ(hass, "two", friendly_name="LOUNGE OCCUPANCY PRESENCE")
    _occ(hass, "three", friendly_name="   Study   ")
    assert list(pr.get_presence_summary(hass)["rooms"]) == ["room sensor", "lounge", "study"]


def test_a_name_that_is_only_the_keywords_adds_no_room(pr, hass, filt):
    _occ(hass, "one", friendly_name="Presence")
    _occ(hass, "two", friendly_name="occupancy  presence")
    assert pr.get_presence_summary(hass)["rooms"] == {}


def test_without_a_friendly_name_the_entity_id_is_used(pr, hass, filt):
    _occ(hass, "den_occupancy")
    assert list(pr.get_presence_summary(hass)["rooms"]) == ["binary_sensor.den_"]


def test_two_sensors_for_one_room_give_one_key(pr, hass, filt):
    _occ(hass, "k1", friendly_name="Kitchen Presence")
    _occ(hass, "k2", friendly_name="Kitchen Occupancy")
    assert pr.get_presence_summary(hass)["rooms"] == {"kitchen": []}


def test_rooms_never_list_people(pr, hass, filt):
    _person(hass, "alex", "home", friendly_name="Alex")
    _occ(hass, "k", friendly_name="Kitchen Presence")
    assert pr.get_presence_summary(hass)["rooms"]["kitchen"] == []


def test_excluded_and_object_sensors_are_skipped(pr, hass, filt):
    _occ(hass, "ghost_occupancy", friendly_name="Ghost Occupancy")
    _occ(hass, "driveway_car_occupancy", friendly_name="Driveway car occupancy")
    _occ(hass, "hall_presence", friendly_name="Hall Presence")
    filt["excluded"].add("binary_sensor.ghost_occupancy")
    filt["objects"].add("binary_sensor.driveway_car_occupancy")
    assert list(pr.get_presence_summary(hass)["rooms"]) == ["hall"]


def test_filter_is_asked_only_about_on_occupancy_sensors(pr, hass, filt):
    _occ(hass, "off_one", state="off", friendly_name="Off Occupancy")
    _occ(hass, "motion", device_class="motion", friendly_name="Motion")
    _occ(hass, "hall", friendly_name="Hall Presence")
    _occ(hass, "ghost", friendly_name="Ghost Occupancy")
    filt["excluded"].add("binary_sensor.ghost")
    pr.get_presence_summary(hass)
    assert filt["excl_calls"] == ["binary_sensor.hall", "binary_sensor.ghost"]
    # the object check runs after exclusion, with the name actually used
    assert filt["obj_calls"] == [("binary_sensor.hall", "Hall Presence")]


def test_object_check_gets_the_entity_id_as_name_when_unnamed(pr, hass, filt):
    _occ(hass, "nameless")
    pr.get_presence_summary(hass)
    assert filt["obj_calls"] == [("binary_sensor.nameless", "binary_sensor.nameless")]


def test_the_entity_filter_is_looked_up_at_call_time(pr, hass, load, monkeypatch):
    ef = load("entity_filter")
    _occ(hass, "hall", friendly_name="Hall Presence")
    monkeypatch.setattr(ef, "is_excluded", lambda h, e, *a, **k: True)
    assert pr.get_presence_summary(hass)["rooms"] == {}
    monkeypatch.setattr(ef, "is_excluded", lambda h, e, *a, **k: False)
    assert pr.get_presence_summary(hass)["rooms"] == {"hall": []}


def test_real_filter_honours_user_exclusions(pr, hass):
    _install_nova_runtime(hass, {"excluded_entities": ["binary_sensor.virtual_occupancy"]})
    _occ(hass, "virtual_occupancy", friendly_name="Virtual Occupancy")
    _occ(hass, "lab_presence", friendly_name="Lab Presence")
    assert list(pr.get_presence_summary(hass)["rooms"]) == ["lab"]


def test_real_filter_drops_object_sensors(pr, hass):
    _occ(hass, "garage_car_occupancy", friendly_name="Garage car occupancy")
    _occ(hass, "garage_person_occupancy", friendly_name="Garage person occupancy")
    assert list(pr.get_presence_summary(hass)["rooms"]) == ["garage person"]


def test_without_the_entity_filter_nothing_is_excluded(pr, hass, monkeypatch):
    real_import = builtins.__import__

    def no_filter(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "entity_filter" or name.endswith(".entity_filter"):
            raise ImportError("no entity filter")
        return real_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", no_filter)
    _occ(hass, "garage_car_occupancy", friendly_name="Garage car occupancy")
    rooms = pr.get_presence_summary(hass)["rooms"]
    assert list(rooms) == ["garage car"]


def test_a_none_friendly_name_on_an_occupancy_sensor_raises(pr, hass, filt):
    _occ(hass, "odd", friendly_name=None)
    with pytest.raises(AttributeError):
        pr.get_presence_summary(hass)


def test_summary_is_read_only(pr, hass, filt):
    _person(hass, "alex", "home")
    _occ(hass, "k", friendly_name="Kitchen Presence")
    before = {s.entity_id: (s.state, dict(s.attributes)) for s in hass.states.async_all()}
    pr.get_presence_summary(hass)
    pr.presence_context_string(hass)
    after = {s.entity_id: (s.state, dict(s.attributes)) for s in hass.states.async_all()}
    assert before == after
    assert hass.service_calls == [] and hass.data == {}


# ── presence_context_string ──────────────────────────────────────────────────

def test_no_people_message(pr, hass, filt):
    _occ(hass, "k", friendly_name="Kitchen Presence")
    assert pr.presence_context_string(hass) == \
        "No person entities configured in Home Assistant."


def test_context_line_per_person(pr, hass, filt):
    _person(hass, "alex", "home", friendly_name="Alex")
    _person(hass, "sam", "not_home", friendly_name="Sam")
    _person(hass, "kim", "Work", friendly_name="Kim")
    _person(hass, "lee", "gym_class", friendly_name="Lee")
    _person(hass, "jo", "unavailable", friendly_name="Jo")
    assert pr.presence_context_string(hass) == (
        "Alex is home. Sam is away. Kim is at Work. Lee is at gym class. "
        "Jo is at unavailable.")


def test_context_lists_occupied_areas(pr, hass, filt):
    _person(hass, "alex", "home", friendly_name="Alex")
    _occ(hass, "k", friendly_name="Kitchen Presence")
    _occ(hass, "o", friendly_name="Office Occupancy")
    assert pr.presence_context_string(hass) == \
        "Alex is home. Occupied areas: kitchen, office."


def test_context_for_one_person_away(pr, hass, filt):
    _person(hass, "sam", "not_home")
    assert pr.presence_context_string(hass) == "sam is away."


def test_context_prints_a_missing_name_as_none(pr, hass, filt):
    _person(hass, "x", "home", friendly_name=None)
    assert pr.presence_context_string(hass) == "None is home."


def test_context_uses_the_module_summary_at_call_time(pr, hass, monkeypatch):
    monkeypatch.setattr(pr, "get_presence_summary", lambda h: {
        "people": [{"name": "Robin", "state": "home"}], "rooms": {"porch": []}})
    assert pr.presence_context_string(hass) == "Robin is home. Occupied areas: porch."
    monkeypatch.setattr(pr, "get_presence_summary", lambda h: {
        "people": [], "rooms": {"porch": []}})
    assert pr.presence_context_string(hass) == \
        "No person entities configured in Home Assistant."


def test_public_names_exist(pr):
    for name in ("person_name", "get_presence_summary", "presence_context_string",
                 "_person_state"):
        assert callable(getattr(pr, name))
