"""Presence-aware, per-person honorifics (honorific.py).

Nova used to address everyone with one global honorific regardless of who
was actually home. New behavior: exactly one person home -> their own
configured honorific (falling back to the global default); nobody home,
or more than one person home -> no honorific at all, since there's no one
specific person to address (and a broadcast heard by everyone can't
correctly guess whose preference to use).
"""
import sys
import types

import pytest


@pytest.fixture
def hon(load):
    return load("honorific")


def _set_nova_config(hon, monkeypatch, mapping):
    pkg = hon.__name__.rsplit(".", 1)[0]
    fake = types.SimpleNamespace(get=lambda k, d=None: mapping.get(k, d))
    monkeypatch.setitem(sys.modules, f"{pkg}.nova_config", fake)


def test_all_people_lists_every_person_entity_with_id_and_name(hon, fake_hass):
    fake_hass.states.set("person.abi", "home", friendly_name="Abi")
    fake_hass.states.set("person.rachel", "not_home", friendly_name="Rachel")
    people = hon.all_people(fake_hass)
    assert {"entity_id": "person.abi", "name": "Abi"} in people
    assert {"entity_id": "person.rachel", "name": "Rachel"} in people
    assert len(people) == 2


def test_home_alone_entity_id_when_exactly_one_person_home(hon, fake_hass):
    fake_hass.states.set("person.abi", "home", friendly_name="Abi")
    fake_hass.states.set("person.rachel", "not_home", friendly_name="Rachel")
    assert hon.home_alone_entity_id(fake_hass) == "person.abi"


def test_home_alone_entity_id_none_when_nobody_home(hon, fake_hass):
    fake_hass.states.set("person.abi", "not_home", friendly_name="Abi")
    fake_hass.states.set("person.rachel", "away", friendly_name="Rachel")
    assert hon.home_alone_entity_id(fake_hass) is None


def test_home_alone_entity_id_none_when_multiple_people_home(hon, fake_hass):
    fake_hass.states.set("person.abi", "home", friendly_name="Abi")
    fake_hass.states.set("person.rachel", "home", friendly_name="Rachel")
    assert hon.home_alone_entity_id(fake_hass) is None


def test_effective_honorific_uses_solo_persons_own_setting(hon, fake_hass, monkeypatch):
    fake_hass.states.set("person.rachel", "home", friendly_name="Rachel")
    _set_nova_config(hon, monkeypatch, {
        "person_honorifics": {"person.rachel": "ma'am"},
        "honorific": "sir",
    })
    assert hon.effective_honorific(fake_hass) == "ma'am"


def test_effective_honorific_falls_back_to_global_default_when_unset(hon, fake_hass, monkeypatch):
    fake_hass.states.set("person.jianna", "home", friendly_name="Jianna")
    _set_nova_config(hon, monkeypatch, {
        "person_honorifics": {"person.rachel": "ma'am"},  # someone else's, not Jianna's
        "honorific": "boss",
    })
    assert hon.effective_honorific(fake_hass) == "boss"


def test_effective_honorific_empty_when_multiple_people_home(hon, fake_hass, monkeypatch):
    fake_hass.states.set("person.abi", "home", friendly_name="Abi")
    fake_hass.states.set("person.rachel", "home", friendly_name="Rachel")
    _set_nova_config(hon, monkeypatch, {
        "person_honorifics": {"person.abi": "sir", "person.rachel": "ma'am"},
        "honorific": "sir",
    })
    assert hon.effective_honorific(fake_hass) == ""


def test_effective_honorific_empty_when_nobody_home(hon, fake_hass, monkeypatch):
    fake_hass.states.set("person.abi", "not_home", friendly_name="Abi")
    _set_nova_config(hon, monkeypatch, {"honorific": "sir"})
    assert hon.effective_honorific(fake_hass) == ""
