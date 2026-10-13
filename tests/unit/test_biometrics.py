"""Wearable readings as context (biometrics.py): off by default, how sensors
are recognised, the sleep signal and the descriptive snapshot."""
import pytest

from fakes import FakeHass


@pytest.fixture
def bio(load, monkeypatch):
    mod = load("biometrics")
    settings = {"biometrics_enabled": True}
    monkeypatch.setattr(mod, "_cfg", lambda k, d=None: settings.get(k, d))
    mod.settings = settings
    return mod


def hass_with(*states):
    hass = FakeHass()
    for eid, value, attrs in states:
        hass.states.set(eid, value, **attrs)
    return hass


def test_cfg_reads_settings(load, monkeypatch):
    mod = load("biometrics")
    cfg = load("nova_config")
    monkeypatch.setattr(cfg, "get", lambda k, d=None: None)
    assert mod._cfg("biometrics_enabled", False) is False
    assert mod._cfg("x") is None
    monkeypatch.setattr(cfg, "get", lambda k, d=None: True)
    assert mod.is_enabled() is True

    def boom(k, d=None):
        raise RuntimeError
    monkeypatch.setattr(cfg, "get", boom)
    assert mod.is_enabled() is False


def test_off_unless_turned_on(bio):
    bio.settings.clear()
    assert bio.is_enabled() is False
    hass = hass_with(("sensor.watch_heart_rate", "60", {}), ("binary_sensor.asleep", "on", {}))
    assert bio.sleep_signal(hass) is None
    assert bio.wellbeing_context(hass) == {"available": False, "summary": "", "readings": {}}
    bio.settings["biometrics_enabled"] = "yes"
    assert bio.is_enabled() is True


# recognising sensors

@pytest.mark.parametrize("eid,name,unit,kind", [
    ("sensor.watch_heartrate", "", "", "heart_rate"),
    ("sensor.x", "Heart Rate", "", "heart_rate"),
    ("sensor.watch_hr", "", "", "heart_rate"),
    ("sensor.pulse_ox", "", "", "heart_rate"),
    ("sensor.x", "", "BPM", "heart_rate"),
    ("sensor.resting_heart_rate", "", "", "heart_rate"),
    ("sensor.rest_heart", "", "", "resting_hr"),
    ("sensor.ring_sleep_stage", "", "", "sleep_stage"),
    ("sensor.x", "Sleep Phase", "", "sleep_stage"),
    ("binary_sensor.is_asleep", "", "", "sleep_state"),
    ("binary_sensor.bed", "In_Bed", "", "sleep_state"),
    ("sensor.blood_oxygen", "", "", "spo2"),
    ("sensor.battery_level", "", "%", "spo2"),
    ("sensor.daily", "", "steps", "steps"),
    ("sensor.step_count", "", "", "steps"),
    ("sensor.breathing_rate", "", "", "respiratory"),
    ("sensor.x", "", "br", "respiratory"),
    ("sensor.stress_level", "", "", "stress"),
    ("sensor.skin_temp", "", "°C", "temperature"),
    ("sensor.outdoor_temperature", "Outdoor", "°C", None),
    ("sensor.power", "", "W", None),
])
def test_classify(bio, eid, name, unit, kind):
    assert bio._classify(eid, name, unit, "") == kind


def test_none_unit_is_fine(bio):
    assert bio._classify("sensor.power", "", None, "") is None


def test_own_mapping_wins(bio):
    bio.settings["biometric_entities"] = {"stress": "sensor.watch_heart_rate",
                                          "steps": "sensor.watch_heart_rate"}
    assert bio._classify("sensor.watch_heart_rate", "", "bpm", "") == "stress"
    bio.settings["biometric_entities"] = ["sensor.watch_heart_rate"]
    assert bio._classify("sensor.watch_heart_rate", "", "", "") == "heart_rate"


# discovery

def test_discover_groups_by_kind(bio):
    hass = hass_with(
        ("sensor.watch_heart_rate", "61", {"unit_of_measurement": "bpm", "friendly_name": "Watch HR"}),
        ("sensor.kitchen_power", "100", {"unit_of_measurement": "W"}),
        ("binary_sensor.asleep", "off", {}),
        ("sensor.band_heart_rate", "64", {}),
        ("light.lamp_sleep", "on", {}),
    )
    assert bio.discover(hass) == {
        "heart_rate": [
            {"entity": "sensor.watch_heart_rate", "value": "61", "unit": "bpm", "name": "Watch HR"},
            {"entity": "sensor.band_heart_rate", "value": "64", "unit": "", "name": ""}],
        "sleep_state": [{"entity": "binary_sensor.asleep", "value": "off", "unit": "", "name": ""}],
    }


def test_discover_never_raises(bio):
    class Broken:
        def async_all(self, domain):
            raise RuntimeError("not ready")
    hass = FakeHass()
    hass.states = Broken()
    assert bio.discover(hass) == {}


# sleep signal

@pytest.mark.parametrize("value,expected", [
    ("Deep", True), ("REM", True), ("light sleep", True), ("asleep", True),
    ("on", True), ("in_bed", True), ("not_asleep", True), ("monitoring", True),
    ("awake", False), ("off", False), ("out_of_bed", False),
    ("unknown", None), ("unavailable", None), ("", None), ("restless", None)])
def test_sleep_signal_values(bio, value, expected):
    assert bio.sleep_signal(hass_with(("sensor.sleep_stage", value, {}))) is expected


def test_stage_beats_state_and_unknowns_are_skipped(bio):
    hass = hass_with(("binary_sensor.is_asleep", "on", {}),
                     ("sensor.sleep_stage", "unknown", {}),
                     ("sensor.ring_sleep_stage", "awake", {}))
    assert bio.sleep_signal(hass) is False


def test_no_sleep_sensor_no_signal(bio):
    assert bio.sleep_signal(hass_with(("sensor.watch_heart_rate", "60", {}))) is None


# snapshot

def test_snapshot_with_nothing_found(bio):
    assert bio.wellbeing_context(FakeHass()) == {
        "available": False,
        "summary": "no biometric entities found — connect a wearable integration to Home Assistant",
        "readings": {}}


def test_snapshot_summary_and_readings(bio):
    hass = hass_with(
        ("sensor.watch_heart_rate", "unavailable", {"unit_of_measurement": "bpm"}),
        ("sensor.band_heart_rate", " 58 ", {"unit_of_measurement": "bpm"}),
        ("binary_sensor.is_asleep", "off", {}),
        ("sensor.ring_sleep_stage", "light", {}),
        ("sensor.step_count", "4200", {"unit_of_measurement": "steps"}),
        ("sensor.skin_temp", "unknown", {}),
    )
    assert bio.wellbeing_context(hass) == {
        "available": True,
        "summary": "heart rate 58 bpm; sleep: light; 4200 steps today",
        "readings": {"heart_rate": {"value": "58", "unit": "bpm"},
                     "sleep_state": {"value": "off", "unit": ""},
                     "sleep_stage": {"value": "light", "unit": ""},
                     "steps": {"value": "4200", "unit": "steps"}},
        "disclaimer": "context only — not medical advice or assessment"}


def test_snapshot_sleep_state_when_no_stage(bio):
    out = bio.wellbeing_context(hass_with(("binary_sensor.is_asleep", "on", {})))
    assert out["summary"] == "sleep state: on"


def test_snapshot_with_only_other_kinds(bio):
    out = bio.wellbeing_context(hass_with(("sensor.stress_level", "30", {})))
    assert out["summary"] == "biometric data present"
    assert out["readings"] == {"stress": {"value": "30", "unit": ""}}
