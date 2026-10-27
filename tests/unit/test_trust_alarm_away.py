"""Armed away, the alarm watches the house and Nova trusts it.

Reproduces 25 Oct 2026: the house emptied at 11:44, Alarmo armed away at
11:46 and the kitchen presence sensor, still on from the residents walking
out, only cleared at 11:49. Nova read that as someone in an empty, armed
house. Armed away or on vacation, motion alone never opens Nova's own
investigation; the alarm going off still does, and so does everything that
happens while the alarm is not armed away.

All state here is fake. Nothing touches a real alarm or lock.
"""
import pytest

ALARM = "alarm_control_panel.home_security"


@pytest.fixture
def make_safety(cognitive_core, fake_hass):
    def _make(**extra):
        cfg = {"honorific": "sir", "security_alarm_entity": ALARM}
        cfg.update(extra)
        return cognitive_core.SafetyManager(fake_hass, cfg)
    return _make


def _away(hass):
    hass.states.set("person.abi", "not_home")
    hass.states.set("person.rachel", "not_home")


def _kitchen_presence(hass):
    hass.states.set("binary_sensor.kitchen_m_ps_presence", "on", device_class="occupancy",
                    friendly_name="Kitchen Main PS Occupancy")


def _hall_motion(hass):
    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")


def _front_door_open(hass):
    hass.states.set("binary_sensor.front_door", "on", device_class="door")


async def _tick(safety, hass, sleeping=False):
    actions = await safety.tick(sleeping=sleeping, anyone_home=False)
    hass.close_pending()
    return [a for a in actions if str(a.get("type", "")).startswith("intrusion")]


@pytest.mark.parametrize("armed", ["armed_away", "armed_vacation"])
async def test_leftover_presence_after_arming_away_says_nothing(make_safety, fake_hass, armed):
    safety = make_safety()
    _away(fake_hass)
    _kitchen_presence(fake_hass)
    fake_hass.states.set(ALARM, armed)
    assert await _tick(safety, fake_hass) == []
    assert safety._investigation is None


async def test_armed_away_motion_through_the_house_never_opens_one(make_safety, fake_hass):
    safety = make_safety()
    _away(fake_hass)
    fake_hass.states.set(ALARM, "armed_away")
    _kitchen_presence(fake_hass)
    _hall_motion(fake_hass)
    _front_door_open(fake_hass)
    assert await _tick(safety, fake_hass) == []
    assert safety._investigation is None


async def test_armed_away_is_left_to_the_alarm_with_confinement_on(make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _away(fake_hass)
    _hall_motion(fake_hass)
    fake_hass.states.set(ALARM, "armed_away")
    assert await _tick(safety, fake_hass) == []


async def test_the_alarm_going_off_while_away_still_alerts(make_safety, fake_hass):
    safety = make_safety()
    _away(fake_hass)
    _front_door_open(fake_hass)
    _hall_motion(fake_hass)
    fake_hass.states.set(ALARM, "triggered")
    intr = await _tick(safety, fake_hass)
    assert [a["type"] for a in intr] == ["intrusion_investigating"]


async def test_away_and_not_armed_nova_still_watches(make_safety, fake_hass):
    safety = make_safety()
    _away(fake_hass)
    _front_door_open(fake_hass)
    _hall_motion(fake_hass)
    fake_hass.states.set(ALARM, "disarmed")
    intr = await _tick(safety, fake_hass)
    assert [a["type"] for a in intr] == ["intrusion_investigating"]


async def test_without_an_alarm_nova_still_watches(cognitive_core, fake_hass):
    safety = cognitive_core.SafetyManager(fake_hass, {"honorific": "sir"})
    _away(fake_hass)
    _front_door_open(fake_hass)
    _hall_motion(fake_hass)
    intr = await _tick(safety, fake_hass)
    assert [a["type"] for a in intr] == ["intrusion_investigating"]


async def test_an_investigation_open_before_arming_keeps_running(make_safety, fake_hass):
    safety = make_safety()
    _away(fake_hass)
    _front_door_open(fake_hass)
    _hall_motion(fake_hass)
    fake_hass.states.set(ALARM, "disarmed")
    assert [a["type"] for a in await _tick(safety, fake_hass)] == ["intrusion_investigating"]
    fake_hass.states.set(ALARM, "armed_away")
    await _tick(safety, fake_hass)
    assert safety._investigation is not None
