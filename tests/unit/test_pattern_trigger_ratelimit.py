"""Motion and occupancy as learned triggers, held in check by the rate gate.

These sensors can be opted in to pattern learning, but they pulse hard: a
garage motion sensor can fire hundreds of times a day. The gate lets one
change per entity through each interval, with a much longer interval for the
fast pulsing classes, so a storm leaves a handful of periodic markers instead
of flooding the store. Ordinary devices keep the shorter base interval.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def gate(load, monkeypatch):
    """cognitive_core with an empty rate memory and an empty config, both
    put back after the test."""
    core = load("cognitive_core")
    monkeypatch.setattr(load("core_learning"), "_PATTERN_LOG_LAST", {})
    monkeypatch.setattr(core._CORE, "config", {})
    return core


@pytest.fixture
def toggles(load, monkeypatch):
    """The panel's pattern settings as a plain dict the test fills in."""
    settings: dict = {}
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: settings.get(key, default))
    return settings


def _let_through(gate, entity_id, start, step, pulses, device_class=""):
    """The times at which a run of evenly spaced pulses was logged."""
    times = (start + n * step for n in range(pulses))
    return [t for t in times if gate._pattern_rate_ok(entity_id, t, device_class)]


def test_an_hour_long_motion_storm_leaves_one_marker_per_five_minutes(gate, monkeypatch):
    monkeypatch.setattr(gate._CORE, "config", {"pattern_motion_min_interval": 300})
    # A pulse every 3 s for an hour: 1200 pulses.
    logged = _let_through(gate, "binary_sensor.garage_motion", 1000.0, 3.0, 1200, "motion")
    assert logged == [1000.0 + 300.0 * k for k in range(12)]
    # Bounded, yet still periodic rather than silent.
    assert 10 <= len(logged) <= 13


def test_a_toggling_light_uses_the_base_minute(gate, monkeypatch):
    monkeypatch.setattr(gate._CORE, "config", {"pattern_log_min_interval": 60})
    # Six minutes of a light flipping every 3 s.
    logged = _let_through(gate, "light.kitchen", 2000.0, 3.0, 120)
    assert logged == [2000.0 + 60.0 * k for k in range(6)]


def test_a_zero_base_interval_switches_the_gate_off_for_ordinary_devices(gate, monkeypatch):
    monkeypatch.setattr(gate._CORE, "config", {"pattern_log_min_interval": 0})
    assert len(_let_through(gate, "light.kitchen", 3000.0, 1.0, 50)) == 50


@pytest.mark.parametrize("device_class,interval", [
    ("motion", 300), ("occupancy", 300), ("presence", 300), ("moving", 300),
    ("vibration", 300), ("sound", 300),
    ("", 60), ("door", 60), ("window", 60), ("temperature", 60),
])
def test_which_classes_get_the_long_interval(gate, monkeypatch, device_class, interval):
    monkeypatch.setattr(gate._CORE, "config", {"pattern_log_min_interval": 60,
                                               "pattern_motion_min_interval": 300})
    assert gate._pattern_log_interval(device_class) == interval


def test_each_entity_has_its_own_clock(gate, monkeypatch):
    monkeypatch.setattr(gate._CORE, "config", {"pattern_motion_min_interval": 300})
    # Two sensors at the same instant both log, so cross-room sequences survive.
    assert gate._pattern_rate_ok("binary_sensor.hall_motion", 5000.0, "motion") is True
    assert gate._pattern_rate_ok("binary_sensor.den_motion", 5000.0, "motion") is True
    # The same sensor again ten seconds later is dropped...
    assert gate._pattern_rate_ok("binary_sensor.hall_motion", 5010.0, "motion") is False
    # ...and a dropped pulse does not restart its interval.
    assert gate._pattern_rate_ok("binary_sensor.hall_motion", 5300.0, "motion") is True
    assert gate._pattern_rate_ok("binary_sensor.den_motion", 5299.0, "motion") is False


@pytest.mark.parametrize("entity_id,device_class", [
    ("binary_sensor.hall_motion", "motion"),
    ("binary_sensor.den", "occupancy"),
    ("binary_sensor.study", "presence"),
    ("binary_sensor.washer", "moving"),
])
def test_motion_classes_are_learned_only_once_opted_in(gate, toggles, entity_id, device_class):
    assert gate._pattern_opted_in(entity_id, device_class) is False      # off by default
    toggles["pattern_learn_motion"] = True
    assert gate._pattern_opted_in(entity_id, device_class) is True


def test_the_motion_toggle_does_not_reach_other_classes(gate, toggles):
    toggles["pattern_learn_motion"] = True
    assert gate._pattern_opted_in("binary_sensor.temp", "temperature") is False
    assert gate._pattern_opted_in("binary_sensor.front_door", "door") is False
    assert gate._pattern_opted_in("sensor.hall_motion_count", "motion") is False


def test_the_door_and_motion_toggles_work_independently(gate, toggles):
    """Since 8.49.37, turning on doors no longer hides motion sensors."""
    toggles.update(pattern_learn_doors=True, pattern_learn_motion=True)
    assert gate._pattern_opted_in("binary_sensor.front_door", "door") is True
    assert gate._pattern_opted_in("binary_sensor.hall_motion", "motion") is True
    assert gate._pattern_opted_in("binary_sensor.temp", "temperature") is False

    toggles["pattern_learn_doors"] = False
    assert gate._pattern_opted_in("binary_sensor.front_door", "door") is False
    assert gate._pattern_opted_in("binary_sensor.hall_motion", "motion") is True

    toggles.update(pattern_learn_doors=True, pattern_learn_motion=False)
    assert gate._pattern_opted_in("binary_sensor.front_door", "door") is True
    assert gate._pattern_opted_in("binary_sensor.hall_motion", "motion") is False
