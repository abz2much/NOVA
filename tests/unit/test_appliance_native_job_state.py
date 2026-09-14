"""Tests for native-appliance job_state completion detection in appliance_monitor.py.

Regression guard: LG ThinQ dryers report job_state "finished" on completion, but
washers/dishwashers report "finish" (no "ed") -- the done-state set only ever
matched "finished", so washer/dishwasher completion was never detected via
job_state at all (a live-caught bug, Sept 2026).
"""
import pytest


@pytest.fixture
def am(load):
    return load("appliance_monitor")


def test_discovers_dryer_job_state_as_native(am, fake_hass):
    fake_hass.states.set("sensor.dryer_job_state", "drying",
                          device_class="enum", friendly_name="Dryer Job state")
    natives = am._discover_native_appliances(fake_hass)
    assert "sensor.dryer_job_state" in natives
    assert "finished" in natives["sensor.dryer_job_state"].trigger_states


def test_washer_finish_spelling_is_a_recognised_done_state(am, fake_hass):
    fake_hass.states.set("sensor.washer_job_state", "wash",
                          device_class="enum", friendly_name="Washer Job state")
    natives = am._discover_native_appliances(fake_hass)
    trigger_states = natives["sensor.washer_job_state"].trigger_states
    assert "finish" in trigger_states


def test_dishwasher_finish_transition_is_detected_as_complete(am, fake_hass):
    fake_hass.states.set("sensor.dishwasher_job_state", "wash",
                          device_class="enum", friendly_name="Dishwasher Job state")
    natives = am._discover_native_appliances(fake_hass)
    native = natives["sensor.dishwasher_job_state"]
    assert native.last_state == "wash"
    assert "finish" in native.trigger_states


def test_dishwasher_is_not_misclassified_as_washer(am, fake_hass):
    """"washer" is a substring of "dishwasher" and a key in its own right —
    the classifier must not let the shorter match win."""
    fake_hass.states.set("sensor.dishwasher_job_state", "wash",
                          device_class="enum", friendly_name="Dishwasher Job state")
    natives = am._discover_native_appliances(fake_hass)
    assert natives["sensor.dishwasher_job_state"].appliance == am.ApplianceType.DISHWASHER
    assert am._type_to_appliance("dishwasher") == am.ApplianceType.DISHWASHER
