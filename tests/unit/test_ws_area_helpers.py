"""Characterisation tests for the panel's area, satellite and camera helpers
(ws_area_helpers.py).

These helpers build the room cards and setup lists in nova/get_panel_data.
Several had almost no coverage (2% to 25%), so these tests pin what they do,
including their quirks, so that moving them out of websocket.py could not
change anything. The Home Assistant registries and the recorder are replaced
by small fakes.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest
from fakes import FakeHass


class _Area(types.SimpleNamespace):
    pass


@pytest.fixture
def registries(monkeypatch):
    """Fake entity, device and area registries, installed where
    `from homeassistant.helpers import ... as er` finds them."""
    reg = types.SimpleNamespace(entities={}, devices={}, areas={}, fail=False)

    def _need(hass):
        if reg.fail:
            raise RuntimeError("registry unavailable")

    er = types.ModuleType("homeassistant.helpers.entity_registry")
    er.async_get = lambda hass: (_need(hass), types.SimpleNamespace(
        entities=reg.entities, async_get=lambda eid: reg.entities.get(eid)))[1]
    dr = types.ModuleType("homeassistant.helpers.device_registry")
    dr.async_get = lambda hass: (_need(hass), types.SimpleNamespace(
        async_get=lambda did: reg.devices.get(did)))[1]
    ar = types.ModuleType("homeassistant.helpers.area_registry")
    ar.async_get = lambda hass: (_need(hass), types.SimpleNamespace(
        async_get_area=lambda aid: reg.areas.get(aid),
        async_list_areas=lambda: list(reg.areas.values())))[1]
    helpers = sys.modules.get("homeassistant.helpers") or types.ModuleType("homeassistant.helpers")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    for name, mod in (("entity_registry", er), ("device_registry", dr), ("area_registry", ar)):
        monkeypatch.setitem(sys.modules, f"homeassistant.helpers.{name}", mod)
        monkeypatch.setattr(helpers, name, mod, raising=False)

    def add_entity(eid, area_id=None, device_id=None):
        reg.entities[eid] = types.SimpleNamespace(
            entity_id=eid, area_id=area_id, device_id=device_id)

    reg.add_entity = add_entity
    return reg


@pytest.fixture
def mod(load, registries):
    load("audio_routing")            # imports the registries at module level
    return load("ws_area_helpers")


@pytest.fixture
def hass():
    return FakeHass()


# ── _area_name / _is_outdoor_area ────────────────────────────────────────────

def test_area_name_uses_the_registry_name(mod, registries, hass):
    registries.areas["kit"] = _Area(id="kit", name="Kitchen")
    assert mod._area_name(hass, "kit") == "Kitchen"


def test_area_name_falls_back_to_the_id(mod, registries, hass):
    registries.areas["nameless"] = _Area(id="nameless", name=None)
    assert mod._area_name(hass, "nameless") == "nameless"      # no name
    assert mod._area_name(hass, "unknown") == "unknown"        # no such area
    registries.fail = True
    assert mod._area_name(hass, "kit") == "kit"                # registry raises


@pytest.mark.parametrize("name,outdoor", [
    ("Back Garden", True), ("Front Yard", True), ("Driveway", True),
    ("Patio", True), ("Deck", True), ("Porch", True), ("Pool", True),
    ("Outdoor Kitchen", True), ("Outside", True), ("Exterior", True),
    ("Lawn", True), ("Kitchen", False), ("Living Room", False),
])
def test_outdoor_area_is_decided_by_name_words(mod, registries, hass, name, outdoor):
    registries.areas["a"] = _Area(id="a", name=name)
    assert mod._is_outdoor_area(hass, "a") is outdoor


def test_outdoor_area_looks_at_the_id_when_the_area_has_no_name(mod, hass):
    assert mod._is_outdoor_area(hass, "back_garden") is True
    assert mod._is_outdoor_area(hass, "hall") is False


# ── _entities_in_area ────────────────────────────────────────────────────────

def test_entities_in_area_by_entity_area_and_by_device_area(mod, registries, hass):
    registries.devices["dev1"] = types.SimpleNamespace(area_id="kit")
    registries.devices["dev2"] = types.SimpleNamespace(area_id="hall")
    registries.add_entity("light.a", area_id="kit")
    registries.add_entity("light.b", device_id="dev1")            # via its device
    registries.add_entity("light.c", area_id="hall", device_id="dev1")  # own area wins
    registries.add_entity("light.d", device_id="dev2")
    registries.add_entity("light.e")                              # no area at all
    registries.add_entity("light.f", device_id="missing")         # unknown device
    assert mod._entities_in_area(hass, "kit") == ["light.a", "light.b"]
    assert mod._entities_in_area(hass, "hall") == ["light.c", "light.d"]
    assert mod._entities_in_area(hass, "nowhere") == []


def test_entities_in_area_skips_user_excluded_entities(mod, registries, hass, load, monkeypatch):
    registries.add_entity("light.a", area_id="kit")
    registries.add_entity("light.b", area_id="kit")
    monkeypatch.setattr(load("entity_filter"), "is_excluded",
                        lambda h, eid: eid == "light.b")
    assert mod._entities_in_area(hass, "kit") == ["light.a"]


def test_entities_in_area_keeps_everything_if_the_filter_cannot_load(
        mod, registries, hass, monkeypatch):
    registries.add_entity("light.a", area_id="kit")
    broken = types.ModuleType("jc.entity_filter")          # no is_excluded
    monkeypatch.setitem(sys.modules, "jc.entity_filter", broken)
    assert mod._entities_in_area(hass, "kit") == ["light.a"]


# ── _area_capabilities ───────────────────────────────────────────────────────

def _fill(hass, registries, area, items):
    for eid, state, attrs in items:
        registries.add_entity(eid, area_id=area)
        hass.states.set(eid, state, **attrs)


def test_area_capabilities_by_domain_and_device_class(mod, registries, hass):
    _fill(hass, registries, "a", [
        ("assist_satellite.s", "idle", {}),
        ("media_player.m", "idle", {}),
        ("camera.c", "idle", {}),
        ("binary_sensor.occ", "on", {"device_class": "occupancy"}),
        ("binary_sensor.door", "off", {"device_class": "door"}),
        ("binary_sensor.leak", "off", {"device_class": "moisture"}),
        ("binary_sensor.smoke", "off", {"device_class": "smoke"}),
        ("light.l", "on", {}),
        ("switch.s", "on", {}),
        ("lock.k", "locked", {}),
        ("climate.t", "heat", {}),
        ("sensor.temp", "20", {"device_class": "temperature"}),   # no capability
    ])
    assert mod._area_capabilities(hass, "a") == [
        "sat", "spkr", "mmwave", "cam", "light", "switch", "lock", "climate",
        "door", "leak", "alarm"]


@pytest.mark.parametrize("dclass,code", [
    ("occupancy", "mmwave"), ("motion", "mmwave"), ("presence", "mmwave"),
    ("door", "door"), ("window", "door"), ("garage_door", "door"), ("opening", "door"),
    ("moisture", "leak"),
    ("smoke", "alarm"), ("gas", "alarm"), ("carbon_monoxide", "alarm"),
    ("safety", "alarm"), ("tamper", "alarm"), ("problem", "alarm"),
])
def test_binary_sensor_device_classes(mod, registries, hass, dclass, code):
    _fill(hass, registries, "a", [("binary_sensor.x", "on", {"device_class": dclass})])
    assert mod._area_capabilities(hass, "a") == [code]


def test_binary_sensor_with_another_class_adds_nothing(mod, registries, hass):
    _fill(hass, registries, "a", [("binary_sensor.x", "on", {"device_class": "battery"})])
    assert mod._area_capabilities(hass, "a") == []


def test_entity_without_a_state_still_counts_by_domain(mod, registries, hass):
    registries.add_entity("light.ghost", area_id="a")           # no state set
    registries.add_entity("binary_sensor.ghost", area_id="a")   # no state: no class
    assert mod._area_capabilities(hass, "a") == ["light"]


def test_area_capabilities_empty_area(mod, hass):
    assert mod._area_capabilities(hass, "empty") == []


# ── _area_light_state / _area_temp_humidity_entities ─────────────────────────

def test_area_light_state_counts_lights_with_a_state(mod, registries, hass):
    _fill(hass, registries, "a", [
        ("light.on1", "on", {}), ("light.on2", "on", {}), ("light.off", "off", {}),
        ("switch.not_a_light", "on", {}),
    ])
    registries.add_entity("light.nostate", area_id="a")
    assert mod._area_light_state(hass, "a") == (2, 3)
    assert mod._area_light_state(hass, "empty") == (0, 0)


def test_first_temperature_and_humidity_sensor_win(mod, registries, hass):
    _fill(hass, registries, "a", [
        ("light.l", "on", {}),
        ("sensor.t1", "20", {"device_class": "temperature"}),
        ("sensor.h1", "40", {"device_class": "humidity"}),
        ("sensor.t2", "21", {"device_class": "temperature"}),
        ("sensor.other", "5", {"device_class": "power"}),
    ])
    assert mod._area_temp_humidity_entities(hass, "a") == ("sensor.t1", "sensor.h1")


def test_temp_humidity_entities_none_when_missing(mod, registries, hass):
    _fill(hass, registries, "a", [("sensor.t1", "20", {"device_class": "temperature"})])
    assert mod._area_temp_humidity_entities(hass, "a") == ("sensor.t1", None)
    assert mod._area_temp_humidity_entities(hass, "empty") == (None, None)


def test_temp_humidity_ignores_non_sensor_domains(mod, registries, hass):
    _fill(hass, registries, "a", [
        ("binary_sensor.t", "on", {"device_class": "temperature"})])
    assert mod._area_temp_humidity_entities(hass, "a") == (None, None)


# ── _area_live_readings ──────────────────────────────────────────────────────

def test_live_readings_format_temperature_and_humidity(mod, registries, hass):
    _fill(hass, registries, "a", [
        ("sensor.t", "21.6", {"device_class": "temperature", "unit_of_measurement": "°C"}),
        ("sensor.h", "45.4", {"device_class": "humidity"}),
    ])
    r = mod._area_live_readings(hass, "a")
    assert r == {"temp": "22°C", "humidity": "45%", "lights": None,
                 "last_motion_seconds": None}


def test_live_readings_temperature_unit_defaults_to_f(mod, registries, hass):
    _fill(hass, registries, "a", [
        ("sensor.t", "70", {"device_class": "temperature"})])
    assert mod._area_live_readings(hass, "a")["temp"] == "70°F"


def test_live_readings_skip_bad_numbers_and_keep_the_first_good_one(mod, registries, hass):
    _fill(hass, registries, "a", [
        ("sensor.t1", "unavailable", {"device_class": "temperature"}),
        ("sensor.t2", "19", {"device_class": "temperature", "unit_of_measurement": "°C"}),
        ("sensor.t3", "25", {"device_class": "temperature", "unit_of_measurement": "°C"}),
        ("sensor.h1", "unknown", {"device_class": "humidity"}),
    ])
    r = mod._area_live_readings(hass, "a")
    assert r["temp"] == "19°C" and r["humidity"] is None


def test_live_readings_light_display(mod, registries, hass):
    _fill(hass, registries, "one_on", [("light.a", "on", {})])
    _fill(hass, registries, "one_off", [("light.b", "off", {})])
    _fill(hass, registries, "many", [("light.c", "on", {}), ("light.d", "off", {}),
                                      ("light.e", "on", {})])
    assert mod._area_live_readings(hass, "one_on")["lights"] == "ON"
    assert mod._area_live_readings(hass, "one_off")["lights"] == "OFF"
    assert mod._area_live_readings(hass, "many")["lights"] == "2/3"


def test_live_readings_last_motion_is_the_youngest_sensor(mod, registries, hass, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(mod.time, "time", lambda: now)
    _fill(hass, registries, "a", [
        ("binary_sensor.old", "off", {"device_class": "motion"}),
        ("binary_sensor.new", "on", {"device_class": "occupancy"}),
        ("binary_sensor.door", "on", {"device_class": "door"}),     # not motion
    ])
    hass.states.get("binary_sensor.old").last_changed = datetime.fromtimestamp(now - 600, timezone.utc)
    hass.states.get("binary_sensor.new").last_changed = datetime.fromtimestamp(now - 30, timezone.utc)
    hass.states.get("binary_sensor.door").last_changed = datetime.fromtimestamp(now - 1, timezone.utc)
    assert mod._area_live_readings(hass, "a")["last_motion_seconds"] == pytest.approx(30)


def test_live_readings_ignore_a_motion_sensor_with_no_timestamp(mod, registries, hass):
    _fill(hass, registries, "a", [("binary_sensor.m", "on", {"device_class": "motion"})])
    hass.states.get("binary_sensor.m").last_changed = None
    assert mod._area_live_readings(hass, "a")["last_motion_seconds"] is None


# ── _dominant_area ───────────────────────────────────────────────────────────

@pytest.fixture
def routing(mod, load, monkeypatch):
    r = load("audio_routing")
    state = types.SimpleNamespace(occupied=[], presence={})
    monkeypatch.setattr(r, "currently_occupied_areas", lambda h: list(state.occupied))
    monkeypatch.setattr(r, "presence_entities_in_area",
                        lambda h, a: list(state.presence.get(a, [])))
    return state


def test_dominant_area_none_when_nobody_is_home(mod, hass, routing):
    assert mod._dominant_area(hass) is None


def test_dominant_area_prefers_indoor_then_most_recent_motion(
        mod, registries, hass, routing):
    registries.areas["yard"] = _Area(id="yard", name="Back Yard")
    registries.areas["kit"] = _Area(id="kit", name="Kitchen")
    registries.areas["lr"] = _Area(id="lr", name="Living Room")
    routing.occupied = ["yard", "kit", "lr"]
    routing.presence = {"yard": ["binary_sensor.yard"], "kit": ["binary_sensor.kit"],
                        "lr": ["binary_sensor.lr"]}
    for eid, ts in (("yard", 900), ("kit", 100), ("lr", 200)):
        hass.states.set(f"binary_sensor.{eid}", "on",
                        last_changed=datetime.fromtimestamp(ts, timezone.utc))
    assert mod._dominant_area(hass) == "lr"       # newest indoor, though yard is newer


def test_dominant_area_uses_outdoor_only_when_that_is_all(mod, registries, hass, routing):
    registries.areas["yard"] = _Area(id="yard", name="Back Yard")
    registries.areas["patio"] = _Area(id="patio", name="Patio")
    routing.occupied = ["yard", "patio"]
    routing.presence = {"yard": ["binary_sensor.y"], "patio": ["binary_sensor.p"]}
    hass.states.set("binary_sensor.y", "on", last_changed=datetime.fromtimestamp(50, timezone.utc))
    hass.states.set("binary_sensor.p", "on", last_changed=datetime.fromtimestamp(80, timezone.utc))
    assert mod._dominant_area(hass) == "patio"


def test_dominant_area_falls_back_to_the_first_candidate(mod, registries, hass, routing):
    registries.areas["kit"] = _Area(id="kit", name="Kitchen")
    registries.areas["lr"] = _Area(id="lr", name="Living Room")
    routing.occupied = ["kit", "lr"]
    routing.presence = {"kit": ["binary_sensor.missing"], "lr": ["binary_sensor.bad"]}
    hass.states.set("binary_sensor.bad", "on")
    hass.states.get("binary_sensor.bad").last_changed = None     # .timestamp() raises
    assert mod._dominant_area(hass) == "kit"


# ── _format_duration, satellites, cast devices ───────────────────────────────

@pytest.mark.parametrize("seconds,text", [
    (None, "—"), (0, "0s"), (59.9, "59s"), (60, "1m"), (3599, "59m"),
    (3600, "1h"), (7300, "2h"),
])
def test_format_duration(mod, seconds, text):
    assert mod._format_duration(seconds) == text


def test_satellite_count(mod, hass):
    hass.states.set("assist_satellite.a", "idle")
    hass.states.set("assist_satellite.b", "unavailable")
    hass.states.set("assist_satellite.c", "unknown")
    hass.states.set("assist_satellite.d", "listening")
    hass.states.set("media_player.x", "idle")
    assert mod._satellite_count(hass) == (2, 4)


def test_get_satellites_with_names_and_areas(mod, registries, hass):
    registries.areas["kit"] = _Area(id="kit", name="Kitchen")
    registries.devices["d1"] = types.SimpleNamespace(area_id="kit")
    registries.devices["d2"] = types.SimpleNamespace(area_id="ghost")
    registries.devices["d3"] = types.SimpleNamespace(area_id=None)
    registries.add_entity("assist_satellite.a", device_id="d1")
    registries.add_entity("assist_satellite.b", device_id="d2")
    registries.add_entity("assist_satellite.c", device_id="d3")
    hass.states.set("assist_satellite.a", "idle", friendly_name="Kitchen Sat")
    hass.states.set("assist_satellite.b", "idle")
    hass.states.set("assist_satellite.c", "idle")
    hass.states.set("assist_satellite.d", "idle")           # not in the registry
    assert mod._get_satellites(hass) == [
        {"entity_id": "assist_satellite.a", "name": "Kitchen Sat", "area": "Kitchen"},
        {"entity_id": "assist_satellite.b", "name": "assist_satellite.b", "area": "ghost"},
        {"entity_id": "assist_satellite.c", "name": "assist_satellite.c", "area": ""},
        {"entity_id": "assist_satellite.d", "name": "assist_satellite.d", "area": ""},
    ]


def test_get_satellites_without_registries_lists_them_with_no_area(mod, registries, hass):
    registries.fail = True
    hass.states.set("assist_satellite.a", "idle", friendly_name="Sat A")
    assert mod._get_satellites(hass) == [
        {"entity_id": "assist_satellite.a", "name": "Sat A", "area": ""}]


def test_get_cast_devices(mod, hass):
    hass.states.set("media_player.cast_tv", "idle", platform="Cast")
    hass.states.set("media_player.a", "idle", friendly_name="Google Mini")
    hass.states.set("media_player.b", "idle", friendly_name="Nest Hub")
    hass.states.set("media_player.c", "idle", friendly_name="Lenovo Clock")
    hass.states.set("media_player.d", "idle", friendly_name="Sonos Beam")
    hass.states.set("media_player.home_group", "idle")
    hass.states.set("media_player.kitchen_group", "idle")
    hass.states.set("media_player.play", "idle", supported_features=16384)
    hass.states.set("media_player.tv", "idle", supported_features=1)    # not cast
    hass.states.set("media_player.off", "unavailable", friendly_name="Sonos Off")
    hass.states.set("media_player.unknown_state", "unknown", friendly_name="Sonos X")
    out = mod._get_cast_devices(hass)
    assert [d["entity_id"] for d in out] == [
        "media_player.cast_tv", "media_player.a", "media_player.b", "media_player.c",
        "media_player.d", "media_player.home_group", "media_player.kitchen_group",
        "media_player.play", "media_player.unknown_state"]
    assert out[1] == {"entity_id": "media_player.a", "name": "Google Mini"}
    assert out[5]["name"] == "media_player.home_group"       # no friendly name


# 8.14.2: a camera's car, animal or package sensor is not motion in a room, so
# it never sets the "last motion" age on an area card. Real motion, occupancy,
# presence and person sensors still do, exactly as before.

def test_live_readings_last_motion_ignores_car_animal_and_package_sensors(
        mod, registries, hass, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(mod.time, "time", lambda: now)
    _fill(hass, registries, "garage", [
        ("binary_sensor.garage_motion", "off", {"device_class": "motion"}),
        ("binary_sensor.garage_car_occupancy", "on", {"device_class": "occupancy"}),
        ("binary_sensor.garage_dog_occupancy", "on", {"device_class": "occupancy"}),
        ("binary_sensor.garage_package_occupancy", "on", {"device_class": "occupancy"}),
    ])
    ages = {"binary_sensor.garage_motion": 900, "binary_sensor.garage_car_occupancy": 5,
            "binary_sensor.garage_dog_occupancy": 6, "binary_sensor.garage_package_occupancy": 7}
    for eid, age in ages.items():
        hass.states.get(eid).last_changed = datetime.fromtimestamp(now - age, timezone.utc)
    assert mod._area_live_readings(hass, "garage")["last_motion_seconds"] == pytest.approx(900)


def test_live_readings_an_area_with_only_object_sensors_has_no_last_motion(
        mod, registries, hass, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(mod.time, "time", lambda: now)
    _fill(hass, registries, "driveway", [
        ("binary_sensor.driveway_car_occupancy", "on", {"device_class": "occupancy"})])
    hass.states.get("binary_sensor.driveway_car_occupancy").last_changed = datetime.fromtimestamp(
        now - 5, timezone.utc)
    assert mod._area_live_readings(hass, "driveway")["last_motion_seconds"] is None


def test_live_readings_real_motion_and_person_sensors_still_count(mod, registries, hass, monkeypatch):
    now = 1_000_000.0
    monkeypatch.setattr(mod.time, "time", lambda: now)
    _fill(hass, registries, "hall", [
        ("binary_sensor.hall_motion", "off", {"device_class": "motion"}),
        ("binary_sensor.hall_person_occupancy", "on", {"device_class": "occupancy"}),
        ("binary_sensor.hall_mmwave", "on", {"device_class": "presence"}),
    ])
    for eid, age in (("binary_sensor.hall_motion", 600), ("binary_sensor.hall_person_occupancy", 20),
                     ("binary_sensor.hall_mmwave", 45)):
        hass.states.get(eid).last_changed = datetime.fromtimestamp(now - age, timezone.utc)
    assert mod._area_live_readings(hass, "hall")["last_motion_seconds"] == pytest.approx(20)
