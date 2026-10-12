"""Tests for the briefing presence gate (household.everyone_away).

Scheduled briefings must fire unless the house is CONFIDENTLY empty; uncertain
presence must not suppress them (fail open).
"""
import pytest


@pytest.fixture
def hh(load):
    return load("household")


def test_all_away_is_true(hh, fake_hass):
    fake_hass.states.set("person.alex", "not_home")
    fake_hass.states.set("person.morgan", "away")
    assert hh.everyone_away(fake_hass) is True


def test_someone_home_is_false(hh, fake_hass):
    fake_hass.states.set("person.alex", "home")
    fake_hass.states.set("person.morgan", "not_home")
    assert hh.everyone_away(fake_hass) is False


def test_unknown_presence_is_false(hh, fake_hass):
    # unknown presence must never suppress a briefing: fail open
    fake_hass.states.set("person.alex", "unknown")
    assert hh.everyone_away(fake_hass) is False


def test_no_people_is_false(hh, fake_hass):
    assert hh.everyone_away(fake_hass) is False
