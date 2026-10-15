"""Room commands act on exactly the room asked for (8.49.40).

get_area_devices took the first area whose name contained the words, so
"office" could land on "Back Office", and an empty name took the first area
of all. It also put an entity in its device's room even when the entity had
been given a room of its own. Room control (bulk_control with an area) read
that list, which stops at 30 entities, so in a busy room some lights were
never switched.
"""
import json
import sys
from types import SimpleNamespace as NS

import pytest


@pytest.fixture
def house(load, monkeypatch, fake_hass):
    load("agent")
    home = sys.modules["jc.agent_runtime.capabilities.home"]
    areas, entities, devices = [], {}, {}
    monkeypatch.setattr(sys.modules["homeassistant.helpers.area_registry"], "async_get",
                        lambda hass: NS(async_list_areas=lambda: list(areas)))
    monkeypatch.setattr(sys.modules["homeassistant.helpers.entity_registry"], "async_get",
                        lambda hass: NS(entities=entities))
    monkeypatch.setattr(sys.modules["homeassistant.helpers.device_registry"], "async_get",
                        lambda hass: NS(async_get=devices.get))

    def area(area_id, name):
        areas.append(NS(id=area_id, name=name))

    def entity(eid, *, room=None, device=None, state="on"):
        entities[eid] = NS(entity_id=eid, domain=eid.split(".")[0],
                           area_id=room, device_id=device)
        fake_hass.states.set(eid, state, friendly_name=eid.split(".")[1])

    def device(dev_id, room):
        devices[dev_id] = NS(area_id=room)

    return NS(mod=home, hass=fake_hass, area=area, entity=entity, device=device)


async def _listed(house, name):
    return json.loads(await house.mod._exec_get_area_devices(house.hass, {"area_name": name}))


async def test_an_exact_room_name_wins_over_a_longer_one(house):
    house.area("back", "Back Office")
    house.area("office", "Office")
    house.entity("light.desk", room="office")
    house.entity("light.shelf", room="back")
    got = await _listed(house, "office")
    assert got["area"] == "Office"
    assert [d["entity_id"] for d in got["devices"]] == ["light.desk"]


async def test_a_partial_name_still_finds_a_room(house):
    house.area("back", "Back Office")
    assert (await _listed(house, "back"))["area"] == "Back Office"


async def test_an_empty_room_name_finds_no_room(house):
    house.area("hall", "Hall")
    assert await _listed(house, "") == {"error": "Area '' not found"}


async def test_an_entity_in_its_own_room_is_not_listed_in_its_devices_room(house):
    house.area("lounge", "Lounge")
    house.area("kitchen", "Kitchen")
    house.device("hub", "lounge")
    house.entity("light.moved", device="hub", room="kitchen")
    house.entity("light.stayed", device="hub")
    lounge = [d["entity_id"] for d in (await _listed(house, "lounge"))["devices"]]
    kitchen = [d["entity_id"] for d in (await _listed(house, "kitchen"))["devices"]]
    assert lounge == ["light.stayed"]
    assert kitchen == ["light.moved"]


async def test_the_list_stops_at_30_but_room_control_sees_every_entity(house):
    house.area("kitchen", "Kitchen")
    for n in range(35):
        house.entity(f"sensor.s{n:02d}", room="kitchen")
    house.entity("light.under_cupboard", room="kitchen")
    listed = await _listed(house, "kitchen")
    assert listed["device_count"] == 36 and len(listed["devices"]) == 30
    assert "light.under_cupboard" not in [d["entity_id"] for d in listed["devices"]]
    assert house.mod.area_entity_ids(house.hass, "kitchen", "light") == ["light.under_cupboard"]


def test_room_control_with_an_unknown_room_finds_nothing(house):
    house.area("kitchen", "Kitchen")
    house.entity("light.a", room="kitchen")
    assert house.mod.area_entity_ids(house.hass, "garage", "light") == []


async def test_turning_off_a_busy_rooms_lights_reaches_every_light(house, load, tmp_path,
                                                                    monkeypatch):
    monkeypatch.setattr(load("action_log"), "_DEFAULT_DB", str(tmp_path / "audit.db"))
    control = sys.modules["jc.agent_runtime.capabilities.control"]
    house.area("kitchen", "Kitchen")
    for n in range(35):
        house.entity(f"sensor.s{n:02d}", room="kitchen")
    house.entity("light.under_cupboard", room="kitchen")
    await control._exec_bulk_control(
        house.hass, {"domain": "light", "action": "turn_off", "area_name": "kitchen"})
    for pending in house.hass._tasks:     # the follow up check is not under test
        pending.close()
    switched = [c for c in house.hass.service_calls if c[:2] == ("light", "turn_off")]
    assert [c[2]["entity_id"] for c in switched] == ["light.under_cupboard"]
