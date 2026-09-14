"""Tests for eufy.py — Eufy Security native-sensor discovery. Discovery is
unique_id-based (immutable) rather than entity_id-based (renameable), mirrored
here against real unique_id strings pulled from a live eufy_security install
(see camera.py's _nest_device_to_camera for the equivalent Nest pattern this
was modeled on)."""
import sys
import types

import pytest


class _Entry:
    def __init__(self, entity_id, platform, device_id, unique_id):
        self.entity_id = entity_id
        self.platform = platform
        self.device_id = device_id
        self.unique_id = unique_id
        self.domain = entity_id.split(".", 1)[0]


class _Registry:
    def __init__(self, entries):
        self.entities = {e.entity_id: e for e in entries}

    def async_get(self, entity_id):
        return self.entities.get(entity_id)


DEVICE = "e64f3b6aeec4e94fa8c46f473f2fe223"
SERIAL = "T82145102549156A"


def _uid(role: str) -> str:
    return f"eufy_security_{SERIAL}_device_{role}"


# The real sibling family on one eufy_security doorbell, exactly as returned
# by HA's entity registry (verified live, not guessed).
FRONT_DOOR_BELL_FAMILY = [
    _Entry("camera.front_door_bell", "eufy_security", DEVICE, _uid("camera")),
    _Entry("binary_sensor.front_door_bell_ringing", "eufy_security", DEVICE, _uid("ringing")),
    _Entry("binary_sensor.front_door_bell_person_detected", "eufy_security", DEVICE, _uid("personDetected")),
    _Entry("binary_sensor.front_door_bell_stranger_person_detected", "eufy_security", DEVICE, _uid("strangerPersonDetected")),
    _Entry("binary_sensor.front_door_bell_identity_person_detected", "eufy_security", DEVICE, _uid("identityPersonDetected")),
    _Entry("binary_sensor.front_door_bell_pet_detected", "eufy_security", DEVICE, _uid("petDetected")),
    _Entry("binary_sensor.front_door_bell_crying_detected", "eufy_security", DEVICE, _uid("cryingDetected")),
    _Entry("binary_sensor.front_door_bell_sound_detected", "eufy_security", DEVICE, _uid("soundDetected")),
    _Entry("binary_sensor.front_door_bell_package_delivered", "eufy_security", DEVICE, _uid("packageDelivered")),
    _Entry("binary_sensor.front_door_bell_package_stranded", "eufy_security", DEVICE, _uid("packageStranded")),
    _Entry("binary_sensor.front_door_bell_package_taken", "eufy_security", DEVICE, _uid("packageTaken")),
    _Entry("binary_sensor.front_door_bell_snooze", "eufy_security", DEVICE, _uid("snooze")),
    _Entry("binary_sensor.front_door_bell_debug_device", "eufy_security", DEVICE, f"eufy_security_{SERIAL}_debug"),
    _Entry("image.front_door_bell_event_image", "eufy_security", DEVICE, _uid("camera")),
]


@pytest.fixture
def eufy(load, monkeypatch):
    helpers = sys.modules.get("homeassistant.helpers") or types.ModuleType("homeassistant.helpers")
    er_mod = types.ModuleType("homeassistant.helpers.entity_registry")
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er_mod)
    monkeypatch.setattr(helpers, "entity_registry", er_mod, raising=False)
    mod = load("eufy")
    mod._er_mod = er_mod  # stash so tests can set async_get per-scenario
    return mod


def _set_registry(eufy_mod, entries):
    eufy_mod._er_mod.async_get = lambda hass: _Registry(entries)


# ── _role_for_unique_id ──────────────────────────────────────────────────────

@pytest.mark.parametrize("suffix,expected", [
    ("ringing", "ringing"),
    ("strangerPersonDetected", "stranger"),
    ("identityPersonDetected", "identity"),
    ("personDetected", "person"),
    ("petDetected", "pet"),
    ("cryingDetected", "crying"),
    ("soundDetected", "sound"),
    ("packageDelivered", "package_delivered"),
    ("packageStranded", "package_stranded"),
    ("packageTaken", "package_taken"),
    ("snooze", "snooze"),
    ("vehicleDetected", "vehicle"),
])
def test_role_for_unique_id_matches_every_known_suffix(eufy, suffix, expected):
    assert eufy._role_for_unique_id(_uid(suffix)) == expected


def test_role_for_unique_id_unrecognised_suffix_is_none(eufy):
    assert eufy._role_for_unique_id(_uid("someFutureFeature")) is None
    assert eufy._role_for_unique_id("") is None
    assert eufy._role_for_unique_id(None) is None


def test_stranger_does_not_also_match_plain_person_role(eufy):
    # A naive .endswith("_device_personDetected") would ALSO match
    # "...stranger_device_personDetected"-shaped strings if suffix order were
    # wrong; confirm the real suffixes are checked distinctly.
    assert eufy._role_for_unique_id(_uid("strangerPersonDetected")) == "stranger"
    assert eufy._role_for_unique_id(_uid("personDetected")) == "person"


# ── is_eufy_camera ───────────────────────────────────────────────────────────

def test_is_eufy_camera_true_for_eufy_platform(eufy, fake_hass):
    _set_registry(eufy, FRONT_DOOR_BELL_FAMILY)
    assert eufy.is_eufy_camera(fake_hass, "camera.front_door_bell") is True


def test_is_eufy_camera_false_for_other_platform(eufy, fake_hass):
    _set_registry(eufy, [_Entry("camera.nest_cam", "nest", "dev2", "nest_uid")])
    assert eufy.is_eufy_camera(fake_hass, "camera.nest_cam") is False


def test_is_eufy_camera_false_for_unknown_entity(eufy, fake_hass):
    _set_registry(eufy, [])
    assert eufy.is_eufy_camera(fake_hass, "camera.does_not_exist") is False


def test_is_eufy_camera_never_raises_on_registry_error(eufy, fake_hass):
    eufy._er_mod.async_get = lambda hass: (_ for _ in ()).throw(RuntimeError("boom"))
    assert eufy.is_eufy_camera(fake_hass, "camera.front_door_bell") is False


# ── discover_roles ───────────────────────────────────────────────────────────

def test_discover_roles_maps_the_full_real_sibling_family(eufy, fake_hass):
    _set_registry(eufy, FRONT_DOOR_BELL_FAMILY)
    roles = eufy.discover_roles(fake_hass, "camera.front_door_bell")
    assert roles == {
        "ringing": "binary_sensor.front_door_bell_ringing",
        "person": "binary_sensor.front_door_bell_person_detected",
        "stranger": "binary_sensor.front_door_bell_stranger_person_detected",
        "identity": "binary_sensor.front_door_bell_identity_person_detected",
        "pet": "binary_sensor.front_door_bell_pet_detected",
        "crying": "binary_sensor.front_door_bell_crying_detected",
        "sound": "binary_sensor.front_door_bell_sound_detected",
        "package_delivered": "binary_sensor.front_door_bell_package_delivered",
        "package_stranded": "binary_sensor.front_door_bell_package_stranded",
        "package_taken": "binary_sensor.front_door_bell_package_taken",
        "snooze": "binary_sensor.front_door_bell_snooze",
        "event_image": "image.front_door_bell_event_image",
    }


def test_discover_roles_ignores_debug_and_camera_entities(eufy, fake_hass):
    _set_registry(eufy, FRONT_DOOR_BELL_FAMILY)
    roles = eufy.discover_roles(fake_hass, "camera.front_door_bell")
    assert "debug" not in roles
    assert all(v != "camera.front_door_bell" for v in roles.values())


def test_discover_roles_only_matches_same_device(eufy, fake_hass):
    other_device = FRONT_DOOR_BELL_FAMILY + [
        _Entry("binary_sensor.backyard_ringing", "eufy_security", "different-device", _uid("ringing")),
    ]
    _set_registry(eufy, other_device)
    roles = eufy.discover_roles(fake_hass, "camera.front_door_bell")
    assert roles["ringing"] == "binary_sensor.front_door_bell_ringing"


def test_discover_roles_empty_for_non_eufy_camera(eufy, fake_hass):
    _set_registry(eufy, [_Entry("camera.nest_cam", "nest", "dev2", "nest_uid")])
    assert eufy.discover_roles(fake_hass, "camera.nest_cam") == {}


def test_discover_roles_empty_for_unregistered_camera(eufy, fake_hass):
    _set_registry(eufy, [])
    assert eufy.discover_roles(fake_hass, "camera.ghost") == {}


def test_discover_roles_never_raises(eufy, fake_hass):
    eufy._er_mod.async_get = lambda hass: (_ for _ in ()).throw(RuntimeError("boom"))
    assert eufy.discover_roles(fake_hass, "camera.front_door_bell") == {}


def test_discover_roles_survives_a_rename(eufy, fake_hass):
    # entity_id changed (user renamed it); unique_id (immutable) unchanged.
    renamed = [
        _Entry("camera.porch_cam", "eufy_security", DEVICE, _uid("camera")),
        _Entry("binary_sensor.porch_cam_ringing", "eufy_security", DEVICE, _uid("ringing")),
    ]
    _set_registry(eufy, renamed)
    roles = eufy.discover_roles(fake_hass, "camera.porch_cam")
    assert roles["ringing"] == "binary_sensor.porch_cam_ringing"


def test_discover_roles_picks_up_vehicle_on_a_driveway_style_camera(eufy, fake_hass):
    # Real-world case: an outdoor camera with no doorbell/package hardware but
    # with vehicle_detected (verified live against an actual eufy_security
    # driveway camera, unique_id "..._device_vehicleDetected").
    driveway = [
        _Entry("camera.driveway", "eufy_security", DEVICE, _uid("camera")),
        _Entry("binary_sensor.driveway_person_detected", "eufy_security", DEVICE, _uid("personDetected")),
        _Entry("binary_sensor.driveway_vehicle_detected", "eufy_security", DEVICE, _uid("vehicleDetected")),
    ]
    _set_registry(eufy, driveway)
    roles = eufy.discover_roles(fake_hass, "camera.driveway")
    assert roles["vehicle"] == "binary_sensor.driveway_vehicle_detected"
    assert "ringing" not in roles
    assert "package_delivered" not in roles
