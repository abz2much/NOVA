"""Rooms only where the home has them (8.27.0).

home_doors.home_features() tells the panel whether to show the basement, an
outside cellar door and a utility room. Each is shown only on a reliable
signal or the user's own choice, never on a name on a device or a default.
Display only, like the garage flag. All state is fake.
"""
import pathlib
import sys
import types

import pytest

NOVA = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"


@pytest.fixture
def hd(load):
    return load("home_doors")


@pytest.fixture
def areas(monkeypatch):
    names = []
    ar = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(ar, "async_get", lambda hass: types.SimpleNamespace(
        async_list_areas=lambda: [types.SimpleNamespace(name=n) for n in names]))
    return names


@pytest.fixture
def floors(monkeypatch):
    """Home Assistant floors as (name, level)."""
    items = []
    fr = types.ModuleType("homeassistant.helpers.floor_registry")
    fr.async_get = lambda hass: types.SimpleNamespace(async_list_floors=lambda: [
        types.SimpleNamespace(name=n, level=lv) for n, lv in items])
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.floor_registry", fr)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "floor_registry", fr, raising=False)
    return items


def _f(hd, fake_hass, cfg=None):
    return hd.home_features(fake_hass, cfg or {})


# ── a home with no basement, no cellar ──────────────────────────────────────

def test_a_plain_home_has_none_of_them(hd, fake_hass, areas, floors):
    areas += ["Kitchen", "Hallway", "Back garden"]
    floors += [("Ground floor", 0), ("First floor", 1)]
    fake_hass.states.set("sensor.basement_temperature", "12")      # a name only
    fake_hass.states.set("binary_sensor.cellar_door", "off", device_class="door")
    assert _f(hd, fake_hass) == {"basement": False, "cellar_door": False, "utility": False}


def test_old_defaults_never_count(hd, fake_hass):
    cfg = {"floor_plan_rooms": {"bsmt": {"rooms": [{"name": "Basement"}]}},
           "door_mapping": {"basement": "", "cellar": ""}}
    assert _f(hd, fake_hass, cfg) == {"basement": False, "cellar_door": False, "utility": False}


def test_no_registries_at_all_is_none(hd, fake_hass):
    assert _f(hd, fake_hass) == {"basement": False, "cellar_door": False, "utility": False}


# ── basement signals ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["Basement", "basement", "Cellar", "Wine cellar"])
def test_an_area_named_basement_or_cellar(hd, fake_hass, areas, name):
    areas.append(name)
    assert _f(hd, fake_hass)["basement"] is True


@pytest.mark.parametrize("name", ["Basement", "Cellar"])
def test_a_floor_named_basement_or_cellar(hd, fake_hass, floors, name):
    floors.append((name, None))
    assert _f(hd, fake_hass)["basement"] is True


def test_a_floor_below_ground(hd, fake_hass, floors):
    floors.append(("Lower ground", -1))
    assert _f(hd, fake_hass)["basement"] is True


@pytest.mark.parametrize("level", [0, 1, None, "-1", True])
def test_other_floor_levels_are_not_a_basement(hd, fake_hass, floors, level):
    floors.append(("Somewhere", level))
    assert _f(hd, fake_hass)["basement"] is False


@pytest.mark.parametrize("slot", ["basement", "cellar"])
def test_a_mapped_door_slot(hd, fake_hass, slot):
    assert _f(hd, fake_hass, {"door_mapping": {slot: "lock.down"}})["basement"] is True


# ── the Basement setting ────────────────────────────────────────────────────

def test_yes_and_no_override(hd, fake_hass, areas):
    assert _f(hd, fake_hass, {"basement_mode": "yes"})["basement"] is True
    areas.append("Basement")
    assert _f(hd, fake_hass, {"basement_mode": "no"})["basement"] is False


def test_a_saved_old_setting_is_still_the_users_choice(hd, fake_hass, areas):
    assert hd.basement_mode({"has_basement": True}) == "yes"
    assert hd.basement_mode({"has_basement": False}) == "no"
    areas.append("Basement")
    assert _f(hd, fake_hass, {"has_basement": False})["basement"] is False


def test_the_new_setting_wins_over_the_old_one(hd, fake_hass):
    assert hd.basement_mode({"basement_mode": "auto", "has_basement": False}) == "auto"
    assert _f(hd, fake_hass, {"basement_mode": "yes", "has_basement": False})["basement"] is True


@pytest.mark.parametrize("cfg", [{}, {"has_basement": None}, {"has_basement": "false"},
                                 {"basement_mode": "maybe"}])
def test_never_saved_or_unknown_is_auto(hd, cfg):
    assert hd.basement_mode(cfg) == "auto"


def test_a_failed_read_is_no_basement(hd, fake_hass, monkeypatch):
    monkeypatch.setattr(hd, "basement_signals", lambda *a: 1 / 0)
    assert hd.has_basement(fake_hass, {}) is False


# ── cellar door and utility room ────────────────────────────────────────────

def test_a_cellar_door_only_when_mapped(hd, fake_hass, areas):
    areas.append("Cellar")                          # a basement, but no outside door known
    assert _f(hd, fake_hass)["cellar_door"] is False
    assert _f(hd, fake_hass, {"door_mapping": '{"cellar": "binary_sensor.bulkhead"}'})["cellar_door"] is True


@pytest.mark.parametrize("name,want", [("Utility", True), ("Utility room", True),
                                       ("Laundry", False), ("Utilities cupboard", False)])
def test_a_utility_room_only_with_a_utility_area(hd, fake_hass, areas, name, want):
    areas.append(name)
    assert _f(hd, fake_hass)["utility"] is want


# ── display only ────────────────────────────────────────────────────────────

def test_no_safety_module_reads_the_room_flags():
    for name in ("core_safety.py", "core_lockdown.py", "core_lockdown_sync.py", "world.py",
                 "household.py", "alert_path.py", "sentinel.py", "cognitive_core.py",
                 "intrusion.py", "outdoor.py"):
        text = (NOVA / name).read_text(encoding="utf-8")
        for word in ("home_doors", "home_features", "basement_mode", "has_basement"):
            assert word not in text, (name, word)
