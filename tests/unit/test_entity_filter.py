"""Entities taken out of Nova's awareness (entity_filter.is_excluded), and the
places that must honour it: presence detection, the panel's room cards and
area commands. The settings live in the Nova entry's runtime_config, which
the `nova_runtime` fixture installs."""
import ast
import json
import types

import pytest
from ws_sources import COMP, ws_function


@pytest.fixture
def ef(load):
    return load("entity_filter")


@pytest.fixture
def home(fake_hass, nova_runtime):
    """home(**settings) -> (hass, live runtime_config). The dict is the one
    the runtime holds, so changing it is like a panel write."""
    def _make(**settings):
        live = dict(settings)
        nova_runtime(fake_hass, live)
        return fake_hass, live
    return _make


def _labelled(ef, monkeypatch, carried, names=None):
    """Make the entity registry give light.porch the label ids in `carried`,
    and (when `names` is given) the label registry a name for each id."""
    entry = types.SimpleNamespace(labels=set(carried))
    monkeypatch.setattr(ef, "er", types.SimpleNamespace(async_get=lambda _h: types.SimpleNamespace(
        async_get=lambda eid: entry if eid == "light.porch" else None)))
    if names is None:
        monkeypatch.setattr(ef, "_lr", None)
    else:
        labels = {lid: types.SimpleNamespace(name=name) for lid, name in names.items()}
        monkeypatch.setattr(ef, "_lr", types.SimpleNamespace(async_get=lambda _h: types.SimpleNamespace(
            async_get_label=labels.get)))


# ── is_excluded ─────────────────────────────────────────────────────────────

def test_nothing_is_excluded_by_default(ef, home):
    hass, _ = home()
    assert ef.is_excluded(hass, "light.kitchen") is False


def test_nothing_is_excluded_without_a_nova_entry(ef, fake_hass):
    assert ef.is_excluded(fake_hass, "light.kitchen") is False


@pytest.mark.parametrize("settings, hit, miss", [
    ({"excluded_entities": ["light.kitchen"]}, "light.kitchen", "light.den"),
    ({"excluded_domains": ["switch"]}, "switch.fan", "light.den"),
    ({"excluded_domains": [" Switch "]}, "switch.fan", "fan.switch"),
    ({"excluded_entities": json.dumps(["binary_sensor.ghost"])},
     "binary_sensor.ghost", "binary_sensor.door"),
    ({"excluded_entities": "light.single"}, "light.single", "light.singles"),
    ({"excluded_domains": '{"not": "a list"}'}, None, "light.den"),
], ids=["entity", "domain", "domain-trimmed", "json-string", "bare-string", "json-object"])
def test_exclusion_settings(ef, home, settings, hit, miss):
    hass, _ = home(**settings)
    if hit is not None:
        assert ef.is_excluded(hass, hit) is True
    assert ef.is_excluded(hass, miss) is False


def test_a_panel_change_applies_at_once(ef, home):
    hass, live = home()
    assert ef.is_excluded(hass, "light.den") is False
    live["excluded_entities"] = ["light.den"]
    assert ef.is_excluded(hass, "light.den") is True


@pytest.mark.parametrize("missing", ["", None])
def test_no_entity_id_is_never_excluded(ef, home, missing):
    hass, _ = home(excluded_domains=["light"])
    assert ef.is_excluded(hass, missing) is False


def test_exclude_by_label_id(ef, home, monkeypatch):
    hass, _ = home(excluded_labels=["lbl_night"])
    _labelled(ef, monkeypatch, {"lbl_night"})
    assert ef.is_excluded(hass, "light.porch") is True
    assert ef.is_excluded(hass, "light.y") is False


def test_exclude_by_label_name(ef, home, monkeypatch):
    hass, _ = home(excluded_labels=["Night lights"])
    _labelled(ef, monkeypatch, {"lbl_7"}, names={"lbl_7": "Night lights"})
    assert ef.is_excluded(hass, "light.porch") is True


def test_label_name_needs_the_label_registry(ef, home, monkeypatch):
    hass, _ = home(excluded_labels=["Night lights"])
    _labelled(ef, monkeypatch, {"lbl_7"}, names=None)
    assert ef.is_excluded(hass, "light.porch") is False


def test_a_broken_registry_means_not_excluded(ef, home, monkeypatch):
    hass, _ = home(excluded_labels=["lbl_night"])

    def _boom(_hass):
        raise RuntimeError("registry gone")
    monkeypatch.setattr(ef, "er", types.SimpleNamespace(async_get=_boom))
    assert ef.is_excluded(hass, "light.porch") is False


def test_snapshot_is_used_instead_of_the_live_config(ef, home):
    hass, live = home(excluded_entities=["light.a"], excluded_domains=["Fan"],
                      excluded_labels=["quiet"])
    snap = ef.exclusion_snapshot(hass)
    assert snap == ({"light.a"}, {"fan"}, {"quiet"})
    live["excluded_entities"] = ["light.b"]
    # The executor job keeps the answer it was handed.
    assert ef.is_excluded(hass, "light.a", exclusions=snap) is True
    assert ef.is_excluded(hass, "light.b", exclusions=snap) is False
    # And the snapshot is a copy, not the live config.
    assert ef.exclusion_snapshot(hass)[0] == {"light.b"}


# ── presence honours it ─────────────────────────────────────────────────────

def _occupancy(hass, eid, name):
    hass.states.set(eid, "on", device_class="occupancy", friendly_name=name)


def test_excluded_occupancy_sensor_is_not_room_presence(load, home):
    """A virtual occupancy sensor taken out of Nova must not put anyone in
    a room."""
    presence = load("presence")
    hass, _ = home(excluded_entities=["binary_sensor.ghost_occupancy"])
    _occupancy(hass, "binary_sensor.kitchen_presence", "Kitchen Presence")
    _occupancy(hass, "binary_sensor.ghost_occupancy", "Ghost Occupancy")
    rooms = presence.get_presence_summary(hass).get("rooms", {})
    assert "kitchen" in rooms
    assert "ghost" not in rooms


def test_the_same_sensor_counts_when_not_excluded(load, home):
    presence = load("presence")
    hass, _ = home()
    _occupancy(hass, "binary_sensor.ghost_occupancy", "Ghost Occupancy")
    assert "ghost" in presence.get_presence_summary(hass).get("rooms", {})


# ── the room card and area commands call it ─────────────────────────────────

def _calls_is_excluded(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name == "is_excluded":
                return True
    return False


def test_room_card_area_listing_filters_excluded():
    """An excluded entity is never counted or listed on a room card."""
    assert _calls_is_excluded(ws_function("_entities_in_area")), \
        "_entities_in_area must filter excluded entities"
