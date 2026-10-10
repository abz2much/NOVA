"""Regression tests for the InfrastructureTriage health aggregator.

Loaded standalone (stdlib-only) and driven by the harness FakeHass. Pins the
thresholds, the offline-vs-unreadable distinction, multi-fault ordering, and the
shape of the returned verdict.

8.28.0: the audit watches only the sensors the user listed. It used to check
one particular home's server, switch and freeze sensor ids, with a power
monitor root cause for that switch; those ids and that root cause are gone.
"""
import importlib.util
import pathlib
import sys

import pytest

from fakes import FakeHass  # provided on sys.path by tests/conftest.py

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


def _load_standalone(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, COMP / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


monitor = _load_standalone("nova_monitor", "diagnostics/monitor.py")


DISK = "sensor.disk_use"
MEMORY = "sensor.memory_use"
SWITCH = "binary_sensor.network_switch"
LINK = "binary_sensor.freezer_link"
LISTED = [DISK, MEMORY, SWITCH, LINK]

_HEALTHY = {
    DISK: ("55", {"unit_of_measurement": "%", "friendly_name": "Disk use"}),
    MEMORY: ("60", {"unit_of_measurement": "%", "friendly_name": "Memory use"}),
    SWITCH: ("on", {"device_class": "connectivity", "friendly_name": "Network switch"}),
    LINK: ("on", {"device_class": "connectivity", "friendly_name": "Freezer link"}),
}


def _verdict(states: dict, sensors=None) -> dict:
    hass = FakeHass()
    for eid, (state, attrs) in states.items():
        hass.states.set(eid, state, **attrs)
    return monitor.InfrastructureTriage(
        hass, honorific="sir", sensors=LISTED if sensors is None else sensors).evaluate()


def _with(eid, state):
    return {**_HEALTHY, eid: (state, _HEALTHY[eid][1])}


# ── nothing listed: the audit does nothing ──────────────────────────────────

def test_nothing_listed_does_nothing_even_with_faults():
    faulty = {**_with(DISK, "99"), SWITCH: ("off", _HEALTHY[SWITCH][1])}
    for sensors in ([], None):
        hass = FakeHass()
        for eid, (state, attrs) in faulty.items():
            hass.states.set(eid, state, **attrs)
        v = monitor.InfrastructureTriage(hass, honorific="sir", sensors=sensors).evaluate()
        assert v == {"alert_required": False, "message": "", "critical": False, "tags": []}


def test_the_old_built_in_ids_are_not_checked():
    v = _verdict({"sensor.server_root_storage_usage": ("99", {"unit_of_measurement": "%"}),
                  "binary_sensor.core_switch_status": ("off", {})}, sensors=[])
    assert v["alert_required"] is False


def test_only_listed_sensors_are_checked():
    v = _verdict({**_with(DISK, "99"), "sensor.other_disk": ("99", {"unit_of_measurement": "%"})},
                 sensors=[MEMORY])
    assert v["alert_required"] is False


# ── a listed set is checked ─────────────────────────────────────────────────

def test_all_healthy_is_silent():
    assert _verdict(_HEALTHY) == {
        "alert_required": False, "message": "", "critical": False, "tags": [],
    }


def test_verdict_carries_finding_tags():
    v = _verdict({**_with(DISK, "97"), LINK: ("off", _HEALTHY[LINK][1])})
    assert "Disk use" in v["tags"] and "Freezer link" in v["tags"]


def test_percent_warning_band():
    v = _verdict(_with(DISK, "92"))
    assert v["alert_required"] is True
    assert v["critical"] is False   # 92 is > warn(90) but not > critical(96)
    assert "disk use is elevated at 92 percent" in v["message"].lower()


def test_percent_critical_band():
    v = _verdict(_with(MEMORY, "97"))
    assert v["critical"] is True
    assert "critically" in v["message"].lower()


def test_binary_sensor_off_is_critical():
    v = _verdict(_with(SWITCH, "off"))
    assert v["critical"] is True
    assert "network switch is reporting offline" in v["message"].lower()


def test_a_problem_sensor_is_a_fault_when_on():
    states = {"binary_sensor.ups": ("on", {"device_class": "problem", "friendly_name": "UPS"})}
    v = _verdict(states, sensors=["binary_sensor.ups"])
    assert v["critical"] is True and "ups is reporting a problem" in v["message"].lower()
    states = {"binary_sensor.ups": ("off", {"device_class": "problem", "friendly_name": "UPS"})}
    assert _verdict(states, sensors=["binary_sensor.ups"])["alert_required"] is False


def test_absent_sensor_is_skipped():
    states = dict(_HEALTHY)
    del states[DISK]
    del states[SWITCH]
    v = _verdict(states)
    assert v["alert_required"] is False
    assert v["message"] == ""


def test_unknown_percent_sensor_that_exists_is_a_warning():
    v = _verdict(_with(DISK, "unknown"))
    assert v["alert_required"] is True
    assert v["critical"] is False
    assert "can't read" in v["message"].lower()


def test_unavailable_binary_is_a_warning():
    v = _verdict(_with(SWITCH, "unavailable"))
    assert v["alert_required"] is True
    assert v["critical"] is False   # offline-visibility, not a confirmed fault


def test_non_numeric_percent_is_handled():
    v = _verdict(_with(DISK, "n/a"))
    assert v["alert_required"] is True
    assert v["critical"] is False


def test_a_listed_sensor_that_is_not_a_percentage_only_warns_when_unreadable():
    states = {"sensor.uptime": ("12", {"friendly_name": "Uptime"})}
    assert _verdict(states, sensors=["sensor.uptime"])["alert_required"] is False
    states = {"sensor.uptime": ("unavailable", {"friendly_name": "Uptime"})}
    v = _verdict(states, sensors=["sensor.uptime"])
    assert v["alert_required"] is True and v["critical"] is False


def test_multiple_faults_lead_with_critical():
    v = _verdict({**_with(DISK, "97"), LINK: ("off", _HEALTHY[LINK][1]),
                  MEMORY: ("50", _HEALTHY[MEMORY][1])})
    assert v["critical"] is True
    assert "disk use" in v["message"].lower()
    assert "freezer link" in v["message"].lower()
    assert v["message"].count(".") >= 1


def test_a_sensor_listed_twice_is_checked_once():
    v = _verdict(_with(DISK, "97"), sensors=[DISK, DISK])
    assert v["message"].lower().count("disk use") == 1
