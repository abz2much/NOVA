"""The 8.8.0 hazard settings: validated on write, shown in the panel data."""
from __future__ import annotations

import sys
import types

import pytest

from test_pin_ws_update_config import _Conn, _entry, _hass, _stub_ws_api, _update


@pytest.fixture
def ws(load, monkeypatch):
    # A fresh websocket module under pass-through decorators. monkeypatch
    # puts back whatever jc.websocket was before, so tests that run later
    # and expect it loaded are unaffected.
    _stub_ws_api(monkeypatch)
    monkeypatch.delitem(sys.modules, "jc.websocket", raising=False)
    monkeypatch.setattr(sys.modules["jc"], "websocket", None, raising=False)
    mod = load("websocket")
    monkeypatch.setattr(mod, "_get_knowledge_stats", lambda: {"facts": 0})
    return mod


@pytest.fixture
def store(load, monkeypatch):
    nc = load("nova_config")
    data: dict = {}
    calls = []
    monkeypatch.setattr(nc, "set", lambda k, v: calls.append((k, v)) or data.__setitem__(k, v) or True)
    monkeypatch.setattr(nc, "set_many", lambda values: calls.append(("set_many", dict(values)))
                        or data.update(values) or True)
    monkeypatch.setattr(nc, "delete", lambda k: calls.append(("delete", k)) or data.pop(k, None))
    monkeypatch.setattr(nc, "get", lambda k, d=None: data.get(k, d))
    return types.SimpleNamespace(data=data, calls=calls)


@pytest.mark.parametrize("key", ["hazard_push_level", "hazard_speak_level"])
@pytest.mark.parametrize("value", ["yellow", "orange", "red"])
async def test_a_level_saves(ws, load, store, key, value):
    conn = await _update(ws, _hass(_entry(load)), key, value)
    assert conn.errors == [] and store.data[key] == value


@pytest.mark.parametrize("key", ["hazard_push_level", "hazard_speak_level"])
@pytest.mark.parametrize("value", ["green", "Yellow", "", None, 1, True])
async def test_a_level_must_be_yellow_orange_or_red(ws, load, store, key, value):
    conn = await _update(ws, _hass(_entry(load)), key, value)
    assert conn.errors == [(1, "invalid_value", f"Key '{key}' must be one of: yellow, orange, red")]
    assert store.calls == []


@pytest.mark.parametrize("value", ["yellow", "orange", "red", "off"])
async def test_the_night_speak_level_saves(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), "hazard_night_speak_level", value)
    assert conn.errors == [] and store.data["hazard_night_speak_level"] == value


@pytest.mark.parametrize("value", ["green", "Off", "", None, 1, True])
async def test_the_night_speak_level_refuses_other_values(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), "hazard_night_speak_level", value)
    assert conn.errors == [(1, "invalid_value", "Key 'hazard_night_speak_level' must be "
                                                "one of: yellow, orange, red, off")]
    assert store.calls == []


@pytest.mark.parametrize("value", ["met_eireann", "us", "custom"])
async def test_a_hazard_source_saves(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), "hazard_source", value)
    assert conn.errors == [] and store.data["hazard_source"] == value


@pytest.mark.parametrize("value", ["europe", "nws", "", None, 1, True])
async def test_a_hazard_source_refuses_other_values(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), "hazard_source", value)
    assert conn.errors == [(1, "invalid_value", "Key 'hazard_source' must be one of: "
                                                "met_eireann, us, custom")]
    assert store.calls == []


@pytest.mark.parametrize(("key", "value"), [
    ("hazard_lat", -90), ("hazard_lat", 90), ("hazard_lat", 53.6),
    ("hazard_lon", -180), ("hazard_lon", 180), ("hazard_lon", -6.2),
])
async def test_hazard_coordinates_accept_only_numbers_in_range(ws, load, store, key, value):
    hass = _hass(_entry(load))
    hass.config = types.SimpleNamespace(latitude=51.478, longitude=-0.001,
                                        country="IE", time_zone="Europe/Dublin")
    conn = await _update(ws, hass, key, value)
    assert conn.errors == []


@pytest.mark.parametrize(("key", "value", "message"), [
    ("hazard_lat", -90.01, "Key 'hazard_lat' must be a number from -90 to 90, or empty to use home coordinates"),
    ("hazard_lat", 90.01, "Key 'hazard_lat' must be a number from -90 to 90, or empty to use home coordinates"),
    ("hazard_lon", -180.01, "Key 'hazard_lon' must be a number from -180 to 180, or empty to use home coordinates"),
    ("hazard_lon", 180.01, "Key 'hazard_lon' must be a number from -180 to 180, or empty to use home coordinates"),
])
@pytest.mark.parametrize("bad_type", [False, "number", "53.6"])
async def test_bad_hazard_coordinates_are_refused(
        ws, load, store, key, value, message, bad_type):
    for bad in (value, bad_type):
        conn = await _update(ws, _hass(_entry(load)), key, bad)
        assert conn.errors == [(1, "invalid_value", message)]
    assert store.calls == []


async def test_home_equivalent_coordinates_are_deleted_not_saved(ws, load, store):
    entry = _entry(load, runtime_config={"hazard_lat": 50.0, "hazard_lon": 1.0})
    hass = _hass(entry)
    hass.config = types.SimpleNamespace(latitude=51.478, longitude=-0.001,
                                        country="IE", time_zone="Europe/Dublin")
    store.data.update(hazard_lat=50.0, hazard_lon=1.0)
    await _update(ws, hass, "hazard_lat", 51.478)
    conn = await _update(ws, hass, "hazard_lon", -0.001)
    assert conn.errors == []
    assert "hazard_lat" not in store.data and "hazard_lon" not in store.data
    assert "hazard_lat" not in entry.runtime_data.runtime_config
    assert "hazard_lon" not in entry.runtime_data.runtime_config


async def test_a_different_location_saves_both_coordinates_and_clear_deletes_both(
        ws, load, store):
    entry = _entry(load)
    hass = _hass(entry)
    hass.config = types.SimpleNamespace(latitude=51.478, longitude=-0.001,
                                        country="IE", time_zone="Europe/Dublin")
    await _update(ws, hass, "hazard_lat", 52.66)
    assert store.data == {"hazard_lat": 52.66, "hazard_lon": -0.001}
    await _update(ws, hass, "hazard_lon", -8.63)
    assert store.data == {"hazard_lat": 52.66, "hazard_lon": -8.63}
    await _update(ws, hass, "hazard_lon", "")
    assert store.data == {}


async def test_one_coordinate_is_refused_when_home_has_no_other_coordinate(
        ws, load, store):
    entry = _entry(load)
    hass = _hass(entry)
    hass.config = types.SimpleNamespace(latitude=None, longitude=None,
                                        country=None, time_zone="UTC")
    conn = await _update(ws, hass, "hazard_lat", 52.66)
    assert conn.errors == [(1, "invalid_value",
                            "Both hazard coordinates are required when Home Assistant "
                            "has no home location")]
    assert store.calls == [] and entry.runtime_data.runtime_config == {}


@pytest.mark.parametrize("value", ['["EI07"]', '["EI14", "EI24"]', "[]"])
async def test_counties_in_the_table_save(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), "hazard_counties", value)
    assert conn.errors == [] and store.data["hazard_counties"] == value


@pytest.mark.parametrize("value", ['["EI08"]', '["EI07", "EI819"]', '["Dublin"]', '"EI07"',
                                   "EI07", '[7]', "", None, '{"EI07": 1}'])
async def test_counties_must_be_a_list_of_table_codes(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), "hazard_counties", value)
    assert conn.errors[0][1] == "invalid_value"
    assert "Met Éireann county codes" in conn.errors[0][2] and store.calls == []


@pytest.mark.parametrize("value", ["", "https://alerts.example.org/cap/index.xml",
                                   "https://192.168.1.20/feed.xml"])
async def test_a_cap_url_saves(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), "hazard_cap_url", value)
    assert conn.errors == [] and store.data["hazard_cap_url"] == value


@pytest.mark.parametrize("value", [
    "http://alerts.example.org/feed.xml",               # https only
    "https://user:pw@alerts.example.org/feed.xml",      # credentials in the URL
    "https://169.254.169.254/latest/meta-data",         # cloud metadata
    "https://metadata.google.internal/x",
    "https://[fe80::1]/x",                              # link local
    "ftp://alerts.example.org/x", "not a url", "https://", 5, None,
])
async def test_a_cap_url_must_be_a_safe_https_address(ws, load, store, value):
    conn = await _update(ws, _hass(_entry(load)), "hazard_cap_url", value)
    assert conn.errors[0][1] == "invalid_value" and store.calls == []


async def test_a_masked_cap_url_is_refused(ws, load, store):
    conn = await _update(ws, _hass(_entry(load)), "hazard_cap_url",
                         "https://**REDACTED**@alerts.example.org/feed.xml")
    assert conn.errors == [(1, "invalid_value", "This field shows a hidden password. "
                                                "Type the full address to change it.")]


@pytest.mark.parametrize("key", ["hazard_cap_area_codes", "hazard_cap_area_names"])
async def test_area_lists(ws, load, store, key):
    ok = await _update(ws, _hass(_entry(load)), key, '["IE061", "Fingal"]')
    assert ok.errors == []
    for bad in ('["", "x"]', '[1]', "IE061", '"x"', None):
        conn = await _update(ws, _hass(_entry(load)), key, bad)
        assert conn.errors[0][:2] == (1, "invalid_value"), bad


@pytest.mark.parametrize("key", ["hazard_met_eireann_on", "hazard_cap_on"])
async def test_the_new_switches_need_a_real_boolean(ws, load, store, key):
    assert (await _update(ws, _hass(_entry(load)), key, "false")).errors[0][1] == "invalid_value"
    assert (await _update(ws, _hass(_entry(load)), key, True)).errors == []


# ── panel data ──────────────────────────────────────────────────────────────

async def test_panel_data_has_the_settings_with_the_url_masked(ws, load, store):
    real = "https://nova:hunter2@alerts.example.org/feed.xml"
    entry = _entry(load, runtime_config={
        "hazard_cap_url": real, "hazard_counties": '["EI07"]', "hazard_cap_on": True,
        "hazard_push_level": "orange", "hazard_cap_area_names": '["Fingal"]'})
    conn = _Conn()
    await ws.ws_get_panel_data(_hass(entry), conn, {"id": 1})
    cfg = conn.results[0][1]["config"]
    assert cfg["hazard_cap_url"] == "https://**REDACTED**@alerts.example.org/feed.xml"
    assert cfg["hazard_counties"] == ["EI07"] and cfg["hazard_cap_on"] is True
    assert cfg["hazard_push_level"] == "orange" and cfg["hazard_speak_level"] == "orange"
    assert cfg["hazard_night_speak_level"] == "red"
    assert cfg["hazard_cap_area_names"] == ["Fingal"] and cfg["hazard_cap_area_codes"] == []
    assert cfg["hazard_source"] == "custom"


@pytest.mark.parametrize("source", ["met_eireann", "us", "custom"])
async def test_panel_data_loads_each_saved_hazard_source(ws, load, store, source):
    entry = _entry(load, runtime_config={"hazard_source": source})
    conn = _Conn()
    await ws.ws_get_panel_data(_hass(entry), conn, {"id": 1})
    assert conn.results[0][1]["config"]["hazard_source"] == source


async def test_panel_data_shows_the_region_defaults(ws, load, store):
    entry = _entry(load)
    hass = _hass(entry)
    hass.config = types.SimpleNamespace(country="IE", latitude=53.35, longitude=-6.26,
                                        time_zone="Europe/Dublin", language="en")
    conn = _Conn()
    await ws.ws_get_panel_data(hass, conn, {"id": 1})
    cfg = conn.results[0][1]["config"]
    assert cfg["hazard_met_eireann_on"] is True
    assert cfg["hazard_source"] == "met_eireann"
    assert (cfg["hazard_lat"], cfg["hazard_lon"]) == (53.35, -6.26)
    assert (cfg["hazard_quakes_on"], cfg["hazard_weather_on"], cfg["hazard_disasters_on"]) == (
        False, False, False)
    hass.config.country = "US"
    conn = _Conn()
    await ws.ws_get_panel_data(hass, conn, {"id": 1})
    cfg = conn.results[0][1]["config"]
    assert cfg["hazard_met_eireann_on"] is False and cfg["hazard_quakes_on"] is True
    assert cfg["hazard_source"] == "us"


async def test_old_config_without_night_level_loads_with_red_default(ws, load, store):
    conn = _Conn()
    await ws.ws_get_panel_data(_hass(_entry(load, runtime_config={})), conn, {"id": 1})
    assert conn.results[0][1]["config"]["hazard_night_speak_level"] == "red"
