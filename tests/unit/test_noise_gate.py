"""Appliance noise taken off the room reading (audio/noise_gate.py)."""
import importlib.util
import logging
import pathlib
import sys

import pytest

from fakes import FakeHass

_FILE = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "audio" / "noise_gate.py"


def _module():
    spec = importlib.util.spec_from_file_location("noise_gate_under_test", _FILE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["noise_gate_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


NG = _module()


def gate(power: dict, profiles=None, **kw):
    hass = FakeHass()
    for eid, watts in power.items():
        hass.states.set(eid, watts)
    return NG.NoiseGate(hass, profiles, **kw)


def test_default_profiles_and_threshold():
    assert NG.DEFAULT_POWER_ON_W == 10.0
    assert NG.DEFAULT_PROFILES == {
        "sensor.dishwasher_power": 8.0, "sensor.washer_power": 7.0,
        "sensor.dryer_power": 9.0, "sensor.microwave_power": 11.0,
        "sensor.sump_pump_power": 9.0, "sensor.dehumidifier_power": 5.0}


def test_attributes():
    g = gate({}, power_on_threshold=25.0)
    assert g.profiles is NG.DEFAULT_PROFILES and g.threshold == 25.0
    own = {}
    assert gate({}, own).profiles is own


def test_quiet_house_leaves_reading_untouched():
    g = gate({"sensor.washer_power": "0"})
    assert g.running_appliances() == [] and g.attenuation() == 0.0
    assert g.compensated_db(52.5) == 52.5


def test_one_running_appliance_is_taken_off():
    g = gate({"sensor.dryer_power": "2100"})
    assert g.running_appliances() == ["sensor.dryer_power"]
    assert g.compensated_db(50.0) == 41.0


def test_loudest_running_appliance_sets_the_cut():
    g = gate({"sensor.washer_power": "400", "sensor.microwave_power": "900",
              "sensor.dehumidifier_power": "200"})
    assert g.running_appliances() == ["sensor.washer_power", "sensor.microwave_power",
                                      "sensor.dehumidifier_power"]
    assert g.attenuation() == 11.0
    assert g.compensated_db(60.0) == 49.0


def test_threshold_is_strictly_above():
    assert gate({"sensor.washer_power": "10"}).running_appliances() == []
    assert gate({"sensor.washer_power": "10.01"}).running_appliances() == ["sensor.washer_power"]
    g = gate({"sensor.washer_power": "60"}, power_on_threshold=60)
    assert g.running_appliances() == []


@pytest.mark.parametrize("state", ["unavailable", "unknown", "", None, True])
def test_unreadable_power_is_not_running(state):
    assert gate({"sensor.washer_power": state}).running_appliances() == []


def test_result_never_below_zero():
    assert gate({"sensor.microwave_power": "1000"}).compensated_db(3.0) == 0.0


def test_unknown_reading_stays_unknown():
    assert gate({"sensor.microwave_power": "1000"}).compensated_db(None) is None


def test_custom_profiles_replace_defaults():
    g = gate({"sensor.kettle_power": "2000", "sensor.dryer_power": "2000"},
             {"sensor.kettle_power": 6.0})
    assert g.running_appliances() == ["sensor.kettle_power"]
    assert g.compensated_db(40.0) == 34.0


def test_adjustment_is_logged_at_debug(caplog):
    caplog.set_level(logging.DEBUG)
    gate({"sensor.dryer_power": "500"}).compensated_db(50.0)
    assert any(r.levelno == logging.DEBUG for r in caplog.records)
