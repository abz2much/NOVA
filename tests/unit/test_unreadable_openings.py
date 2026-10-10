"""Gap 2 (8.25.0): a door or window that cannot be read is never taken as
closed.

Lockdown names it ("I can't tell if ... is closed") and never says "fully
secured"; the world model lists it and secure() is unknown. Intrusion is
unchanged: an unreadable sensor is still not a way in. All state is fake.
"""
import pytest

from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401

ALARM = "alarm_control_panel.home_security"


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_lockdown_names_a_door_it_cannot_read(cc, fake_hass, state):
    fake_hass.states.set("binary_sensor.back_door", state, device_class="door",
                         friendly_name="Back door")
    fake_hass.states.set("lock.front", "locked")
    action = await cc.LockdownManager(fake_hass, {}).engage("alarm armed")
    assert "I can't tell if Back door is closed" in action["message"]
    assert "fully secured" not in action["message"]


async def test_lockdown_names_it_alongside_what_it_did(cc, fake_hass):
    fake_hass.states.set("binary_sensor.kitchen_window", "unavailable",
                         device_class="window", friendly_name="Kitchen window")
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    action = await cc.LockdownManager(fake_hass, {}).engage("alarm armed")
    fake_hass.close_pending()
    assert "Front" in action["message"]
    assert "I can't tell if Kitchen window is closed" in action["message"]


async def test_an_unreadable_garage_cover_is_named(cc, fake_hass):
    fake_hass.states.set("cover.garage", "unavailable", device_class="garage",
                         friendly_name="Garage")
    action = await cc.LockdownManager(fake_hass, {}).engage("alarm armed")
    assert "I can't tell if Garage is closed" in action["message"]


async def test_a_readable_house_is_still_fully_secured(cc, fake_hass):
    fake_hass.states.set("binary_sensor.back_door", "off", device_class="door",
                         friendly_name="Back door")
    fake_hass.states.set("lock.front", "locked")
    action = await cc.LockdownManager(fake_hass, {}).engage("alarm armed")
    assert "fully secured" in action["message"]


async def test_an_unreadable_fridge_or_shed_is_not_named(cc, load, fake_hass, monkeypatch):
    # Only ways into the house count, as for open doors.
    fake_hass.states.set("binary_sensor.fridge_door", "unavailable", device_class="door",
                         friendly_name="Fridge door")
    action = await cc.LockdownManager(fake_hass, {}).engage("alarm armed")
    assert "Fridge" not in action["message"] and "fully secured" in action["message"]


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
def test_the_world_model_lists_it_and_is_not_secure(load, fake_hass, state):
    world = load("world")
    fake_hass.states.set("binary_sensor.back_door", state, device_class="door")
    fake_hass.states.set("lock.front", "locked")
    w = world.read(fake_hass, {})
    assert w.unreadable_openings == ("binary_sensor.back_door",)
    assert w.secure() == "unknown"


def test_the_world_model_is_secure_when_everything_reads_shut(load, fake_hass):
    world = load("world")
    fake_hass.states.set("binary_sensor.back_door", "off", device_class="door")
    fake_hass.states.set("lock.front", "locked")
    w = world.read(fake_hass, {})
    assert w.unreadable_openings == () and w.secure() is True


# ── intrusion is unchanged ──────────────────────────────────────────────────

def test_an_unreadable_sensor_is_still_not_a_way_in(cc, fake_hass):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("binary_sensor.back_door", "unavailable", device_class="door")
    assert safety._open_entry() is None


async def test_motion_with_only_an_unreadable_door_raises_no_away_alert(cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("binary_sensor.back_door", "unavailable", device_class="door")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    actions = await safety.tick(sleeping=False, anyone_home=False)
    fake_hass.close_pending()
    assert [a for a in actions if a["type"].startswith("intrusion")] == []


async def test_armed_away_still_alerts_with_an_unreadable_door(cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("binary_sensor.back_door", "unavailable", device_class="door")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    actions = await safety.tick(sleeping=False, anyone_home=False)
    fake_hass.close_pending()
    assert [a["type"] for a in actions] == ["intrusion_investigating"]


def test_an_unreadable_lock_alone_is_unknown_and_an_open_door_is_not_secure(load, fake_hass):
    world = load("world")
    fake_hass.states.set("lock.shed", "unavailable")
    assert world.read(fake_hass, {}).secure() == "unknown"
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    assert world.read(fake_hass, {}).secure() is False      # known open beats unknown
