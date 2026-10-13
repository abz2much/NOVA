"""Acceptance tests for residence_graph.py, written from its behaviour spec."""
import json
import sys
import types

import pytest


@pytest.fixture
def rg(load):
    return load("residence_graph")


@pytest.fixture
def areas(monkeypatch):
    """Fake HA area registry. Fill with {area_id: name}."""
    data = {}
    ar = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(ar, "async_get", lambda hass: types.SimpleNamespace(
        async_get_area=lambda aid: (types.SimpleNamespace(id=aid, name=data[aid])
                                    if aid in data else None),
        async_list_areas=lambda: [types.SimpleNamespace(id=k, name=v) for k, v in data.items()]))
    return data


def room(name, x, y, w=10, h=10):
    return {"name": name, "x": x, "y": y, "w": w, "h": h}


def plan(*floors):
    return {"floor_plan_rooms": {f"F{i}": {"rooms": rooms} for i, rooms in enumerate(floors)}}


# A row of rooms: Porch | Hall | Lounge | Den, plus a detached Studio.
ROW = plan([room("Porch", 0, 0), room("Hall", 10, 0), room("Lounge", 20, 0),
            room("Den", 30, 0), room("Studio", 300, 300)])


# ── slug ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Guest Bedroom", "guest_bedroom"), ("  Utility   Room ", "utility_room"),
    ("HALL", "hall"), (None, ""), ("", ""), (12, "12"), ("a\tb\nc", "a_b_c")])
def test_slug(rg, raw, expected):
    assert rg.slug(raw) == expected


# ── _touch ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("other,expected", [
    (("B", 5, 5, 10, 10), True),          # overlap
    (("B", 10, 0, 10, 10), True),         # shared edge
    (("B", 15, 0, 10, 10), True),         # gap 5
    (("B", 19.9, 0, 10, 10), True),       # gap just under 10
    (("B", 20, 0, 10, 10), False),        # gap exactly 10
    (("B", 0, 20, 10, 10), False),        # vertical gap exactly 10
    (("B", 19.9, 19.9, 10, 10), True),    # diagonal, both axes within
    (("B", 15, 40, 10, 10), False),       # x close, y far
    (("B", -15, 0, 10, 10), True),        # gap 5 on the left
])
def test_touch_default_gap(rg, other, expected):
    assert rg._touch(("A", 0, 0, 10, 10), other) is expected


def test_touch_custom_gap(rg):
    assert rg._touch(("A", 0, 0, 10, 10), ("B", 15, 0, 10, 10), gap=2) is False
    assert rg._touch(("A", 0, 0, 10, 10), ("B", 15, 0, 10, 10), gap=6) is True


# ── room_adjacency ───────────────────────────────────────────────────────────

def test_adjacency_row(rg):
    adj = rg.room_adjacency(ROW)
    assert adj == {"porch": {"hall"}, "hall": {"porch", "lounge"},
                   "lounge": {"hall", "den"}, "den": {"lounge"}}


def test_adjacency_no_plan(rg):
    assert rg.room_adjacency({}) == {}
    assert rg.room_adjacency({"floor_plan_rooms": None}) == {}
    assert rg.room_adjacency({"floor_plan_rooms": {}}) == {}
    assert rg.room_adjacency({"floor_plan_rooms": "not json"}) == {}


def test_adjacency_json_string(rg):
    cfg = {"floor_plan_rooms": json.dumps({"G": {"rooms": [room("A", 0, 0), room("B", 12, 0)]}})}
    assert rg.room_adjacency(cfg) == {"a": {"b"}, "b": {"a"}}


def test_adjacency_floor_as_list(rg):
    cfg = {"floor_plan_rooms": {"G": [room("A", 0, 0), room("B", 0, 10)], "U": None}}
    assert rg.room_adjacency(cfg) == {"a": {"b"}, "b": {"a"}}


def test_adjacency_floors_are_separate(rg):
    cfg = plan([room("Kitchen", 0, 0)], [room("Bedroom", 0, 0)])
    assert rg.room_adjacency(cfg) == {}


def test_adjacency_skips_bad_rooms_and_defaults_size(rg):
    cfg = plan([{"name": "A", "x": 0, "y": 0}, {"name": "B", "x": "5", "y": "0"},
                {"x": 1, "y": 1}, {"name": "C", "x": "far", "y": 0},
                {"name": "D", "y": 0}])
    assert rg.room_adjacency(cfg) == {"a": {"b"}, "b": {"a"}}


def test_adjacency_zero_size_same_point(rg):
    cfg = plan([{"name": "A", "x": 3, "y": 3}, {"name": "B", "x": 3, "y": 3}])
    assert rg.room_adjacency(cfg) == {"a": {"b"}, "b": {"a"}}


def test_adjacency_ignores_same_slug(rg):
    cfg = plan([room("Hall", 0, 0), room("hall", 5, 0), room("Stairs", 100, 100)])
    assert rg.room_adjacency(cfg) == {}


def test_adjacency_isolated_rooms_absent(rg):
    assert "studio" not in rg.room_adjacency(ROW)


def test_adjacency_list_plan_raises_quirk(rg):
    with pytest.raises(AttributeError):
        rg.room_adjacency({"floor_plan_rooms": [room("A", 0, 0)]})


# ── _area_slug ───────────────────────────────────────────────────────────────

def test_area_slug_uses_registry_name(rg, fake_hass, areas):
    areas["a1"] = "Living Room"
    assert rg._area_slug(fake_hass, "a1") == "living_room"


def test_area_slug_falls_back_to_id(rg, fake_hass, areas):
    assert rg._area_slug(fake_hass, "Spare Room") == "spare_room"
    areas["a2"] = ""
    assert rg._area_slug(fake_hass, "a2") == "a2"


def test_area_slug_registry_error(rg, fake_hass, monkeypatch):
    def boom(hass):
        raise RuntimeError("x")
    monkeypatch.setattr(sys.modules["homeassistant.helpers.area_registry"], "async_get", boom)
    assert rg._area_slug(fake_hass, "Back Hall") == "back_hall"


# ── adjacent_areas ───────────────────────────────────────────────────────────

def test_adjacent_areas_maps_neighbours(rg, fake_hass, areas):
    areas.update({"id_porch": "Porch", "id_hall": "Hall", "id_lounge": "Lounge",
                  "id_den": "Den", "id_garage": "Garage"})
    assert rg.adjacent_areas(fake_hass, ROW, "id_hall") == {"id_porch", "id_lounge"}
    assert rg.adjacent_areas(fake_hass, ROW, "id_porch") == {"id_hall"}


def test_adjacent_areas_empty_cases(rg, fake_hass, areas):
    areas.update({"id_hall": "Hall"})
    assert rg.adjacent_areas(fake_hass, ROW, None) == set()
    assert rg.adjacent_areas(fake_hass, ROW, "") == set()
    assert rg.adjacent_areas(fake_hass, {}, "id_hall") == set()
    assert rg.adjacent_areas(fake_hass, ROW, "id_unknown") == set()
    # neighbours exist but no area is named after them
    assert rg.adjacent_areas(fake_hass, ROW, "id_hall") == set()


def test_adjacent_areas_registry_error(rg, fake_hass, monkeypatch):
    monkeypatch.setattr(rg, "_area_slug", lambda h, a: a)
    def boom(hass):
        raise RuntimeError("x")
    monkeypatch.setattr(sys.modules["homeassistant.helpers.area_registry"], "async_get", boom)
    assert rg.adjacent_areas(fake_hass, ROW, "hall") == set()


def test_adjacent_areas_honours_patched_seams(rg, fake_hass, areas, monkeypatch):
    areas.update({"x1": "Alpha", "x2": "Beta"})
    monkeypatch.setattr(rg, "room_adjacency", lambda cfg: {"alpha": {"beta"}, "beta": {"alpha"}})
    monkeypatch.setattr(rg, "_area_slug", lambda h, a: "alpha")
    assert rg.adjacent_areas(fake_hass, {}, "anything") == {"x2"}


# ── hops_from_breach ─────────────────────────────────────────────────────────

def test_hops_row(rg, fake_hass, areas):
    areas.update({"id_porch": "Porch", "id_hall": "Hall", "id_lounge": "Lounge",
                  "id_den": "Den", "id_studio": "Studio", "id_loft": "Loft"})
    assert rg.hops_from_breach(fake_hass, ROW, "id_porch") == {
        "id_porch": 0, "id_hall": 1, "id_lounge": 2, "id_den": 3}
    assert rg.hops_from_breach(fake_hass, ROW, "id_lounge") == {
        "id_lounge": 0, "id_hall": 1, "id_den": 1, "id_porch": 2}


def test_hops_shortest_path(rg, fake_hass, areas, monkeypatch):
    areas.update({k: k for k in ("a", "b", "c", "d")})
    monkeypatch.setattr(rg, "room_adjacency", lambda cfg: {
        "a": {"b", "c"}, "b": {"a", "d"}, "c": {"a", "d"}, "d": {"b", "c"}})
    assert rg.hops_from_breach(fake_hass, {}, "a") == {"a": 0, "b": 1, "c": 1, "d": 2}


def test_hops_empty_cases(rg, fake_hass, areas):
    areas.update({"id_hall": "Hall", "id_studio": "Studio"})
    assert rg.hops_from_breach(fake_hass, ROW, None) == {}
    assert rg.hops_from_breach(fake_hass, {}, "id_hall") == {}
    assert rg.hops_from_breach(fake_hass, ROW, "id_studio") == {}   # not in graph
    assert rg.hops_from_breach(None, {}, None) == {}


def test_hops_unmatched_areas_absent(rg, fake_hass, areas):
    areas.update({"id_hall": "Hall"})
    assert rg.hops_from_breach(fake_hass, ROW, "id_hall") == {"id_hall": 0}


def test_hops_registry_list_failure(rg, fake_hass, monkeypatch):
    monkeypatch.setattr(rg, "_area_slug", lambda h, a: a)
    def boom(hass):
        raise RuntimeError("x")
    monkeypatch.setattr(sys.modules["homeassistant.helpers.area_registry"], "async_get", boom)
    assert rg.hops_from_breach(fake_hass, ROW, "hall") == {}


def test_two_areas_same_room_quirk(rg, fake_hass, areas):
    areas.update({"id_hall": "Hall", "id_hall2": "hall", "id_den": "Den", "id_lounge": "Lounge"})
    assert rg.hops_from_breach(fake_hass, ROW, "id_den")["id_hall2"] == 2
    assert rg.adjacent_areas(fake_hass, ROW, "id_lounge") == {"id_hall", "id_hall2", "id_den"}
