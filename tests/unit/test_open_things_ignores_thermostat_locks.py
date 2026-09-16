"""Thermostat keypad/child locks (lock.downstairs_thermo_lock,
lock.upstairs_thermo_lock — see cognitive_core.LOCKDOWN_EXEMPT_LOCKS_DEFAULT)
aren't physical security. The formal lockdown sweep already knew to leave
them alone, but briefing._gather_open_things swept every lock.* entity
blind, so Nova would list them as "unlocked" and offer to lock them in
briefings — something nobody actually wants (there's no reason to lock a
thermostat panel). This asserts the briefing gatherer now shares the same
exemption set as lockdown, so it stays consistent if the config-driven
override (`lockdown_exempt_locks`) ever changes.
"""
import pytest


@pytest.fixture
def briefing(load):
    return load("briefing")


def test_thermostat_locks_are_not_reported_as_unlocked(briefing, fake_hass):
    fake_hass.states.set("lock.downstairs_thermo_lock", "unlocked", friendly_name="Downstairs Thermo Lock")
    fake_hass.states.set("lock.upstairs_thermo_lock", "unlocked", friendly_name="Upstairs Thermo Lock")
    fake_hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")

    items = briefing._gather_open_things(fake_hass)

    assert "Front Door is unlocked" in items
    assert not any("Thermo Lock" in item for item in items)


def test_real_locks_still_reported_when_no_thermostat_locks_present(briefing, fake_hass):
    fake_hass.states.set("lock.front_door", "unlocked", friendly_name="Front Door")
    fake_hass.states.set("lock.garage", "locked", friendly_name="Garage")

    items = briefing._gather_open_things(fake_hass)

    assert items == ["Front Door is unlocked"]
