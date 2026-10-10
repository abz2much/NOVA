"""Thermostat keypad/child locks (lock.downstairs_thermo_lock,
lock.upstairs_thermo_lock, saved in lockdown_exempt_locks)
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


def test_thermostat_locks_are_not_reported_as_unlocked(briefing, load, fake_hass, monkeypatch, tmp_path):
    # 8.28.0: the exempt list comes from the saved setting (as after the one
    # time migration), never from a built in default.
    cc = load("cognitive_core")
    monkeypatch.setattr(cc, "LOCKDOWN_STATE_PATH", str(tmp_path / "lockdown.json"))
    monkeypatch.setattr(cc._CORE, "lockdown_mgr", cc.LockdownManager(fake_hass, {
        "lockdown_exempt_locks": ["lock.downstairs_thermo_lock", "lock.upstairs_thermo_lock"]}))
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
