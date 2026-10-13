"""Acceptance tests for outdoor.py, written from its behaviour spec."""
import sys
import types

import pytest


@pytest.fixture
def od(load):
    return load("outdoor")


@pytest.fixture
def cfg(load, monkeypatch):
    store = {}
    monkeypatch.setattr(load("nova_config"), "get", lambda key, default=None: store.get(key, default))
    return store


@pytest.fixture
def registries(monkeypatch):
    """Fake entity/device/area registries. Fill the returned dicts."""
    data = {"entities": {}, "devices": {}, "areas": {}}
    er = sys.modules["homeassistant.helpers.entity_registry"]
    dr = sys.modules["homeassistant.helpers.device_registry"]
    ar = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: types.SimpleNamespace(
        async_get=lambda eid: data["entities"].get(eid)))
    monkeypatch.setattr(dr, "async_get", lambda hass: types.SimpleNamespace(
        async_get=lambda did: data["devices"].get(did)))
    monkeypatch.setattr(ar, "async_get", lambda hass: types.SimpleNamespace(
        async_get_area=lambda aid: data["areas"].get(aid),
        async_list_areas=lambda: list(data["areas"].values())))
    return data


def _ent(area_id=None, device_id=None):
    return types.SimpleNamespace(area_id=area_id, device_id=device_id)


def _area(aid, name):
    return types.SimpleNamespace(id=aid, name=name)


# ── data ─────────────────────────────────────────────────────────────────────

def test_default_outdoor_areas_exact(od):
    assert od.DEFAULT_OUTDOOR_AREAS == {
        "backyard", "back_yard", "front_yard", "front_door", "driveway", "patio",
        "porch", "side_yard", "deck", "garden", "yard", "pool", "exterior",
        "outside", "outdoor", "outdoors", "shed", "carport", "walkway", "sidewalk",
        "lawn", "curb", "gazebo"}


def test_notable_table_exact(od):
    assert od.NOTABLE_OUTDOOR == {
        "person": True, "package": True, "mail": True, "damage": True,
        "vehicle": False, "animal": False, "motion": False}


# ── _cfg_list ────────────────────────────────────────────────────────────────

def test_cfg_list_list_is_normalised(od, cfg):
    cfg["indoor_entities"] = ["  Light.Hall ", "", "   ", 5]
    assert od._cfg_list("indoor_entities") == ["light.hall", "5"]


def test_cfg_list_tuple_ok(od, cfg):
    cfg["indoor_entities"] = ("A", "b")
    assert od._cfg_list("indoor_entities") == ["a", "b"]


def test_cfg_list_json_string(od, cfg):
    cfg["outdoor_entities"] = '["Camera.Yard", "x"]'
    assert od._cfg_list("outdoor_entities") == ["camera.yard", "x"]


@pytest.mark.parametrize("value", [None, 3, {"a": 1}, "not json", '"a string"', '{"a": 1}', "", "  "])
def test_cfg_list_other_values_empty(od, cfg, value):
    cfg["outdoor_entities"] = value
    assert od._cfg_list("outdoor_entities") == []


def test_cfg_list_missing_key(od, cfg):
    assert od._cfg_list("outdoor_areas") == []


def test_cfg_list_never_raises(od, load, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(load("nova_config"), "get", boom)
    assert od._cfg_list("indoor_entities") == []


# ── _area_slug ───────────────────────────────────────────────────────────────

def test_area_slug_none_hass(od):
    assert od._area_slug(None, "light.hall") == ""


def test_area_slug_from_entity(od, fake_hass, registries):
    registries["entities"]["camera.one"] = _ent(area_id="a1")
    registries["areas"]["a1"] = _area("a1", "  Back   Garden ")
    assert od._area_slug(fake_hass, "camera.one") == "back_garden"


def test_area_slug_from_device(od, fake_hass, registries):
    registries["entities"]["camera.one"] = _ent(device_id="d1")
    registries["devices"]["d1"] = types.SimpleNamespace(area_id="a2")
    registries["areas"]["a2"] = _area("a2", "Driveway")
    assert od._area_slug(fake_hass, "camera.one") == "driveway"


def test_area_slug_unknown_area_uses_id(od, fake_hass, registries):
    registries["entities"]["camera.one"] = _ent(area_id="Side Yard")
    assert od._area_slug(fake_hass, "camera.one") == "side_yard"


def test_area_slug_no_entry_or_no_area(od, fake_hass, registries):
    assert od._area_slug(fake_hass, "camera.none") == ""
    registries["entities"]["camera.one"] = _ent()
    assert od._area_slug(fake_hass, "camera.one") == ""


def test_area_slug_registry_failure(od, fake_hass, monkeypatch):
    def boom(hass):
        raise RuntimeError("no registry")
    monkeypatch.setattr(sys.modules["homeassistant.helpers.entity_registry"], "async_get", boom)
    assert od._area_slug(fake_hass, "camera.one") == ""


# ── is_outdoor: name layer ───────────────────────────────────────────────────

KEYWORDS = ["outdoor", "outside", "exterior", "backyard", "back_yard", "frontyard",
            "front_yard", "sideyard", "side_yard", "yard", "garden", "driveway",
            "porch", "patio", "deck", "shed", "gazebo", "carport", "curb", "street",
            "sidewalk", "walkway", "lawn", "pool", "doorbell", "mailbox", "gate", "fence"]


@pytest.mark.parametrize("kw", KEYWORDS)
def test_every_keyword_is_outdoor(od, cfg, fake_hass, kw):
    assert od.is_outdoor(fake_hass, f"binary_sensor.{kw}_motion") is True
    assert od.is_outdoor(fake_hass, f"binary_sensor.motion_{kw}") is True


@pytest.mark.parametrize("eid", [
    "binary_sensor.front_door", "lock.front_door", "cover.garage_door",
    "binary_sensor.gateway_status", "binary_sensor.patio2", "binary_sensor.kitchen_motion",
    "binary_sensor.front_motion", "binary_sensor.decking_light", "light.hall",
])
def test_non_keywords_are_indoor(od, cfg, fake_hass, eid):
    assert od.is_outdoor(fake_hass, eid) is False


@pytest.mark.parametrize("eid", ["camera.porch", "binary_sensor.porch-motion", "Camera.PORCH_cam"])
def test_keyword_boundaries_and_case(od, cfg, fake_hass, eid):
    assert od.is_outdoor(fake_hass, eid) is True


def test_friendly_name_argument(od, cfg, fake_hass):
    assert od.is_outdoor(fake_hass, "binary_sensor.zone_1", "Back Garden Motion") is True
    assert od.is_outdoor(fake_hass, "binary_sensor.zone_1", "Landing") is False


def test_friendly_name_from_state(od, cfg, fake_hass):
    fake_hass.states.set("binary_sensor.zone_2", "on", friendly_name="Front Yard Sensor")
    assert od.is_outdoor(fake_hass, "binary_sensor.zone_2") is True


def test_explicit_friendly_name_beats_state(od, cfg, fake_hass):
    fake_hass.states.set("binary_sensor.zone_3", "on", friendly_name="Patio")
    assert od.is_outdoor(fake_hass, "binary_sensor.zone_3", "Study") is False


def test_none_hass_and_none_entity(od, cfg):
    assert od.is_outdoor(None, "camera.driveway") is True
    assert od.is_outdoor(None, None) is False


# ── is_outdoor: config layers ────────────────────────────────────────────────

def test_indoor_glob_beats_keyword(od, cfg, fake_hass):
    cfg["indoor_entities"] = ["binary_sensor.pool_*"]
    assert od.is_outdoor(fake_hass, "binary_sensor.pool_room") is False


def test_indoor_glob_beats_outdoor_glob(od, cfg, fake_hass):
    cfg["indoor_entities"] = ["sensor.x"]
    cfg["outdoor_entities"] = ["sensor.*"]
    assert od.is_outdoor(fake_hass, "sensor.x") is False
    assert od.is_outdoor(fake_hass, "sensor.y") is True


def test_indoor_glob_beats_area(od, cfg, fake_hass, monkeypatch):
    monkeypatch.setattr(od, "_area_slug", lambda h, e: "driveway")
    cfg["indoor_entities"] = ["camera.c1"]
    assert od.is_outdoor(fake_hass, "camera.c1") is False


def test_globs_match_friendly_name(od, cfg, fake_hass):
    cfg["outdoor_entities"] = ["*greenhouse*"]
    assert od.is_outdoor(fake_hass, "sensor.zone_9", "Greenhouse Temp") is True


def test_glob_case_insensitive_via_lowercasing(od, cfg, fake_hass):
    cfg["outdoor_entities"] = ["SENSOR.ZONE_*"]
    assert od.is_outdoor(fake_hass, "Sensor.Zone_4") is True


def test_json_string_globs(od, cfg, fake_hass):
    cfg["outdoor_entities"] = '["sensor.zone_*"]'
    assert od.is_outdoor(fake_hass, "sensor.zone_5") is True


# ── is_outdoor: area layer ───────────────────────────────────────────────────

def test_default_area_makes_outdoor(od, cfg, fake_hass, monkeypatch):
    monkeypatch.setattr(od, "_area_slug", lambda h, e: "front_door")
    assert od.is_outdoor(fake_hass, "camera.c1") is True


def test_unknown_area_falls_through(od, cfg, fake_hass, monkeypatch):
    monkeypatch.setattr(od, "_area_slug", lambda h, e: "study")
    assert od.is_outdoor(fake_hass, "camera.c1") is False
    assert od.is_outdoor(fake_hass, "camera.c1_driveway") is True


def test_configured_area_extends(od, cfg, fake_hass, monkeypatch):
    monkeypatch.setattr(od, "_area_slug", lambda h, e: "allotment")
    cfg["outdoor_areas"] = ["Allotment"]
    assert od.is_outdoor(fake_hass, "camera.c1") is True


def test_configured_area_not_slugged_quirk(od, cfg, fake_hass, monkeypatch):
    monkeypatch.setattr(od, "_area_slug", lambda h, e: "far_field")
    cfg["outdoor_areas"] = ["Far Field"]
    assert od.is_outdoor(fake_hass, "camera.c1") is False


def test_area_layer_via_registries(od, cfg, fake_hass, registries):
    registries["entities"]["camera.c7"] = _ent(area_id="a9")
    registries["areas"]["a9"] = _area("a9", "Car Port")
    assert od.is_outdoor(fake_hass, "camera.c7") is False   # "car_port" not a default
    registries["areas"]["a9"] = _area("a9", "Carport")
    assert od.is_outdoor(fake_hass, "camera.c7") is True


# ── location_mode ────────────────────────────────────────────────────────────

def test_location_mode(od):
    assert od.location_mode("camera.a", ["camera.a"], []) == "indoor"
    assert od.location_mode("camera.a", [], ["CAMERA.A"]) == "outdoor"
    assert od.location_mode("Camera.A", ["camera.a"], ["camera.a"]) == "indoor"
    assert od.location_mode("camera.a", ["camera.*"], ["camera.*"]) == "auto"
    assert od.location_mode("camera.a", None, None) == "auto"
    assert od.location_mode(None, [], []) == "auto"


def test_location_mode_does_not_strip(od):
    assert od.location_mode(" camera.a ", ["camera.a"], []) == "auto"


# ── set_entity_location ──────────────────────────────────────────────────────

def test_pin_indoor(od):
    ind, out = ["camera.*", "camera.b"], ["Camera.A", "camera.c"]
    new_in, new_out = od.set_entity_location(ind, out, " Camera.A ", "indoor")
    assert new_in == ["camera.*", "camera.b", "camera.a"]
    assert new_out == ["camera.c"]
    assert ind == ["camera.*", "camera.b"] and out == ["Camera.A", "camera.c"]


def test_pin_outdoor(od):
    new_in, new_out = od.set_entity_location(["camera.a"], ["camera.z"], "camera.a", "outdoor")
    assert new_in == [] and new_out == ["camera.z", "camera.a"]


@pytest.mark.parametrize("mode", ["auto", "bogus", ""])
def test_unpin(od, mode):
    new_in, new_out = od.set_entity_location(["camera.a", "camera.a"], ["CAMERA.A"], "camera.a", mode)
    assert new_in == [] and new_out == []


def test_pin_none_lists(od):
    assert od.set_entity_location(None, None, "camera.a", "outdoor") == ([], ["camera.a"])


def test_pin_returns_tuple(od):
    assert isinstance(od.set_entity_location([], [], "camera.a", "auto"), tuple)


# ── notable ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("dtype,expected", [
    ("person", True), ("package", True), ("mail", True), ("damage", True),
    ("vehicle", False), ("animal", False), ("motion", False), ("PERSON", True),
    ("unicorn", False)])
def test_notable_outdoor_area(od, cfg, dtype, expected):
    assert od.notable(None, "sensor.any", dtype, area_name="Back Yard") is expected


def test_notable_indoor_area(od, cfg):
    assert od.notable(None, "camera.driveway", "person", area_name="Kitchen") is False


def test_notable_configured_area(od, cfg):
    cfg["outdoor_areas"] = ["paddock"]
    assert od.notable(None, "x.y", "package", area_name="  Paddock ") is True


def test_notable_empty_area_name_quirk(od, cfg):
    assert od.notable(None, "camera.driveway", "person", area_name="") is False


def test_notable_by_entity(od, cfg, fake_hass):
    assert od.notable(fake_hass, "camera.driveway", "person") is True
    assert od.notable(fake_hass, "camera.hallway", "person") is False


def test_notable_default_type_is_motion(od, cfg, fake_hass):
    assert od.notable(fake_hass, "camera.driveway") is False


def test_notable_uses_is_outdoor(od, cfg, monkeypatch):
    monkeypatch.setattr(od, "is_outdoor", lambda hass, eid, *a, **k: eid == "camera.c1")
    assert od.notable(None, "camera.c1", "mail") is True
    assert od.notable(None, "camera.c2", "mail") is False
