"""Regression: Nova announced "dishwasher finished" on every Home Assistant
restart, even with nothing new happening — caught live, Sept 2026.

Root cause: a native appliance's `last_state` is seeded from whatever
hass.states.get() returns at monitor-start time. If the underlying device's
state hasn't been restored yet, that's "unavailable"/"unknown"/"" — and if it
HAD already restored, it could just as easily be a "finish" retained from
hours before the restart. Either way, the first real state_changed event this
native appliance receives afterward isn't evidence of a fresh completion, but
_on_state_changed treated any old→trigger-state transition as one.
"""
import pytest


@pytest.fixture
def am(load):
    return load("appliance_monitor")


@pytest.fixture
def native_dishwasher(am, fake_hass):
    am._MON.hass = fake_hass
    am._MON.running = True
    native = am._NativeAppliance(
        entity_id="sensor.dishwasher_job_state",
        device_name="Dishwasher",
        appliance=am.ApplianceType.DISHWASHER,
        trigger_states=frozenset({"finish", "finished"}),
    )
    am._MON.natives = {"sensor.dishwasher_job_state": native}
    return native


def _event(am, entity_id, old, new):
    from homeassistant.core import State
    old_state = State(entity_id, old) if old is not None else None
    new_state = State(entity_id, new)
    return am.Event("state_changed", {
        "entity_id": entity_id, "old_state": old_state, "new_state": new_state,
    })


def test_first_event_landing_on_finish_from_unavailable_does_not_announce(am, fake_hass, native_dishwasher):
    """Device hadn't reported at discovery time (last_state seeded ""), then
    its first real event already shows "finish" — must not announce."""
    native_dishwasher.last_state = ""
    ev = _event(am, "sensor.dishwasher_job_state", None, "finish")
    am._on_state_changed(ev)
    assert fake_hass._tasks == []
    assert native_dishwasher.announced is True  # seeded, so a later real cycle still fires


def test_first_event_from_stale_unavailable_does_not_announce(am, fake_hass, native_dishwasher):
    native_dishwasher.last_state = "unavailable"
    ev = _event(am, "sensor.dishwasher_job_state", "unavailable", "finish")
    am._on_state_changed(ev)
    assert fake_hass._tasks == []


def test_a_real_completion_after_the_seeded_first_event_still_announces(am, fake_hass, native_dishwasher):
    # First event after restart: already sitting at "finish" (stale) — absorbed.
    native_dishwasher.last_state = ""
    am._on_state_changed(_event(am, "sensor.dishwasher_job_state", None, "finish"))
    assert fake_hass._tasks == []

    # A fresh cycle starts (leaves the done state) — resets `announced`.
    am._on_state_changed(_event(am, "sensor.dishwasher_job_state", "finish", "wash"))
    assert fake_hass._tasks == []

    # ...and genuinely finishes — this one must announce.
    am._on_state_changed(_event(am, "sensor.dishwasher_job_state", "wash", "finish"))
    assert len(fake_hass._tasks) == 1
    fake_hass.close_pending()


def test_genuine_completion_with_no_startup_race_still_announces(am, fake_hass, native_dishwasher):
    """When the native was already seeded (e.g. discovery caught a real
    'wash' state, not a startup gap), a real finish must announce normally —
    the fix must not suppress everything forever."""
    native_dishwasher.last_state = "wash"
    native_dishwasher.seeded = True  # already past the startup-race window
    am._on_state_changed(_event(am, "sensor.dishwasher_job_state", "wash", "finish"))
    assert len(fake_hass._tasks) == 1
    fake_hass.close_pending()
