"""Acceptance tests for vision/spatial.py, written from its behaviour spec."""
import importlib.util
import pathlib

import pytest

from fakes import FakeHass

COMP = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
AREA = "study"
COUNT = f"sensor.{AREA}_frigate_person_count"
GAZE = f"binary_sensor.{AREA}_camera_gaze_detected"
MMW = f"binary_sensor.{AREA}_mmwave_presence"


@pytest.fixture
def sp(load):
    return load("vision.spatial")


@pytest.fixture
def hass():
    return FakeHass()


def run(sp, hass, area=AREA):
    return sp.SpatialContextEngine(hass).evaluate(area)


def test_constants(sp):
    E = sp.SpatialContextEngine
    assert (E.PERSON_COUNT_WEIGHT, E.GAZE_WEIGHT, E.PRESENCE_WEIGHT) == (0.60, 0.20, 0.35)
    assert (E.CONFIDENCE_MIN, E.CONFIDENCE_MAX) == (0.0, 1.0)


def test_package_reexport(load, sp):
    pkg = load("vision")
    assert pkg.SpatialContextEngine is sp.SpatialContextEngine
    assert "SpatialContextEngine" in pkg.__all__


def test_standalone_load(hass):
    spec = importlib.util.spec_from_file_location("spec_spatial_standalone", COMP / "vision" / "spatial.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.SpatialContextEngine(hass).evaluate("x")["confidence"] == 0.0


def test_hass_attribute(sp, hass):
    assert sp.SpatialContextEngine(hass).hass is hass


def test_no_signals(sp, hass):
    assert run(sp, hass) == {"confidence": 0.0, "skip_preamble": False,
                             "person_count": 0, "gaze": False, "presence": False}


@pytest.mark.parametrize("count,gaze,mmw,conf,skip", [
    ("1", "off", "off", 0.6, False),
    ("0", "on", "off", 0.2, False),
    ("0", "off", "on", 0.35, False),
    ("2", "on", "off", 0.8, False),
    ("3", "off", "on", 0.95, False),
    ("0", "on", "on", 0.55, True),
    ("1", "on", "on", 1.0, True),
    ("7", "off", "off", 0.6, False),
])
def test_fusion_table(sp, hass, count, gaze, mmw, conf, skip):
    hass.states.set(COUNT, count)
    hass.states.set(GAZE, gaze)
    hass.states.set(MMW, mmw)
    r = run(sp, hass)
    assert r["confidence"] == conf
    assert r["skip_preamble"] is skip
    assert set(r) == {"confidence", "skip_preamble", "person_count", "gaze", "presence"}


@pytest.mark.parametrize("raw,expected", [
    ("3", 3), ("2.8", 2), ("0.9", 0), ("-4", 0), ("unknown", 0), ("unavailable", 0),
    ("", 0), ("nan", 0), (5, 5), (None, 0)])
def test_person_count_parsing(sp, hass, raw, expected):
    hass.states.set(COUNT, raw)
    r = run(sp, hass)
    assert r["person_count"] == expected and type(r["person_count"]) is int
    assert r["confidence"] == (0.6 if expected > 0 else 0.0)


def test_infinite_count_raises_quirk(sp, hass):
    hass.states.set(COUNT, "inf")
    with pytest.raises(OverflowError):
        run(sp, hass)


@pytest.mark.parametrize("state", ["on", "ON", "true", "True", "detected", "home", "occupied"])
def test_on_states(sp, hass, state):
    hass.states.set(GAZE, state)
    hass.states.set(MMW, state)
    r = run(sp, hass)
    assert r["gaze"] is True and r["presence"] is True and r["skip_preamble"] is True


@pytest.mark.parametrize("state", ["off", "false", "clear", "away", "unavailable", "unknown", "", "1", False])
def test_off_states(sp, hass, state):
    hass.states.set(GAZE, state)
    hass.states.set(MMW, state)
    r = run(sp, hass)
    assert r["gaze"] is False and r["presence"] is False and r["skip_preamble"] is False


def test_bool_true_state_reads_as_true(sp, hass):
    # str(True).lower() == "true"
    hass.states.set(GAZE, True)
    assert run(sp, hass)["gaze"] is True


def test_skip_preamble_ignores_person_count(sp, hass):
    hass.states.set(COUNT, "0")
    hass.states.set(GAZE, "on")
    hass.states.set(MMW, "occupied")
    assert run(sp, hass)["skip_preamble"] is True


def test_other_area_ignored(sp, hass):
    hass.states.set("sensor.garage_frigate_person_count", "2")
    hass.states.set("binary_sensor.garage_camera_gaze_detected", "on")
    hass.states.set("binary_sensor.garage_mmwave_presence", "on")
    assert run(sp, hass)["confidence"] == 0.0
    assert run(sp, hass, "garage")["confidence"] == 1.0


def test_reads_fresh_each_call(sp, hass):
    eng = sp.SpatialContextEngine(hass)
    assert eng.evaluate(AREA)["presence"] is False
    hass.states.set(MMW, "on")
    assert eng.evaluate(AREA)["presence"] is True


def test_weight_override_is_honoured(sp, hass):
    class Heavy(sp.SpatialContextEngine):
        GAZE_WEIGHT = 0.5
    hass.states.set(GAZE, "on")
    assert Heavy(hass).evaluate(AREA)["confidence"] == 0.5
    eng = sp.SpatialContextEngine(hass)
    eng.GAZE_WEIGHT = 2.0
    assert eng.evaluate(AREA)["confidence"] == 1.0


def test_negative_override_clamped(sp, hass):
    eng = sp.SpatialContextEngine(hass)
    eng.PRESENCE_WEIGHT = -0.5
    hass.states.set(MMW, "on")
    assert eng.evaluate(AREA)["confidence"] == 0.0
