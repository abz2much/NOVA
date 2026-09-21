"""Source-specific label mapping into camera_semantic's canonical vocabulary
(Phase 4, v7.109.0):
  - camera.py::_map_bus_label     — Frigate/Nest's nova_camera_event 'label'
  - camera.py::_map_vision_category — Nova's own vision-analysis judgment
  - package_monitor.py::_PACKAGE_KIND_TO_STATE — package state machine 'kind'
"""
import sys
import types

import pytest


@pytest.fixture
def cam(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    return load("camera")


@pytest.fixture
def pm(load):
    return load("package_monitor")


# ── camera.py::_map_bus_label (Frigate object labels + Nest event-type text) ──

@pytest.mark.parametrize("raw,expected", [
    ("person", "person"),
    ("car", "vehicle"), ("truck", "vehicle"), ("motorcycle", "vehicle"),
    ("dog", "animal"), ("cat", "animal"),
    ("package", "package"),
    ("bicycle", "activity"),
])
def test_frigate_labels_map_to_canonical(cam, raw, expected):
    assert cam._map_bus_label(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("camera_person_detected", "person"),
    ("sdm.devices.events.CameraMotion.Motion", "activity"),
    ("camera_package_delivered", "package"),
])
def test_nest_style_labels_map_by_substring(cam, raw, expected):
    assert cam._map_bus_label(raw) == expected


@pytest.mark.parametrize("raw", [
    "doorbell_chime", "sound", "", None, "unknown_object_type_v3",
])
def test_unrecognised_or_interaction_labels_are_dropped(cam, raw):
    assert cam._map_bus_label(raw) is None


def test_bus_label_mapping_only_returns_canonical_labels(cam, load):
    cs = load("camera_semantic")
    for raw in ("person", "car", "truck", "motorcycle", "dog", "cat", "package", "bicycle"):
        assert cam._map_bus_label(raw) in cs.CANONICAL_LABELS


# ── camera.py::_map_vision_category (the _reason_about_scene JSON schema) ────

@pytest.mark.parametrize("category,expected_label,expected_pkg", [
    ("delivery", "package", "delivered"),
    ("package", "package", "delivered"),
    ("mail", "package", "mail"),
    ("person", "person", None),
    ("known_resident", "person", None),
    ("vehicle", "vehicle", None),
    ("animal", "animal", None),
    ("other", "activity", None),
])
def test_vision_categories_map_to_canonical(cam, category, expected_label, expected_pkg):
    label, pkg = cam._map_vision_category(category)
    assert (label, pkg) == (expected_label, expected_pkg)


@pytest.mark.parametrize("category", ["empty", "", None, "unexpected_llm_output"])
def test_vision_empty_or_unrecognised_categories_are_not_recorded(cam, category):
    label, pkg = cam._map_vision_category(category)
    assert label is None


def test_vision_category_mapping_only_returns_canonical_labels(cam, load):
    cs = load("camera_semantic")
    for category in ("delivery", "package", "mail", "person", "known_resident",
                      "vehicle", "animal", "other"):
        label, _ = cam._map_vision_category(category)
        assert label in cs.CANONICAL_LABELS


# ── package_monitor.py::_PACKAGE_KIND_TO_STATE ────────────────────────────────

@pytest.mark.parametrize("kind,expected_state", [
    ("delivered", "delivered"),
    ("mail", "mail"),
    ("removed", "taken"),
    ("stranded", "stranded"),
])
def test_package_kind_maps_to_camera_semantic_package_state(pm, kind, expected_state):
    assert pm._PACKAGE_KIND_TO_STATE[kind] == expected_state


def test_package_kind_map_only_produces_valid_package_states(pm, load):
    cs = load("camera_semantic")
    for state in pm._PACKAGE_KIND_TO_STATE.values():
        assert state in cs.PACKAGE_STATES
