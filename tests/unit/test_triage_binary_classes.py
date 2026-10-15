"""The audit reads a fault the right way round for each sensor class (8.49.41).

Every listed binary sensor except a "problem" one was faulted when "off".
For an update, battery, tamper, smoke or similar sensor "off" is the normal
reading, so a healthy one was reported as critically offline.
"""
import importlib.util
import pathlib
import sys

import pytest

from fakes import FakeHass

_PATH = (pathlib.Path(__file__).resolve().parents[2]
         / "custom_components" / "nova" / "diagnostics" / "monitor.py")


@pytest.fixture(scope="module")
def monitor():
    spec = importlib.util.spec_from_file_location("nova_monitor_classes", _PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _audit(monitor, state, device_class):
    hass = FakeHass()
    eid = "binary_sensor.server_check"
    hass.states.set(eid, state, device_class=device_class, friendly_name="Server check")
    return monitor.InfrastructureTriage(hass, sensors=[eid]).evaluate()


@pytest.mark.parametrize("device_class", [
    "update", "battery", "tamper", "safety", "smoke", "gas", "carbon_monoxide",
    "moisture", "heat", "cold", "problem",
])
def test_off_is_healthy_and_on_is_the_fault(monitor, device_class):
    assert _audit(monitor, "off", device_class)["alert_required"] is False
    on = _audit(monitor, "on", device_class)
    assert on["critical"] is True and "reporting a problem" in on["message"]


@pytest.mark.parametrize("device_class", ["connectivity", "running", "power", "plug", None])
def test_off_is_still_the_fault_where_on_is_good(monitor, device_class):
    assert _audit(monitor, "on", device_class)["alert_required"] is False
    off = _audit(monitor, "off", device_class)
    assert off["critical"] is True and "reporting offline" in off["message"]
