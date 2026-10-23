"""Open windows while the alarm is armed home or night.

Armed for people at home, residents open windows, so a door or window only
corroborates an intrusion when it opened after the alarm armed and, with
ground_floor_areas set, is on the ground floor. Armed away and vacation keep
today's rules. The "open while the alarm is armed" anticipation is for armed
away or vacation only.

All state here is fake. Nothing touches a real alarm or lock.
"""
from datetime import datetime, timedelta, timezone

import pytest

ALARM = "alarm_control_panel.home"
WINDOW = "binary_sensor.kitchen_window"
ARMED_AT = datetime(2026, 10, 22, 23, 0, tzinfo=timezone.utc)
BEFORE = ARMED_AT - timedelta(hours=4)
AFTER = ARMED_AT + timedelta(minutes=30)


@pytest.fixture
def make_safety(cognitive_core, fake_hass):
    def _make(**extra):
        cfg = {"honorific": "sir", "security_alarm_entity": ALARM}
        cfg.update(extra)
        return cognitive_core.SafetyManager(fake_hass, cfg)
    return _make


def _arm(hass, state="armed_home"):
    hass.states.set(ALARM, state, last_changed=ARMED_AT)


def _window(hass, when, eid=WINDOW):
    hass.states.set(eid, "on", device_class="window", last_changed=when)


def _motion(hass):
    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")


def _home(hass):
    hass.states.set("person.username", "home")


async def _tick(safety, hass, sleeping=False):
    actions = await safety.tick(sleeping=sleeping, anyone_home=True)
    hass.close_pending()
    return [a for a in actions if str(a.get("type", "")).startswith("intrusion")]


# ── which openings count ────────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["armed_home", "armed_night", "armed_custom_bypass"])
def test_armed_home_since_reads_the_alarm_time(make_safety, fake_hass, state):
    _arm(fake_hass, state)
    assert make_safety()._armed_home_since() == ARMED_AT.timestamp()


@pytest.mark.parametrize("state", ["armed_away", "armed_vacation", "disarmed",
                                   "arming", "pending", "triggered"])
def test_armed_home_since_is_none_otherwise(make_safety, fake_hass, state):
    _arm(fake_hass, state)
    assert make_safety()._armed_home_since() is None


def test_unreadable_alarm_time_leaves_the_old_rule(make_safety, fake_hass):
    _arm(fake_hass)
    fake_hass.states.get(ALARM).last_changed = None
    assert make_safety()._armed_home_since() is None


def test_open_entry_leaves_out_what_was_open_before(make_safety, fake_hass):
    safety = make_safety()
    _window(fake_hass, BEFORE)
    assert safety._open_entry(opened_after=ARMED_AT.timestamp()) is None
    assert safety._open_entry() == WINDOW
    _window(fake_hass, AFTER)
    assert safety._open_entry(opened_after=ARMED_AT.timestamp()) == WINDOW


def test_open_entry_counts_an_unreadable_change_time(make_safety, fake_hass):
    _window(fake_hass, AFTER)
    fake_hass.states.get(WINDOW).last_changed = None
    assert make_safety()._open_entry(opened_after=ARMED_AT.timestamp()) == WINDOW


# ── confined, home and awake ────────────────────────────────────────────────

async def test_window_open_before_arming_home_is_no_breach(make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _arm(fake_hass)
    _window(fake_hass, BEFORE)
    _motion(fake_hass)
    assert await _tick(safety, fake_hass) == []


async def test_window_opened_after_arming_home_is_a_breach(make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _arm(fake_hass)
    _window(fake_hass, AFTER)
    _motion(fake_hass)
    intr = await _tick(safety, fake_hass)
    assert len(intr) == 1
    assert safety._investigation["trigger"] == "confined"


async def test_upstairs_window_opened_after_arming_is_no_breach(
        make_safety, fake_hass, monkeypatch):
    safety = make_safety(intrusion_requires_confinement=True,
                         ground_floor_areas=["kitchen"])
    monkeypatch.setattr(safety, "_breach_area", lambda eid: "landing")
    _home(fake_hass)
    _arm(fake_hass, "armed_night")
    _window(fake_hass, AFTER)
    _motion(fake_hass)
    assert await _tick(safety, fake_hass) == []


async def test_ground_floor_window_opened_after_arming_is_a_breach(
        make_safety, fake_hass, monkeypatch):
    safety = make_safety(intrusion_requires_confinement=True,
                         ground_floor_areas=["kitchen"])
    monkeypatch.setattr(safety, "_breach_area", lambda eid: "kitchen")
    _home(fake_hass)
    _arm(fake_hass, "armed_night")
    _window(fake_hass, AFTER)
    _motion(fake_hass)
    assert len(await _tick(safety, fake_hass)) == 1


async def test_armed_away_is_unchanged(make_safety, fake_hass):
    """Armed away is corroboration on its own, open windows or not."""
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _arm(fake_hass, "armed_away")
    _window(fake_hass, BEFORE)
    _motion(fake_hass)
    assert len(await _tick(safety, fake_hass)) == 1


async def test_lockdown_while_disarmed_keeps_any_open_entry(
        make_safety, fake_hass, cognitive_core, monkeypatch):
    safety = make_safety(intrusion_requires_confinement=True)
    monkeypatch.setattr(cognitive_core, "is_lockdown", lambda: True)
    _home(fake_hass)
    _arm(fake_hass, "disarmed")
    _window(fake_hass, BEFORE)
    _motion(fake_hass)
    assert len(await _tick(safety, fake_hass)) == 1


# ── asleep ──────────────────────────────────────────────────────────────────

async def test_asleep_armed_night_window_open_before_arming_is_no_breach(
        make_safety, fake_hass):
    safety = make_safety()
    _arm(fake_hass, "armed_night")
    _window(fake_hass, BEFORE)
    _motion(fake_hass)
    assert await _tick(safety, fake_hass, sleeping=True) == []


async def test_asleep_armed_night_window_opened_after_arming_is_a_breach(
        make_safety, fake_hass):
    safety = make_safety()
    _arm(fake_hass, "armed_night")
    _window(fake_hass, AFTER)
    _motion(fake_hass)
    intr = await _tick(safety, fake_hass, sleeping=True)
    assert len(intr) == 1
    assert safety._investigation["trigger"] == "sleeping"


async def test_asleep_disarmed_keeps_any_open_window(make_safety, fake_hass):
    safety = make_safety()
    _arm(fake_hass, "disarmed")
    _window(fake_hass, BEFORE)
    _motion(fake_hass)
    assert len(await _tick(safety, fake_hass, sleeping=True)) == 1


# ── a sensor dropping out while armed ───────────────────────────────────────

async def test_window_open_at_arming_that_drops_out_stays_left_out(
        make_safety, fake_hass):
    """A window open since before arming whose sensor goes unavailable and
    comes back gets a new change time. It is still no breach."""
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _arm(fake_hass)
    _window(fake_hass, BEFORE)
    assert await _tick(safety, fake_hass) == []
    assert WINDOW in safety._open_when_armed
    fake_hass.states.set(WINDOW, "unavailable", device_class="window", last_changed=AFTER)
    assert await _tick(safety, fake_hass) == []
    _window(fake_hass, AFTER + timedelta(seconds=20))
    _motion(fake_hass)
    assert await _tick(safety, fake_hass) == []


async def test_window_shut_then_opened_again_after_arming_counts(
        make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _arm(fake_hass)
    _window(fake_hass, BEFORE)
    await _tick(safety, fake_hass)
    fake_hass.states.set(WINDOW, "off", device_class="window", last_changed=AFTER)
    await _tick(safety, fake_hass)
    assert WINDOW not in safety._open_when_armed
    _window(fake_hass, AFTER + timedelta(minutes=5))
    _motion(fake_hass)
    assert len(await _tick(safety, fake_hass)) == 1


async def test_disarming_forgets_what_was_open(make_safety, fake_hass):
    safety = make_safety(intrusion_requires_confinement=True)
    _home(fake_hass)
    _arm(fake_hass)
    _window(fake_hass, BEFORE)
    await _tick(safety, fake_hass)
    assert safety._open_when_armed == {WINDOW}
    _arm(fake_hass, "disarmed")
    await _tick(safety, fake_hass)
    assert safety._open_when_armed == set()
    assert safety._open_when_armed_at is None
