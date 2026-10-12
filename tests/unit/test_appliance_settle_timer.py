"""A cycle ends even when the power sensor stops reporting (8.39.1).

Many smart plugs only report when the reading changes. A washer that drops to
0 W and stays there sent one reading, so the settle wait was never checked
again and the cycle was never announced. A timer now re-checks every running
appliance that is waiting to settle.
"""
import time as _time

import pytest


@pytest.fixture
def am(load, fake_hass):
    mod = load("appliance_monitor")
    mod._MON.hass = fake_hass
    mod._MON.sensors.clear()
    mod._MON.running = True
    yield mod
    mod._MON.sensors.clear()
    mod._MON.running = False


def _running_washer(am):
    sensor = am._SensorState(
        entity_id="sensor.washer_power", friendly_name="Washer",
        appliance=am.ApplianceType.WASHER, discovery_method="declared_entity",
        peak_power=500.0)
    a = sensor.appliance
    sensor.run_start = _time.time() - (a.sustained_seconds + 5)
    am._process_reading(sensor, 500.0)
    assert sensor.phase == "running"
    am._MON.sensors[sensor.entity_id] = sensor
    return sensor


def test_the_settle_check_ends_a_cycle_with_no_new_reading(am, monkeypatch):
    sensor = _running_washer(am)
    assert am._process_reading(sensor, 1.0) is None       # the one and only low reading
    done = []

    async def _nothing():
        return None

    def announce(s, label):
        done.append(label)
        return _nothing()
    monkeypatch.setattr(am, "_announce_done", announce)
    am._settle_tick()                                       # not settled yet
    assert sensor.phase == "running"

    sensor.settle_start = _time.time() - (sensor.appliance.settle_seconds + 5)
    am._settle_tick()
    am._MON.hass.close_pending()
    assert sensor.phase == "idle" and done == [sensor.appliance.label]


def test_the_settle_check_leaves_a_still_running_appliance_alone(am, monkeypatch):
    sensor = _running_washer(am)
    monkeypatch.setattr(am, "_announce_done", lambda *a: pytest.fail("announced"))
    am._settle_tick()
    assert sensor.phase == "running" and sensor.settle_start == 0.0


def test_the_settle_check_does_nothing_once_stopped(am):
    sensor = _running_washer(am)
    am._process_reading(sensor, 1.0)
    sensor.settle_start = _time.time() - (sensor.appliance.settle_seconds + 5)
    am._MON.running = False
    am._settle_tick()
    assert sensor.phase == "running"
