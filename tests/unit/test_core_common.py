"""The shared constants and helpers of the cognitive core.

cognitive_core re-exports every name here as the same object, and the
managers read them from here, so the values and the helper results are the
contract.
"""
import logging
import sys
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def cc(load):
    return load("core_common")


CONSTANTS = {
    "TICK_INTERVAL": 30,
    "LOCKDOWN_CHECK_INTERVAL": 300,
    "ALARM_ARMED_STATES": {"armed_home", "armed_away", "armed_night",
                           "armed_vacation", "armed_custom_bypass"},
    "LOCKDOWN_DOOR_COVER_CLASSES": {"door", "garage", "garage_door"},
    "LOCKDOWN_BREACH_COOLDOWN": 120,
    "LOCKDOWN_SECURE_VERIFY_DELAY": 25,
    "LOCKDOWN_EXEMPT_LOCKS_DEFAULT": set(),
    "LEGACY_LOCKDOWN_EXEMPT_LOCKS": ("lock.downstairs_thermo_lock",
                                     "lock.upstairs_thermo_lock"),
    "FREEZE_WARN_TEMP_F": 35,
    "FREEZE_CRITICAL_TEMP_F": 20,
    "FREEZE_REMINDER_SECONDS": 10800,
    "PROACTIVE_CHECK_INTERVAL": 120,
    "PROACTIVE_OFFER_COOLDOWN": 1800,
    "DARK_LUX_THRESHOLD": 15,
    "STALE_LIGHT_MINUTES": 90,
    "HIGH_TEMP_AWAY_F": 78,
    "LOW_TEMP_AWAY_F": 62,
    "INTRUSION_SPREAD_ZONES": 2,
    "INTRUSION_INWARD_DEPTH": 2,
    "INTRUSION_CLEAR_QUIET_SECS": 180,
    "INTRUSION_MAX_INVESTIGATE_SECS": 600,
    "INTRUSION_SUSTAINED_SECS": 60,
    "INTRUSION_RESPONSE_TIMEOUT_SECS": 120,
}


@pytest.mark.parametrize("name", sorted(CONSTANTS))
def test_each_constant_keeps_its_value(cc, name):
    value = getattr(cc, name)
    assert value == CONSTANTS[name]
    assert type(value) is type(CONSTANTS[name])


def test_the_constants_are_all_there_is(cc):
    public = {n for n, v in vars(cc).items()
              if n.isupper() and not n.startswith("_")}
    assert public == set(CONSTANTS)


def test_the_core_logger_name_and_the_write_patch_point(cc):
    assert cc._LOGGER.name == "jc.cognitive_core"
    assert cc.write_json_atomic.__name__ == "write_json_atomic"
    assert cc.write_json_atomic.__module__.endswith("persistence.files")


def test_the_core_reexports_the_same_objects(load, cc):
    core = load("cognitive_core")
    names = [n for n in CONSTANTS
             if n not in ("LEGACY_LOCKDOWN_EXEMPT_LOCKS", "FREEZE_REMINDER_SECONDS")]
    for name in names + ["_temp_to_f", "_f_to_unit", "_fmt_temp",
                                   "_hass_lang", "_notify_i18n", "_persona",
                                   "_live_honorific", "discover_outdoor_temp",
                                   "write_json_atomic"]:
        assert getattr(core, name) is getattr(cc, name), name


# ── temperature helpers ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value, unit, expected", [
    (0, "°C", 32.0), (100, "°C", 212.0), (-40, "°C", -40.0), (21.5, "°c", 70.7),
    (20, "C", 68.0), (70, "°F", 70), (70, "F", 70), (70, "", 70), (70, None, 70),
    (300, "K", 300)])
def test_a_temperature_is_converted_to_fahrenheit(cc, value, unit, expected):
    assert cc._temp_to_f(value, unit) == pytest.approx(expected)


@pytest.mark.parametrize("value, unit, expected", [
    (32, "°C", 0.0), (212, "°C", 100.0), (-40, "°C", -40.0), (68, "C", 20.0),
    (70, "°F", 70), (70, "", 70), (70, None, 70)])
def test_fahrenheit_is_converted_to_the_homes_unit(cc, value, unit, expected):
    assert cc._f_to_unit(value, unit) == pytest.approx(expected)


def test_the_two_conversions_undo_each_other(cc):
    for celsius in (-12.5, 0, 18.3, 35):
        assert cc._f_to_unit(cc._temp_to_f(celsius, "°C"), "°C") == pytest.approx(celsius)


@pytest.mark.parametrize("args, expected", [
    ((18.34, "°C"), "18.3°C"), ((18.35, "°F"), "18.4°F"), ((18, "°C", 0), "18°C"),
    ((18.3456, "°C", 3), "18.346°C"), ((70, ""), "70.0°"), ((70, None), "70.0°"),
    ((-3.2, "°C"), "-3.2°C")])
def test_a_temperature_is_written_with_its_unit(cc, args, expected):
    assert cc._fmt_temp(*args) == expected


# ── discover_outdoor_temp ────────────────────────────────────────────────────

def _hass(unit="°C", with_units=True):
    hass = FakeHass()
    hass.config = types.SimpleNamespace(
        units=types.SimpleNamespace(temperature_unit=unit)) if with_units \
        else types.SimpleNamespace()
    return hass


def test_a_weather_entity_is_read_first(cc):
    hass = _hass()
    hass.states.set("weather.home", "sunny", temperature=7.5, temperature_unit="°F")
    hass.states.set("sensor.outdoor_temp", "20", device_class="temperature",
                    unit_of_measurement="°C")
    assert cc.discover_outdoor_temp(hass) == (7.5, "°F")


def test_a_weather_entity_without_a_unit_uses_the_homes_unit(cc):
    hass = _hass("°C")
    hass.states.set("weather.home", "sunny", temperature="12")
    assert cc.discover_outdoor_temp(hass) == (12.0, "°C")


def test_the_unit_falls_back_to_fahrenheit_when_the_home_has_none(cc):
    hass = _hass(with_units=False)
    hass.states.set("weather.home", "sunny", temperature=50)
    assert cc.discover_outdoor_temp(hass) == (50.0, "°F")


def test_weather_entities_without_a_usable_temperature_are_skipped(cc):
    hass = _hass()
    hass.states.set("weather.a", "sunny")
    hass.states.set("weather.b", "sunny", temperature="warm")
    hass.states.set("weather.c", "sunny", temperature=None)
    hass.states.set("weather.d", "sunny", temperature=3, temperature_unit="°C")
    assert cc.discover_outdoor_temp(hass) == (3.0, "°C")


@pytest.mark.parametrize("entity, attrs", [
    ("sensor.outdoor_temperature", {}),
    ("sensor.garden_outside", {}),
    ("sensor.thermometer_2", {"friendly_name": "Outdoor Thermometer"}),
    ("sensor.OUTDOOR_x", {})])
def test_an_outdoor_sensor_is_found_by_its_id_or_name(cc, entity, attrs):
    hass = _hass("°F")
    hass.states.set(entity, "4.5", device_class="temperature",
                    unit_of_measurement="°C", **attrs)
    assert cc.discover_outdoor_temp(hass) == (4.5, "°C")


def test_a_sensor_with_no_unit_uses_the_homes_unit(cc):
    hass = _hass("°C")
    hass.states.set("sensor.outdoor", "4", device_class="temperature")
    assert cc.discover_outdoor_temp(hass) == (4.0, "°C")


def test_sensors_that_are_not_outdoor_temperatures_are_ignored(cc):
    hass = _hass()
    hass.states.set("sensor.indoor", "21", device_class="temperature")
    hass.states.set("sensor.outdoor_humidity", "80", device_class="humidity")
    hass.states.set("sensor.outdoor_plain", "9")
    assert cc.discover_outdoor_temp(hass) is None


def test_the_first_outdoor_sensor_decides_even_when_it_has_no_reading(cc):
    hass = _hass()
    hass.states.set("sensor.outdoor_a", "unavailable", device_class="temperature")
    hass.states.set("sensor.outdoor_b", "9", device_class="temperature")
    assert cc.discover_outdoor_temp(hass) is None


def test_nothing_found_gives_none(cc):
    assert cc.discover_outdoor_temp(_hass()) is None


# ── lazily imported helpers ──────────────────────────────────────────────────

def _stub(monkeypatch, name, **attrs):
    mod = types.ModuleType(f"jc.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    return mod


def test_the_language_comes_from_the_output_language_setting(cc, monkeypatch):
    seen = []
    _stub(monkeypatch, "output_language", resolve=lambda hass: seen.append(hass) or "de")
    hass = object()
    assert cc._hass_lang(hass) == "de"
    assert seen == [hass]


def test_the_notification_and_persona_modules_are_loaded_on_first_use(cc, monkeypatch):
    notify = _stub(monkeypatch, "notify_i18n")
    persona = _stub(monkeypatch, "persona")
    assert cc._notify_i18n() is notify
    assert cc._persona() is persona


def test_the_honorific_is_asked_for_afresh_every_time(cc, monkeypatch):
    names = iter(["madam", "dr"])
    _stub(monkeypatch, "honorific", effective_honorific=lambda hass: next(names))
    assert cc._live_honorific(object()) == "madam"
    assert cc._live_honorific(object()) == "dr"


def test_the_honorific_falls_back_to_sir_on_any_failure(cc, monkeypatch):
    def boom(hass):
        raise RuntimeError("no presence data")
    _stub(monkeypatch, "honorific", effective_honorific=boom)
    assert cc._live_honorific(object()) == "sir"


def test_the_honorific_falls_back_to_sir_when_the_module_will_not_load(cc, monkeypatch):
    monkeypatch.setitem(sys.modules, "jc.honorific", None)
    assert cc._live_honorific(object()) == "sir"
