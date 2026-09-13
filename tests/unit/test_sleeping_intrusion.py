"""Tests for the sleeping-household intrusion check (v7.86.0).

Before this change, `_check_intrusion`'s sleeping branch fired a HIGH-urgency
"motion detected... investigating" alert on ANY qualifying indoor motion
sensor while the household was asleep — a trip to the bathroom or kitchen for
water was indistinguishable from an intruder. It now requires the same kind
of corroboration the away-branch already had: an actual breach (a ground-floor
exterior door/window open), routed through the same silent, room-by-room
investigation state machine. Plain movement with no breach produces zero
notifications; `ground_floor_areas` (new, config_flow Routing step) scopes
which areas count, falling back to every floor when unconfigured so nothing
gets LESS safe.
"""
import pytest


@pytest.fixture
def cc(load):
    return load("cognitive_core")


@pytest.fixture
def clock(cc, monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(cc.time, "time", lambda: t["now"])
    return t


@pytest.fixture
def safety(cc, fake_hass):
    return cc.SafetyManager(fake_hass, {"honorific": "sir"})


def _motion(hass, eid, on=True):
    hass.states.set(eid, "on" if on else "off", device_class="motion")


def _door_open(hass, eid="binary_sensor.kitchen_window"):
    hass.states.set(eid, "on", device_class="window")


async def _sleep_tick(safety, hass):
    actions = await safety.tick(sleeping=True, anyone_home=True)
    hass.close_pending()
    return [a for a in actions if str(a.get("type", "")).startswith("intrusion")]


async def test_plain_motion_with_no_breach_produces_no_alert(safety, fake_hass, clock):
    """The actual bug report: someone up for water/the loo triggers nothing."""
    _motion(fake_hass, "binary_sensor.hallway_motion")
    result = await _sleep_tick(safety, fake_hass)
    assert result == []
    assert safety._investigation is None


async def test_repeated_plain_motion_never_alerts(safety, fake_hass, clock):
    """Not just the first tick — ongoing ordinary movement all night stays silent."""
    for i in range(5):
        _motion(fake_hass, "binary_sensor.hallway_motion", on=(i % 2 == 0))
        clock["now"] += 30
        assert await _sleep_tick(safety, fake_hass) == []
    assert safety._investigation is None


async def test_unconfigured_ground_floor_falls_back_to_any_door(safety, fake_hass, clock):
    """No ground_floor_areas set — must stay as cautious as before this change:
    any exterior door/window open anywhere still corroborates."""
    assert safety.config.get("ground_floor_areas") in (None, [])
    _door_open(fake_hass)
    _motion(fake_hass, "binary_sensor.hallway_motion")
    result = await _sleep_tick(safety, fake_hass)
    assert len(result) == 1
    assert result[0]["type"] == "intrusion_investigating"
    assert result[0]["urgency"] == "high"
    assert safety._investigation is not None
    assert safety._investigation["trigger"] == "sleeping"


async def test_breach_outside_configured_ground_floor_does_not_alert(safety, fake_hass, clock, monkeypatch):
    """A window open UPSTAIRS must not fire when only downstairs areas are
    flagged as ground floor — scoping actually restricts, not just documents."""
    safety.config["ground_floor_areas"] = ["kitchen"]
    monkeypatch.setattr(safety, "_breach_area", lambda eid: "upstairs_landing")
    _door_open(fake_hass, "binary_sensor.upstairs_window")
    _motion(fake_hass, "binary_sensor.hallway_motion")
    result = await _sleep_tick(safety, fake_hass)
    assert result == []
    assert safety._investigation is None


async def test_breach_inside_configured_ground_floor_alerts(safety, fake_hass, clock, monkeypatch):
    safety.config["ground_floor_areas"] = ["kitchen"]
    monkeypatch.setattr(safety, "_breach_area", lambda eid: "kitchen")
    _door_open(fake_hass, "binary_sensor.kitchen_window")
    _motion(fake_hass, "binary_sensor.hallway_motion")
    result = await _sleep_tick(safety, fake_hass)
    assert len(result) == 1
    assert result[0]["type"] == "intrusion_investigating"
    assert "asleep" in result[0]["message"]


async def test_one_alert_then_silent_investigation_while_asleep(safety, fake_hass, clock):
    _door_open(fake_hass)
    _motion(fake_hass, "binary_sensor.hallway_motion")
    first = await _sleep_tick(safety, fake_hass)
    assert len(first) == 1
    clock["now"] += 30
    # same sensor still active — no repeat alert, still just watching
    assert await _sleep_tick(safety, fake_hass) == []
    assert safety._investigation is not None


async def test_household_waking_up_stands_down_investigation(safety, fake_hass, clock):
    """Mid-investigation, the household wakes up (sleeping flips False) and
    nobody has left — the sleeping-triggered investigation must stand down
    rather than either escalating or being kept alive forever."""
    _door_open(fake_hass)
    _motion(fake_hass, "binary_sensor.hallway_motion")
    await _sleep_tick(safety, fake_hass)
    assert safety._investigation is not None
    clock["now"] += 10
    actions = await safety.tick(sleeping=False, anyone_home=True)
    fake_hass.close_pending()
    assert [a for a in actions if str(a.get("type", "")).startswith("intrusion")] == []
    assert safety._investigation is None


async def test_away_branch_unaffected_by_sleeping_changes(safety, fake_hass, clock):
    """Regression guard: the away-branch's own corroboration/investigation
    behaviour (pre-existing, well-tested) must be untouched by the refactor
    that extracted the shared _begin_investigation helper."""
    fake_hass.states.set("person.username", "not_home")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    _motion(fake_hass, "binary_sensor.living_motion")
    actions = await safety.tick(sleeping=False, anyone_home=False)
    fake_hass.close_pending()
    result = [a for a in actions if str(a.get("type", "")).startswith("intrusion")]
    assert len(result) == 1
    assert result[0]["type"] == "intrusion_investigating"
    assert safety._investigation["trigger"] == "away"
