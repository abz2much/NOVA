"""A parked car, an animal or a package is not a person (8.14.0).

Camera integrations such as Frigate expose one binary_sensor per detected
object class, for example binary_sensor.garage_car_occupancy or
binary_sensor.kitchen_dog_occupancy, with device_class occupancy. Those sensors
say an object is in view, not that a person is there, so they must never make
Nova think someone is home or in a room. A person sensor, and ordinary motion,
occupancy and presence sensors, still count exactly as before.

is_object_sensor() is the one small pure check. The other tests feed object
sensors through the real code paths that decide "someone is here"."""
import sys
import types

import pytest

from cognitive_safety_kit import (  # noqa: F401  (fixtures)
    _isolated_core, cc, clock, service_calls)
from fakes import FakeEntityRegistry, FakeRegistryEntry


# ── the pure check ─────────────────────────────────────────────────────────

@pytest.fixture
def ef(load):
    return load("entity_filter")


@pytest.mark.parametrize("eid, name", [
    ("binary_sensor.garage_car_occupancy", None),
    ("binary_sensor.driveway_car_occupancy", "Driveway car occupancy"),
    ("binary_sensor.front_truck_occupancy", None),
    ("binary_sensor.kitchen_dog_occupancy", None),
    ("binary_sensor.hall_cat_occupancy", None),
    ("binary_sensor.garden_bird_occupancy", None),
    ("binary_sensor.porch_package_occupancy", None),
    ("binary_sensor.front_door_package_detected", None),
    ("binary_sensor.doorbell_pet_detected", None),
    ("binary_sensor.doorbell_motion_detection_type_vehicle", None),
    ("binary_sensor.x", "Back yard dog motion"),
])
def test_object_sensors(ef, eid, name):
    assert ef.is_object_sensor(eid, name) is True


@pytest.mark.parametrize("eid, name", [
    ("binary_sensor.garage_person_occupancy", None),
    ("binary_sensor.kitchen_motion", "Kitchen motion"),
    ("binary_sensor.lounge_presence", "Lounge presence"),
    ("binary_sensor.office_occupancy", "Office occupancy"),
    ("binary_sensor.car_park_lights_motion", None),        # "car" not before a presence word
    ("binary_sensor.carport_motion", "Carport motion"),    # carport is a place, not a car
    ("binary_sensor.dog_door", "Dog door"),                # a door, not a detection
    ("binary_sensor.catherine_room_occupancy", None),      # a name, not a cat
    ("binary_sensor.garage_all_occupancy", None),          # "all" may be a person
    (None, None), ("", ""),
])
def test_not_object_sensors(ef, eid, name):
    assert ef.is_object_sensor(eid, name) is False


def test_never_raises(ef):
    assert ef.is_object_sensor(object(), 12) is False


# ── anyone home and occupied rooms (audio routing) ─────────────────────────

@pytest.fixture
def registry(monkeypatch):
    er = sys.modules["homeassistant.helpers.entity_registry"]
    ar = sys.modules["homeassistant.helpers.area_registry"]
    reg = FakeEntityRegistry()
    reg.areas = {}
    monkeypatch.setattr(er, "async_get", lambda hass: reg)
    monkeypatch.setattr(ar, "async_get", lambda hass: types.SimpleNamespace(
        async_get_area=lambda area_id: (types.SimpleNamespace(name=reg.areas[area_id])
                                        if area_id in reg.areas else None)))
    return reg


OBJECTS = ("binary_sensor.garage_car_occupancy", "binary_sensor.kitchen_dog_occupancy",
           "binary_sensor.porch_package_occupancy")


def _objects_on(hass, registry=None, area="garage"):
    for eid in OBJECTS:
        hass.states.set(eid, "on", device_class="occupancy")
        if registry is not None:
            registry.add(FakeRegistryEntry(eid, "frigate", area_id=area))


def test_objects_alone_do_not_make_anyone_home(load, fake_hass):
    ar = load("audio_routing")
    _objects_on(fake_hass)
    assert ar.anyone_home(fake_hass) is False


def test_a_person_sensor_still_makes_someone_home(load, fake_hass):
    ar = load("audio_routing")
    _objects_on(fake_hass)
    fake_hass.states.set("binary_sensor.garage_person_occupancy", "on", device_class="occupancy")
    assert ar.anyone_home(fake_hass) is True


def test_ordinary_motion_still_makes_someone_home(load, fake_hass):
    ar = load("audio_routing")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert ar.anyone_home(fake_hass) is True


def test_objects_do_not_make_a_room_occupied(load, fake_hass, registry):
    ar = load("audio_routing")
    _objects_on(fake_hass, registry)
    assert ar.is_area_occupied(fake_hass, "garage") is False
    assert ar.currently_occupied_areas(fake_hass) == []


def test_a_person_and_a_car_make_the_room_occupied_once(load, fake_hass, registry):
    ar = load("audio_routing")
    _objects_on(fake_hass, registry)
    fake_hass.states.set("binary_sensor.garage_person_occupancy", "on", device_class="occupancy")
    registry.add(FakeRegistryEntry("binary_sensor.garage_person_occupancy", "frigate", area_id="garage"))
    assert ar.currently_occupied_areas(fake_hass) == ["garage"]


# ── the presence summary (what the model is told) ──────────────────────────

def test_objects_are_not_occupied_areas_in_the_summary(load, fake_hass):
    pr = load("presence")
    for eid, name in (("binary_sensor.garage_car_occupancy", "Garage car occupancy"),
                      ("binary_sensor.kitchen_presence", "Kitchen presence")):
        fake_hass.states.set(eid, "on", device_class="occupancy", friendly_name=name)
    assert list(pr.get_presence_summary(fake_hass)["rooms"]) == ["kitchen"]


# ── the dark room offer ────────────────────────────────────────────────────

def test_a_parked_car_does_not_make_a_dark_garage_occupied(cc, fake_hass, registry):
    proactive = cc.ProactiveManager(fake_hass, {})
    registry.areas["garage"] = "Garage"
    fake_hass.states.set("binary_sensor.garage_car_occupancy", "on", device_class="occupancy")
    registry.add(FakeRegistryEntry("binary_sensor.garage_car_occupancy", "frigate", area_id="garage"))
    assert proactive._area_has_presence("garage", registry) is False
    fake_hass.states.set("binary_sensor.garage_person_occupancy", "on", device_class="occupancy")
    registry.add(FakeRegistryEntry("binary_sensor.garage_person_occupancy", "frigate", area_id="garage"))
    assert proactive._area_has_presence("garage", registry) is True


# ── intrusion: indoor motion that may start an investigation ───────────────

@pytest.fixture
def safety(cc, fake_hass, monkeypatch):
    cam = types.ModuleType("jc.camera")
    cam.active_camera_states = lambda hass: []
    monkeypatch.setitem(sys.modules, "jc.camera", cam)
    return cc.SafetyManager(fake_hass, {})


def test_an_indoor_pet_camera_does_not_count_as_indoor_motion(safety, fake_hass):
    fake_hass.states.set("binary_sensor.kitchen_dog_occupancy", "on", device_class="occupancy")
    fake_hass.states.set("binary_sensor.hall_package_occupancy", "on", device_class="occupancy")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    fake_hass.states.set("binary_sensor.kitchen_person_occupancy", "on", device_class="occupancy")
    got = [e for e, _ in safety._qualifying_motion(False)]
    # Real motion and a person still count, exactly as before.
    assert sorted(got) == ["binary_sensor.hall_motion", "binary_sensor.kitchen_person_occupancy"]


# ── intrusion, end to end through SafetyManager.tick ───────────────────────
# Same setup as test_cognitive_core_intrusion: residents tracked away and a
# door open, so indoor motion is corroborated and starts an investigation.

@pytest.fixture
def intrusion(cognitive_core, fake_hass):
    fake_hass.states.set("person.resident", "not_home")
    fake_hass.states.set("device_tracker.resident_phone", "not_home")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    manager = cognitive_core.SafetyManager(fake_hass, {"honorific": "sir"})

    async def _run():
        actions = await manager.tick(sleeping=False, anyone_home=False)
        fake_hass.close_pending()
        return [a for a in actions if str(a.get("type", "")).startswith("intrusion")]
    return _run


async def test_real_motion_still_starts_an_intrusion_check(intrusion, fake_hass):
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    found = await intrusion()
    assert [a["type"] for a in found] == ["intrusion_investigating"]


async def test_a_person_sensor_still_starts_an_intrusion_check(intrusion, fake_hass):
    fake_hass.states.set("binary_sensor.kitchen_person_occupancy", "on", device_class="occupancy")
    found = await intrusion()
    assert [a["type"] for a in found] == ["intrusion_investigating"]


async def test_object_sensors_alone_start_no_intrusion_check(intrusion, fake_hass):
    for eid in ("binary_sensor.kitchen_dog_occupancy", "binary_sensor.hall_car_occupancy",
                "binary_sensor.hall_package_occupancy"):
        fake_hass.states.set(eid, "on", device_class="occupancy")
    assert await intrusion() == []
