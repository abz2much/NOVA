"""The empty house security briefing counts only real openings (8.49.39).

Before, any state_changed event whose new state read "on" (door, window,
garage door) or "unlocked" counted, so an open door that only updated its
attributes, or a sensor coming back after a restart, could add up to a
false security briefing while everyone was away.
"""
from types import SimpleNamespace

import pytest


def _state(value, device_class=None):
    attrs = {"device_class": device_class} if device_class else {}
    return SimpleNamespace(state=value, attributes=attrs)


def _event(entity_id, new, old):
    return SimpleNamespace(data={"entity_id": entity_id, "new_state": new,
                                 "old_state": old})


@pytest.fixture
def pb(load, monkeypatch, fake_hass):
    mod = load("proactive_briefing")
    mod._STATE.hass = fake_hass
    mod._STATE.running = True
    mod._STATE.security_events = []
    mod._STATE.last_briefing_time = 0.0
    mod._STATE.pending_arrival_person = ""
    monkeypatch.setattr(mod, "_configured_front_door", lambda: "")
    monkeypatch.setattr(mod, "_house_maybe_occupied", lambda hass: False)
    fired = []

    async def _fake_trigger(reason, person_name="", person_entity=""):
        fired.append(reason)
    monkeypatch.setattr(mod, "_trigger_briefing", _fake_trigger)
    mod._fired = fired
    return mod


async def _feed(pb, fake_hass, events):
    for ev in events:
        pb._on_state_changed(ev)
    await fake_hass.drain()
    return pb._fired


async def test_attribute_updates_of_an_open_door_are_not_openings(pb, fake_hass):
    door = "binary_sensor.back_door"
    events = [_event(door, _state("on", "door"), _state("on", "door"))] * 3
    assert await _feed(pb, fake_hass, events) == []
    assert pb._STATE.security_events == []


async def test_sensors_returning_after_a_restart_are_not_openings(pb, fake_hass):
    events = [
        _event("binary_sensor.window_a", _state("on", "window"), _state("unavailable")),
        _event("binary_sensor.window_b", _state("on", "window"), _state("unknown")),
        _event("binary_sensor.garage", _state("on", "garage_door"), None),
        _event("lock.side", _state("unlocked"), _state("unavailable")),
    ]
    assert await _feed(pb, fake_hass, events) == []


async def test_a_lock_staying_unlocked_is_not_an_opening(pb, fake_hass):
    events = [_event("lock.side", _state("unlocked"), _state("unlocked"))] * 3
    assert await _feed(pb, fake_hass, events) == []


async def test_three_real_openings_still_start_a_security_briefing(pb, fake_hass):
    events = [
        _event("binary_sensor.back_door", _state("on", "door"), _state("off", "door")),
        _event("binary_sensor.window_a", _state("on", "window"), _state("off", "window")),
        _event("lock.side", _state("unlocked"), _state("locked")),
    ]
    assert await _feed(pb, fake_hass, events) == ["security"]
    assert pb._STATE.security_events == []


async def test_openings_with_someone_home_are_ignored(pb, fake_hass, monkeypatch):
    monkeypatch.setattr(pb, "_house_maybe_occupied", lambda hass: True)
    events = [_event("binary_sensor.back_door", _state("on", "door"),
                     _state("off", "door"))] * 3
    assert await _feed(pb, fake_hass, events) == []


async def test_other_sensor_classes_are_not_openings(pb, fake_hass):
    events = [_event("binary_sensor.hall_motion", _state("on", "motion"),
                     _state("off", "motion"))] * 3
    assert await _feed(pb, fake_hass, events) == []
