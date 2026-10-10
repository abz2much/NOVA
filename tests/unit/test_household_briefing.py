"""The scheduled briefing's presence check uses household.everyone_away
(8.23.0): skipped only when every person reads away. All state is fake."""
import pytest


@pytest.fixture
def hh(load):
    return load("household")


@pytest.mark.parametrize("people,skipped", [
    ({"person.abi": "home"}, False),
    ({"person.abi": "home", "person.rachel": "not_home"}, False),
    ({"person.abi": "not_home"}, True),
    ({"person.abi": "not_home", "person.rachel": "away"}, True),
    ({"person.abi": "Work"}, True),                       # another zone is away
    ({"person.abi": "unknown"}, False),
    ({"person.abi": "not_home", "person.rachel": "unavailable"}, False),
    ({}, False),                                          # nobody set up: fail open
])
def test_the_briefing_is_skipped_only_when_everyone_is_away(hh, fake_hass, people, skipped):
    for eid, state in people.items():
        fake_hass.states.set(eid, state)
    assert hh.everyone_away(fake_hass) is skipped


def test_a_linked_phone_reading_home_keeps_the_briefing(hh, fake_hass):
    fake_hass.states.set("person.abi", "not_home", device_trackers=["device_tracker.abi_phone"])
    fake_hass.states.set("device_tracker.abi_phone", "home")
    assert hh.everyone_away(fake_hass) is False


def test_loose_trackers_never_skip_the_briefing(hh, fake_hass):
    fake_hass.states.set("device_tracker.car", "not_home")
    assert hh.everyone_away(fake_hass) is False
