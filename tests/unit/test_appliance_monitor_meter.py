"""Tests for whole-home meter discovery in appliance_monitor.py.

Regression guard: EcoFlow PowerOcean names its whole-home consumption sensor
"Home Consumption" (not "Home Energy" or any other previously recognised
phrase), so a correctly-tagged sensor (device_class=power, unit=W) was never
picked up here either -- appliance-cycle detection had no whole-home baseline
to delta against, on top of the same bug in energy.py's fallback search.
"""
import pytest


@pytest.fixture
def am(load):
    return load("appliance_monitor")


def test_discovers_powerocean_home_consumption(am, fake_hass):
    fake_hass.states.set("sensor.solar_inverter_housepower", "512.3",
                          unit_of_measurement="W", device_class="power",
                          friendly_name="Home Consumption")
    tracker = am._discover_whole_home_meter(fake_hass)
    assert tracker is not None
    assert tracker.entity_id == "sensor.solar_inverter_housepower"
    assert tracker.baseline_w == pytest.approx(512.3)


def test_prefers_entry_without_circuit_suffix(am, fake_hass):
    fake_hass.states.set("sensor.electric_consumption_1", "300",
                          unit_of_measurement="W", device_class="power",
                          friendly_name="Electric Consumption (1)")
    fake_hass.states.set("sensor.electric_consumption_main", "900",
                          unit_of_measurement="W", device_class="power",
                          friendly_name="Electric Consumption")
    tracker = am._discover_whole_home_meter(fake_hass)
    assert tracker.entity_id == "sensor.electric_consumption_main"


def test_no_candidates_returns_none(am, fake_hass):
    fake_hass.states.set("sensor.washer_power", "1200",
                          unit_of_measurement="W", device_class="power",
                          friendly_name="Washer Power")
    assert am._discover_whole_home_meter(fake_hass) is None
