"""Nova reads the home's own labels (8.82.0).

Most homes label devices, not entities, and an entity inherits its device's
labels. Three labels tell Nova what a sensor is, and they beat any guess from
its name:

* Appliance: never a way into the house (a fridge or oven door).
* Entry Point: an outside door or window, a way in.
* Group: a sensor that repeats others (a floor or room group). Never a way
  in and never a person's motion, so one person is not counted twice. A
  Home Assistant group is recognised by its member list even unlabelled.

Matched by label id or name, ignoring case, spaces and hyphens.
"""
import types

import pytest


@pytest.fixture
def ef(load):
    return load("entity_filter")


@pytest.fixture
def household(load):
    return load("household")


@pytest.fixture
def labels(ef, monkeypatch):
    """labels(entity_id, entity=[...], device=[...], names={id: name})."""
    entities, devices, names = {}, {}, {}

    def _set(entity_id, entity=(), device=(), label_names=None):
        dev_id = f"dev_{entity_id}" if device else None
        entities[entity_id] = types.SimpleNamespace(labels=set(entity), device_id=dev_id)
        if dev_id:
            devices[dev_id] = types.SimpleNamespace(labels=set(device))
        names.update(label_names or {})

    monkeypatch.setattr(ef, "er", types.SimpleNamespace(async_get=lambda _h: types.SimpleNamespace(
        async_get=entities.get)))
    monkeypatch.setattr(ef, "dr", types.SimpleNamespace(async_get=lambda _h: types.SimpleNamespace(
        async_get=devices.get)))
    monkeypatch.setattr(ef, "_lr", types.SimpleNamespace(async_get=lambda _h: types.SimpleNamespace(
        async_get_label=lambda lid: types.SimpleNamespace(name=names[lid]) if lid in names else None)))
    return _set


def _state(hass, eid, state="on", device_class="door", members=None):
    attrs = {"device_class": device_class}
    if members:
        attrs["entity_id"] = members       # how a Home Assistant group lists them
    hass.states.async_set(eid, state, attrs)
    return hass.states.get(eid)


# ── reading roles ───────────────────────────────────────────────────────────

def test_roles_come_from_the_entity_and_its_device(ef, labels, fake_hass):
    labels("binary_sensor.a", entity=["entry_point"], device=["appliance", "sensor"])
    assert ef.label_roles(fake_hass, "binary_sensor.a") >= {"entry_point", "appliance"}


@pytest.mark.parametrize("label_id,name", [
    ("lbl_1", "Entry Point"), ("lbl_2", "entry-point"), ("lbl_3", "ENTRY POINT"),
    ("entry_point", None),
])
def test_a_role_matches_by_id_or_name(ef, labels, fake_hass, label_id, name):
    labels("binary_sensor.a", device=[label_id], label_names={label_id: name} if name else {})
    assert "entry_point" in ef.label_roles(fake_hass, "binary_sensor.a")


def test_no_registry_entry_or_a_broken_registry_means_no_roles(ef, labels, fake_hass, monkeypatch):
    assert ef.label_roles(fake_hass, "binary_sensor.unknown") == set()

    def boom(_h):
        raise RuntimeError("registry gone")
    monkeypatch.setattr(ef, "er", types.SimpleNamespace(async_get=boom))
    assert ef.label_roles(fake_hass, "binary_sensor.a") == set()


def test_a_device_label_now_excludes_too(ef, labels, fake_hass, nova_runtime):
    nova_runtime(fake_hass, {"excluded_labels": ["Ghost Entry"]})
    labels("binary_sensor.a", device=["ghost_entry"], label_names={"ghost_entry": "Ghost Entry"})
    assert ef.is_excluded(fake_hass, "binary_sensor.a") is True


# ── a way in ────────────────────────────────────────────────────────────────

def test_an_appliance_label_is_never_a_way_in(household, labels, fake_hass):
    labels("binary_sensor.kitchen_door_2", device=["appliance"])
    assert household.is_way_in(fake_hass, _state(fake_hass, "binary_sensor.kitchen_door_2")) is False


def test_an_entry_point_label_is_a_way_in_whatever_the_name(household, labels, fake_hass):
    # "Cabinet" would be read as an appliance from the name alone.
    labels("binary_sensor.cabinet_room_window", entity=["entry_point"])
    state = _state(fake_hass, "binary_sensor.cabinet_room_window", device_class="window")
    assert household.is_way_in(fake_hass, state) is True


def test_a_group_label_is_never_a_way_in(household, labels, fake_hass):
    labels("binary_sensor.ground_floor_doors", entity=["group"])
    assert household.is_way_in(fake_hass, _state(fake_hass, "binary_sensor.ground_floor_doors")) is False


def test_an_unlabelled_group_is_known_by_its_members(household, labels, fake_hass):
    state = _state(fake_hass, "binary_sensor.ground_floor_doors",
                   members=["binary_sensor.front_door", "binary_sensor.back_door"])
    assert household.is_way_in(fake_hass, state) is False


def test_an_excluded_entry_point_stays_excluded(household, labels, fake_hass, nova_runtime):
    nova_runtime(fake_hass, {"excluded_entities": ["binary_sensor.front_door"]})
    labels("binary_sensor.front_door", entity=["entry_point"])
    assert household.is_way_in(fake_hass, _state(fake_hass, "binary_sensor.front_door")) is False


def test_without_labels_the_old_rules_stand(household, labels, fake_hass):
    assert household.is_way_in(fake_hass, _state(fake_hass, "binary_sensor.front_door")) is True
    assert household.is_way_in(fake_hass, _state(fake_hass, "binary_sensor.fridge_door")) is False


# ── a person's motion ───────────────────────────────────────────────────────

def test_a_group_motion_sensor_is_not_a_person(household, labels, fake_hass):
    labels("binary_sensor.ground_floor_ms", entity=["group"])
    state = _state(fake_hass, "binary_sensor.ground_floor_ms", device_class="motion")
    assert household.is_person_motion(fake_hass, state) is False


def test_an_unlabelled_motion_group_is_known_by_its_members(household, labels, fake_hass):
    state = _state(fake_hass, "binary_sensor.kitchen_motion", device_class="motion",
                   members=["binary_sensor.kitchen_ps", "binary_sensor.kitchen_ms"])
    assert household.is_person_motion(fake_hass, state) is False


def test_an_ordinary_motion_sensor_is_still_a_person(household, labels, fake_hass):
    labels("binary_sensor.kitchen_ps", device=["presence_occupancy", "sensor"])
    state = _state(fake_hass, "binary_sensor.kitchen_ps", device_class="occupancy")
    assert household.is_person_motion(fake_hass, state) is True


# ── end to end: one person never makes two rooms ────────────────────────────

async def test_one_person_and_their_floor_group_are_one_zone(cognitive_core, labels, fake_hass):
    """Asleep with the back door open: the kitchen sensor and the Ground
    Floor group both fire for one person. Before, the group (with no area)
    was a second zone."""
    safety = cognitive_core.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.kitchen_ps", "on", device_class="occupancy")
    fake_hass.states.async_set("binary_sensor.ground_floor_ms", "on", {
        "device_class": "motion", "entity_id": ["binary_sensor.kitchen_ps"]})
    found = [eid for eid, _ in safety._qualifying_motion(sleeping=True)]
    assert found == ["binary_sensor.kitchen_ps"]
