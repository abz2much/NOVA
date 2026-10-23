"""quick_identify falls back to the house when the room cannot decide.

With an area id, quick_identify first asks resolve() about that room. When
the room gives no name but exactly one person is home, the event is still
theirs. Returning "unknown" there used to keep single person homes from
ever forming personal routines.
"""
from __future__ import annotations

import pytest

ALEX, JORDAN = "Alex", "Jordan"


@pytest.fixture
def house(load, monkeypatch):
    """identity with the room answer and the people at home chosen per test.

    Set ``room`` to the Identification resolve() should give, ``home`` to
    who is in, ``enabled`` for the master switch. ``asked`` collects the
    area ids resolve() was called with."""
    idm = load("identity")
    world = {"room": idm.Identification(), "home": [], "enabled": True, "asked": []}

    def fake_resolve(hass, area_id=None):
        world["asked"].append(area_id)
        room = world["room"]
        if isinstance(room, Exception):
            raise room
        return room

    monkeypatch.setattr(idm, "resolve", fake_resolve)
    monkeypatch.setattr(idm, "_home_people", lambda hass: list(world["home"]))
    monkeypatch.setattr(idm, "_cfg", lambda key, default: world["enabled"])
    world["mod"] = idm
    return world


def test_an_undecided_room_with_one_person_home_names_them(house, fake_hass):
    idm = house["mod"]
    house["room"] = idm.Identification(idm.UNKNOWN, 0.0, "no_room_signal", {})
    house["home"] = [ALEX]
    got = idm.quick_identify(fake_hass, area_id="kitchen")
    assert got.known and got.person == ALEX
    assert (got.confidence, got.method, got.candidates) == (0.6, "sole_occupant", {ALEX: 0.6})
    assert house["asked"] == ["kitchen"]          # the room was tried first


def test_a_room_that_names_someone_is_returned_as_it_is(house, fake_hass):
    idm = house["mod"]
    named = idm.Identification(JORDAN, 0.9, "face", {JORDAN: 0.9})
    house["room"], house["home"] = named, [ALEX]   # the room beats the house
    got = idm.quick_identify(fake_hass, area_id="kitchen")
    assert got.person == JORDAN
    assert got is named


def test_with_several_home_the_room_candidates_are_kept(house, fake_hass):
    idm = house["mod"]
    house["room"] = idm.Identification(idm.UNKNOWN, 0.4, "room", {ALEX: 0.4, JORDAN: 0.3})
    house["home"] = [ALEX, JORDAN]
    got = idm.quick_identify(fake_hass, area_id="kitchen")
    assert not got.known and got.method == "room"
    assert got.candidates == {ALEX: 0.4, JORDAN: 0.3}


def test_with_several_home_and_no_room_candidates_everyone_is_a_candidate(house, fake_hass):
    idm = house["mod"]
    house["home"] = [ALEX, JORDAN]
    got = idm.quick_identify(fake_hass, area_id="kitchen")
    assert (got.person, got.method) == (idm.UNKNOWN, "ambiguous_home")
    assert got.candidates == {ALEX: 0.15, JORDAN: 0.15}


def test_an_undecided_room_in_an_empty_house_has_no_signal(house, fake_hass):
    idm = house["mod"]
    got = idm.quick_identify(fake_hass, area_id="kitchen")
    assert (got.person, got.method, got.candidates) == (idm.UNKNOWN, "no_signal", {})


def test_without_an_area_the_room_is_never_asked(house, fake_hass):
    house["home"] = [ALEX]
    assert house["mod"].quick_identify(fake_hass).person == ALEX
    assert house["asked"] == []


def test_a_failing_room_lookup_gives_unknown_not_an_error(house, fake_hass):
    house["room"] = RuntimeError("recognition offline")
    house["home"] = [ALEX]
    got = house["mod"].quick_identify(fake_hass, area_id="kitchen")
    assert not got.known and got.method == "no_signal"


def test_switched_off_names_nobody_and_asks_nothing(house, fake_hass):
    house["enabled"] = False
    house["home"] = [ALEX]
    got = house["mod"].quick_identify(fake_hass, area_id="kitchen")
    assert not got.known
    assert got.method == "disabled"
    assert house["asked"] == []
