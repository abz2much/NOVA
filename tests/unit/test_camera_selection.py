"""Which cameras Nova may use (v7.0.0): every camera, a chosen few, or none.

active_camera_states and active_cameras walk Home Assistant's camera domain
and leave out whatever Settings, Cameras has switched off. The switched-off
set is replaced per test so these cases do not depend on saved options.
"""
import sys
import types

import pytest

from fakes import FakeHass

ALL_THREE = ("camera.a", "camera.b", "camera.c")


@pytest.fixture
def cam(load, monkeypatch):
    # camera.py imports aiohttp at the top; the unit environment may not have
    # it, and nothing here reaches the network, so an empty module will do.
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    return load("camera")


@pytest.fixture
def switched_off(cam, monkeypatch):
    """Call with the set of cameras Settings has turned off."""
    def apply(ids):
        monkeypatch.setattr(cam, "_disabled_cameras", lambda: set(ids))
    return apply


def _home(*entity_ids):
    hass = FakeHass()
    for eid in entity_ids:
        hass.states.set(eid, "idle")
    return hass


@pytest.mark.parametrize("off, expected", [
    ((), ["camera.a", "camera.b", "camera.c"]),            # nothing off: all
    (("camera.b",), ["camera.a", "camera.c"]),             # some off
    (ALL_THREE, []),                                        # every one off
    (("camera.gone",), ["camera.a", "camera.b", "camera.c"]),  # stale entry
])
def test_selection(cam, switched_off, off, expected):
    switched_off(off)
    assert cam.active_cameras(_home(*ALL_THREE)) == expected


def test_states_are_the_real_state_objects(cam, switched_off):
    switched_off({"camera.b"})
    hass = _home("camera.a", "camera.b")
    picked = cam.active_camera_states(hass)
    assert [st.entity_id for st in picked] == ["camera.a"]
    assert picked[0] is hass.states.get("camera.a")


def test_other_domains_are_never_offered(cam, switched_off):
    switched_off(())
    hass = _home("camera.a", "sensor.camera_b", "binary_sensor.camera_c")
    assert cam.active_cameras(hass) == ["camera.a"]


def test_home_order_is_kept(cam, switched_off):
    switched_off({"camera.m"})
    assert cam.active_cameras(_home("camera.z", "camera.m", "camera.a")) == [
        "camera.z", "camera.a"]


def test_no_cameras_at_all(cam, switched_off):
    switched_off(())
    assert cam.active_cameras(_home()) == []


def test_bad_setting_means_no_cameras_rather_than_a_crash(cam, monkeypatch):
    def broken():
        raise RuntimeError("bad config")
    monkeypatch.setattr(cam, "_disabled_cameras", broken)
    assert cam.active_camera_states(_home("camera.a")) == []
    assert cam.active_cameras(_home("camera.a")) == []


def test_missing_hass_gives_nothing(cam, switched_off):
    switched_off(())
    assert cam.active_camera_states(None) == []
