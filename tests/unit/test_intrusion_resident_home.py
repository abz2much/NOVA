"""Residents home, alarm armed home, someone walks to the kitchen at night.

Reproduces 9 Oct 2026, 23:15: Alarmo was armed_home and never triggered, the
residents were home, the fridge door opened and the kitchen and keypad motion
sensors fired. Nova announced "Intrusion confirmed — someone is moving inward
through the house from the point of entry". None of that is a stranger coming
in, so it must never confirm an intrusion.

All state here is fake. Nothing touches a real alarm or lock.
"""
import pytest

ALARM = "alarm_control_panel.home_security"


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture
def clock(cc, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(cc.time, "time", lambda: t["now"])
    return t


def _evening(hass):
    """The house as it was: residents home, armed home, fridge door open,
    kitchen and keypad motion. Every exterior door and window is shut."""
    hass.states.set("person.abi", "home")
    hass.states.set("person.rachel", "home")
    hass.states.set(ALARM, "armed_home")
    hass.states.set("binary_sensor.entry_front_door_cs_door", "off", device_class="door")
    hass.states.set("binary_sensor.laundry_room_back_door_cs_door", "off", device_class="door")
    hass.states.set("binary_sensor.fridge_fridge_door", "on", device_class="door",
                    friendly_name="Fridge Fridge door")
    hass.states.set("binary_sensor.fridge_any_door_open", "on", device_class="door",
                    friendly_name="Fridge Any Door Open")
    hass.states.set("binary_sensor.kitchen_motion", "on", device_class="motion",
                    friendly_name="Kitchen Motion")


async def _walk_about(safety, hass, clock, sleeping):
    """Tick the way the 30 second loop does while the resident moves from the
    kitchen to the keypad and back. Returns every intrusion action raised."""
    seen = []

    async def tick():
        actions = await safety.tick(sleeping=sleeping, anyone_home=True)
        hass.close_pending()
        seen.extend(a for a in actions if str(a.get("type", "")).startswith("intrusion"))

    await tick()
    for step in range(6):
        clock["now"] += 30
        hass.states.set("binary_sensor.keypad_ms", "on" if step % 2 == 0 else "off",
                        device_class="motion", friendly_name="Keypad MS")
        await tick()
    return seen


async def test_armed_home_residents_home_kitchen_walk_never_confirms(
        cc, clock, fake_hass):
    # "Only when confined" on: armed home makes Nova watch, residents awake.
    safety = cc.SafetyManager(fake_hass, {
        "honorific": "sir", "security_alarm_entity": ALARM,
        "intrusion_requires_confinement": True})
    _evening(fake_hass)
    seen = await _walk_about(safety, fake_hass, clock, sleeping=False)
    assert not [a for a in seen if a["type"] == "intrusion_confirmed"], seen


async def test_asleep_residents_home_kitchen_walk_never_confirms(
        cc, clock, fake_hass):
    # The automatic default: the household is marked asleep.
    safety = cc.SafetyManager(fake_hass, {
        "honorific": "sir", "security_alarm_entity": ALARM})
    _evening(fake_hass)
    seen = await _walk_about(safety, fake_hass, clock, sleeping=True)
    assert not [a for a in seen if a["type"] == "intrusion_confirmed"], seen


# ── fix A: what counts as a way in ──────────────────────────────────────────

@pytest.mark.parametrize("eid,name", [
    ("binary_sensor.fridge_fridge_door", "Fridge Fridge door"),
    ("binary_sensor.kitchen_freezer", "Chest Freezer"),
    ("binary_sensor.oven_door", "Oven door"),
    ("binary_sensor.dishwasher_door", "Dishwasher door"),
    ("binary_sensor.washing_machine_door", "Washing Machine door"),
    ("binary_sensor.dryer_door", "Dryer door"),
    ("binary_sensor.microwave_door", "Microwave"),
    ("binary_sensor.hall_cabinet", "Hall cabinet"),
])
def test_appliance_door_is_never_an_entry_point(cc, fake_hass, eid, name):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set(eid, "on", device_class="door", friendly_name=name)
    assert safety._open_entry() is None
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door",
                         friendly_name="Front door")
    assert safety._open_entry() == "binary_sensor.front_door"


def test_real_doors_are_not_mistaken_for_appliances(load):
    ef = load("entity_filter")
    for eid, name in (("binary_sensor.laundry_room_back_door_cs_door", "Laundry Room Back Door"),
                      ("binary_sensor.entry_front_door_cs_door", "Entry Front Door"),
                      ("binary_sensor.kitchen_window", "Kitchen Window")):
        assert ef.is_appliance_opening(eid, name) is False


def test_exclude_list_is_respected_for_entry_points(cc, load, fake_hass, monkeypatch):
    ef = load("entity_filter")
    monkeypatch.setattr(ef, "_exclusion_config",
                        lambda hass: ({"binary_sensor.front_door"}, set(), set()))
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    assert safety._open_entry() is None
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    assert safety._open_entry() == "binary_sensor.back_door"


# ── fix B: residents home need stronger proof ───────────────────────────────

async def _route(safety, hass, clock, sleeping, anyone_home):
    """Motion starts in the hall, then spreads to the kitchen and landing for
    three minutes: what both a resident and an intruder look like."""
    seen = []

    async def tick():
        actions = await safety.tick(sleeping=sleeping, anyone_home=anyone_home)
        hass.close_pending()
        seen.extend(a for a in actions if str(a.get("type", "")).startswith("intrusion"))

    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    await tick()
    for eid in ("binary_sensor.kitchen_motion", "binary_sensor.landing_motion",
                "binary_sensor.hall_motion", "binary_sensor.kitchen_motion"):
        clock["now"] += 40
        hass.states.set(eid, "on", device_class="motion")
        await tick()
    return seen


def _confirmed(seen):
    return [a for a in seen if a["type"] == "intrusion_confirmed"]


async def test_resident_through_a_real_open_door_never_confirms_on_motion(
        cc, clock, fake_hass):
    # The back door really is open (someone stepped out to the garden).
    safety = cc.SafetyManager(fake_hass, {
        "honorific": "sir", "security_alarm_entity": ALARM,
        "intrusion_requires_confinement": True})
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set(ALARM, "armed_home")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    seen = await _route(safety, fake_hass, clock, sleeping=False, anyone_home=True)
    assert seen and not _confirmed(seen), seen
    assert not any("no one is home" in a["message"] for a in seen)


async def test_asleep_with_a_real_open_door_never_confirms_on_motion(
        cc, clock, fake_hass):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set(ALARM, "armed_night")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    seen = await _route(safety, fake_hass, clock, sleeping=True, anyone_home=True)
    assert seen and not _confirmed(seen), seen


async def test_alarm_triggered_still_confirms_when_residents_are_home(
        cc, clock, fake_hass):
    safety = cc.SafetyManager(fake_hass, {
        "honorific": "sir", "security_alarm_entity": ALARM,
        "intrusion_requires_confinement": True})
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set(ALARM, "armed_home")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    first = await safety.tick(sleeping=False, anyone_home=True)
    fake_hass.close_pending()
    assert any(a["type"] == "intrusion_investigating" for a in first)
    clock["now"] += 30
    fake_hass.states.set(ALARM, "triggered")
    actions = await safety.tick(sleeping=False, anyone_home=True)
    fake_hass.close_pending()
    conf = _confirmed(actions)
    assert len(conf) == 1 and "alarm has gone off" in conf[0]["message"]


async def test_armed_away_still_confirms_on_motion(cc, clock, fake_hass):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    seen = await _route(safety, fake_hass, clock, sleeping=False, anyone_home=False)
    assert len(_confirmed(seen)) == 1, seen


async def test_everyone_away_still_confirms_on_motion(cc, clock, fake_hass):
    # No alarm at all: tracked away plus an open door, as before.
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    seen = await _route(safety, fake_hass, clock, sleeping=False, anyone_home=False)
    conf = _confirmed(seen)
    assert len(conf) == 1 and "while no one is home" in conf[0]["message"], seen
