"""SafetyManager.tick, freeze, and the small helpers that decide whether the
house is "away", which door is a breach and which motion counts.

Characterisation tests (8.7.15): they pin what the code does today, ahead of a
later split of cognitive_core.py. Existing files cover the main intrusion,
confinement, sleeping and face stand down flows; these fill the gaps. A test
named test_current_behaviour_* pins something odd that is NOT being changed
here (see "Found, not fixed" in the PR).
"""
import sys
import types

import pytest

from cognitive_safety_kit import (  # noqa: F401  (fixtures)
    _isolated_core, away, cc, clock, intrusions, motion, service_calls)
from fakes import FakeRegistryEntry

ALARM = "alarm_control_panel.home"


@pytest.fixture(autouse=True)
def _camera_stand_in(monkeypatch):
    """camera.py needs aiohttp at import time, which the unit environment may
    not have, so give the lazy `from .camera import active_camera_states` a
    stand in. Nothing here is about the camera module itself."""
    stand_in = types.ModuleType("jc.camera")
    stand_in.active_camera_states = lambda hass: hass.states.async_all("camera")
    monkeypatch.setitem(sys.modules, "jc.camera", stand_in)


@pytest.fixture
def make_safety(cc, fake_hass):
    def _make(**extra):
        cfg = {"honorific": "sir"}
        cfg.update(extra)
        return cc.SafetyManager(fake_hass, cfg)
    return _make


@pytest.fixture
def safety(make_safety):
    return make_safety()


async def _tick(safety, hass, sleeping=False, anyone_home=None):
    if anyone_home is None:
        anyone_home = not sleeping
    actions = await safety.tick(sleeping=sleeping, anyone_home=anyone_home)
    hass.close_pending()
    return actions


def _types(actions):
    return [a["type"] for a in actions]


# ── tick: the order of freeze, intrusion and nighttime lockdown ─────────────

async def test_tick_runs_freeze_then_intrusion_then_nighttime_lockdown(
        make_safety, fake_hass, clock):
    """Order matters because _emit_action announces in list order: the freeze
    notice, then the break in, then the quiet "I locked up" note."""
    safety = make_safety(lockdown_auto_on_arm=True)
    fake_hass.states.set("weather.home", "snowy", temperature=10)
    away(fake_hass)
    motion(fake_hass)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    actions = await _tick(safety, fake_hass, sleeping=True, anyone_home=False)
    assert _types(actions) == ["freeze_critical", "intrusion_investigating", "lockdown"]
    assert service_calls(fake_hass, "lock", "lock") == [("lock", "lock", {"entity_id": "lock.front"})]


async def test_tick_with_nothing_wrong_returns_no_actions(safety, fake_hass, clock):
    fake_hass.states.set("weather.home", "sunny", temperature=60)
    assert await _tick(safety, fake_hass) == []


async def test_confinement_on_does_not_gate_freeze_or_the_nighttime_sweep(
        make_safety, fake_hass, clock):
    """The confinement switch only decides whether intrusion is watched. A
    freeze warning and the (opt in) sleeping sweep run either way."""
    safety = make_safety(intrusion_requires_confinement=True, lockdown_auto_on_arm=True,
                         security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, "disarmed")
    fake_hass.states.set("weather.home", "cloudy", temperature=30)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    away(fake_hass)
    motion(fake_hass)
    actions = await _tick(safety, fake_hass, sleeping=True, anyone_home=False)
    assert _types(actions) == ["freeze_warning", "lockdown"]       # no intrusion alert


async def test_confinement_on_but_not_confined_drops_even_an_away_investigation(
        make_safety, fake_hass, clock):
    safety = make_safety(security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, "disarmed")
    away(fake_hass)
    motion(fake_hass)
    first = await _tick(safety, fake_hass)
    assert _types(intrusions(first)) == ["intrusion_investigating"]
    assert safety._investigation["trigger"] == "away"
    safety.config["intrusion_requires_confinement"] = True          # switch the master switch on
    clock["now"] += 10
    await _tick(safety, fake_hass)
    assert safety._investigation is None                            # not confined: nothing is watched


async def test_setting_off_keeps_an_investigation_that_did_not_come_from_confinement(
        make_safety, fake_hass, clock):
    """Only a confined investigation is dropped when the setting goes off. A
    sleeping one (opened by a ground floor breach) carries on."""
    safety = make_safety()
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    motion(fake_hass)
    first = await _tick(safety, fake_hass, sleeping=True)
    assert _types(intrusions(first)) == ["intrusion_investigating"]
    assert safety._investigation["trigger"] == "sleeping"
    clock["now"] += 30
    await _tick(safety, fake_hass, sleeping=True)
    assert safety._investigation is not None


async def test_confinement_on_with_an_armed_alarm_keeps_watching_each_tick(
        make_safety, fake_hass, clock):
    safety = make_safety(intrusion_requires_confinement=True, security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, "armed_away")
    motion(fake_hass)
    first = await _tick(safety, fake_hass)
    assert _types(intrusions(first)) == ["intrusion_investigating"]
    assert safety._investigation["trigger"] in ("away", "confined")
    clock["now"] += 30
    await _tick(safety, fake_hass)
    assert safety._investigation is not None                        # still armed: still watching
    fake_hass.states.set(ALARM, "disarmed")
    clock["now"] += 30
    await _tick(safety, fake_hass)
    assert safety._investigation is None                            # disarmed ends it at once


async def test_formal_lockdown_counts_as_confinement_without_an_alarm(
        make_safety, cc, fake_hass, clock):
    safety = make_safety(intrusion_requires_confinement=True)
    mgr = cc.LockdownManager(fake_hass, {})
    mgr.active = True
    cc._CORE.lockdown_mgr = mgr
    fake_hass.states.set("person.username", "home")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    motion(fake_hass)
    actions = await _tick(safety, fake_hass)
    assert _types(intrusions(actions)) == ["intrusion_investigating"]


# ── tick: one failing stage never loses what the others gathered ───────────

def _boom(*a, **k):
    raise RuntimeError("stage broke")


async def _boom_async(*a, **k):
    raise RuntimeError("stage broke")


async def _all_three_would_fire(make_safety, fake_hass):
    safety = make_safety(lockdown_auto_on_arm=True)
    fake_hass.states.set("weather.home", "snowy", temperature=10)
    away(fake_hass)
    motion(fake_hass)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    return safety


async def test_a_freeze_error_is_logged_and_intrusion_and_the_sweep_still_run(
        make_safety, fake_hass, clock, monkeypatch, caplog):
    safety = await _all_three_would_fire(make_safety, fake_hass)
    monkeypatch.setattr(safety, "_check_freeze", _boom_async)
    with caplog.at_level("WARNING"):
        actions = await _tick(safety, fake_hass, sleeping=True, anyone_home=False)
    assert _types(actions) == ["intrusion_investigating", "lockdown"]
    assert "freeze check failed" in caplog.text


async def test_an_intrusion_error_keeps_the_freeze_alert_and_the_sweep_still_runs(
        make_safety, fake_hass, clock, monkeypatch, caplog):
    safety = await _all_three_would_fire(make_safety, fake_hass)
    monkeypatch.setattr(safety, "_check_intrusion", _boom_async)
    with caplog.at_level("WARNING"):
        actions = await _tick(safety, fake_hass, sleeping=True, anyone_home=False)
    assert _types(actions) == ["freeze_critical", "lockdown"]
    assert "intrusion check failed" in caplog.text


async def test_a_sweep_error_keeps_the_freeze_and_intrusion_alerts(
        make_safety, fake_hass, clock, monkeypatch, caplog):
    safety = await _all_three_would_fire(make_safety, fake_hass)
    monkeypatch.setattr(safety, "_nighttime_lockdown", _boom_async)
    with caplog.at_level("WARNING"):
        actions = await _tick(safety, fake_hass, sleeping=True, anyone_home=False)
    assert _types(actions) == ["freeze_critical", "intrusion_investigating"]
    assert "nighttime lockdown failed" in caplog.text


async def test_an_error_before_the_intrusion_check_is_also_contained(
        make_safety, fake_hass, clock, monkeypatch):
    safety = await _all_three_would_fire(make_safety, fake_hass)
    monkeypatch.setattr(safety, "_residents_away", _boom)
    actions = await _tick(safety, fake_hass, sleeping=False, anyone_home=False)
    assert _types(actions) == ["freeze_critical"]


# ── tick: the nighttime sweep guards ────────────────────────────────────────

async def test_the_nighttime_sweep_does_nothing_without_the_opt_in(make_safety, fake_hass, clock):
    for setting in ({}, {"lockdown_auto_on_arm": False}, {"lockdown_auto_on_arm": "true"}):
        safety = make_safety(**setting)
        fake_hass.states.set("lock.front", "unlocked")
        fake_hass.states.set("cover.garage", "open")
        assert await _tick(safety, fake_hass, sleeping=True) == []
    assert fake_hass.service_calls == []                                # only the literal true counts


async def test_nighttime_sweep_needs_sleeping_the_opt_in_and_no_formal_lockdown(
        make_safety, cc, fake_hass, clock):
    safety = make_safety(lockdown_auto_on_arm=True)
    fake_hass.states.set("lock.front", "unlocked")
    await _tick(safety, fake_hass, sleeping=False)                  # awake: no sweep
    assert fake_hass.service_calls == []
    mgr = cc.LockdownManager(fake_hass, {})
    mgr.active = True
    cc._CORE.lockdown_mgr = mgr                                     # formal lockdown handles securing
    await _tick(safety, fake_hass, sleeping=True)
    assert fake_hass.service_calls == []
    assert safety._last_lockdown_check == 0.0                       # and the sweep timer was not consumed
    mgr.active = False
    await _tick(safety, fake_hass, sleeping=True)
    assert len(service_calls(fake_hass, "lock", "lock")) == 1


async def test_nighttime_sweep_repeats_no_more_often_than_every_300_seconds(
        make_safety, fake_hass, clock):
    safety = make_safety(lockdown_auto_on_arm=True)
    fake_hass.states.set("lock.front", "unlocked", friendly_name="Front")
    first = await _tick(safety, fake_hass, sleeping=True)
    assert _types(first) == ["lockdown"]
    clock["now"] += 300                                             # exactly the interval: not yet
    assert _types(await _tick(safety, fake_hass, sleeping=True)) == []
    clock["now"] += 1
    assert _types(await _tick(safety, fake_hass, sleeping=True)) == ["lockdown"]
    assert len(service_calls(fake_hass, "lock", "lock")) == 2


# ── _check_freeze ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("temp,expected", [
    (20, "freeze_critical"),            # <= 20F is critical, inclusive
    (20.5, "freeze_warning"),
    (35, "freeze_warning"),             # <= 35F warns, inclusive
    (35.5, None),
    (-40, "freeze_critical"),
])
async def test_freeze_thresholds_are_inclusive(safety, fake_hass, clock, temp, expected):
    fake_hass.states.set("weather.home", "cloudy", temperature=temp)
    action = await safety._check_freeze()
    assert (action or {}).get("type") == expected


async def test_freeze_cooldown_is_one_hour_and_critical_repeats_each_hour(
        safety, fake_hass, clock):
    fake_hass.states.set("weather.home", "snowy", temperature=10)
    assert (await safety._check_freeze())["type"] == "freeze_critical"
    clock["now"] += 3599
    assert await safety._check_freeze() is None
    clock["now"] += 1
    assert (await safety._check_freeze())["type"] == "freeze_critical"   # repeats while it stays critical


async def test_a_critical_alert_does_not_use_up_the_one_shot_warning(
        safety, fake_hass, clock):
    fake_hass.states.set("weather.home", "snowy", temperature=10)
    assert (await safety._check_freeze())["type"] == "freeze_critical"
    clock["now"] += 3600
    fake_hass.states.set("weather.home", "snowy", temperature=30)
    assert (await safety._check_freeze())["type"] == "freeze_warning"


async def test_the_warning_rearms_only_after_it_gets_above_40f(
        safety, fake_hass, clock):
    fake_hass.states.set("weather.home", "cloudy", temperature=30)
    assert (await safety._check_freeze())["type"] == "freeze_warning"
    for temp in (38, 40):                                           # still not "clearly warm"
        clock["now"] += 3600
        fake_hass.states.set("weather.home", "cloudy", temperature=temp)
        assert await safety._check_freeze() is None
    clock["now"] += 3600
    fake_hass.states.set("weather.home", "cloudy", temperature=30)
    assert await safety._check_freeze() is None                     # warned already, not re-armed
    clock["now"] += 3600
    fake_hass.states.set("weather.home", "cloudy", temperature=40.5)
    assert await safety._check_freeze() is None                     # this reading re-arms it
    clock["now"] += 3600
    fake_hass.states.set("weather.home", "cloudy", temperature=30)
    assert (await safety._check_freeze())["type"] == "freeze_warning"


async def test_no_outdoor_reading_means_no_alert_and_the_cooldown_is_not_spent(
        safety, fake_hass, clock):
    assert await safety._check_freeze() is None
    assert safety._last_freeze_alert == 0.0
    fake_hass.states.set("weather.home", "snowy", temperature=10)
    assert (await safety._check_freeze())["type"] == "freeze_critical"


async def test_freeze_message_is_localised_and_uses_the_presence_aware_honorific(
        safety, cc, fake_hass, clock, monkeypatch):
    fake_hass.states.set("weather.home", "snowy", temperature=10, temperature_unit="°F")
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "madam")
    monkeypatch.setattr(cc, "_hass_lang", lambda hass: "fr")
    fr = await safety._check_freeze()
    i18n = cc._notify_i18n()
    assert fr["message"] == i18n.message(
        "freeze_critical", "fr", honorific="Madam", reading="10.0°F", set_to="55°F")
    safety._last_freeze_alert = 0.0
    monkeypatch.setattr(cc, "_hass_lang", lambda hass: "en")
    en = await safety._check_freeze()
    assert en["message"] == i18n.message(
        "freeze_critical", "en", honorific="Madam", reading="10.0°F", set_to="55°F")
    assert "Madam" in en["message"] and fr["message"] != en["message"]


async def test_freeze_celsius_message_suggests_the_setpoint_in_celsius(
        safety, fake_hass, clock):
    fake_hass.states.set("weather.home", "snowy", temperature=-10, temperature_unit="°C")
    action = await safety._check_freeze()
    assert "13°C" in action["message"]                              # 55°F, shown in the home's own unit


async def test_a_non_numeric_weather_temperature_falls_through_to_an_outdoor_sensor(
        safety, fake_hass, clock):
    fake_hass.states.set("weather.broken", "cloudy", temperature="n/a")
    fake_hass.states.set("sensor.outdoor_temp", "10", device_class="temperature",
                         friendly_name="Outdoor Temperature", unit_of_measurement="°F")
    assert (await safety._check_freeze())["type"] == "freeze_critical"


async def test_discover_outdoor_temp_reads_the_default_unit_when_none_is_given(
        cc, fake_hass):
    fake_hass.config.units = type("U", (), {"temperature_unit": "°C"})()
    fake_hass.states.set("weather.home", "snowy", temperature=-5)
    assert cc.discover_outdoor_temp(fake_hass) == (-5.0, "°C")
    fake_hass.states.set("sensor.outdoor_temp", "oops", device_class="temperature",
                         friendly_name="Outdoor Temperature")
    fake_hass.states.remove("weather.home")
    assert cc.discover_outdoor_temp(fake_hass) is None              # unreadable sensor: nothing found


# ── _residents_away ─────────────────────────────────────────────────────────

def test_a_resident_at_home_always_wins_over_an_armed_away_alarm(make_safety, fake_hass):
    safety = make_safety(security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("person.username", "Home")                 # case does not matter
    assert safety._residents_away() is False
    # A phone linked to the person still counts (8.21.0).
    fake_hass.states.set("person.username", "not_home",
                         device_trackers=["device_tracker.phone"])
    fake_hass.states.set("device_tracker.phone", "home")
    assert safety._residents_away() is False


def test_a_tracker_linked_to_nobody_does_not_stop_armed_away(make_safety, fake_hass):
    # Bug 1 (8.21.0): a TV or hub reading home is not a resident.
    safety = make_safety(security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("person.username", "not_home")
    fake_hass.states.set("device_tracker.living_room_tv", "home")
    assert safety._residents_away() is True


@pytest.mark.parametrize("alarm,expected", [
    ("armed_away", True), ("armed_vacation", True),
    ("armed_home", False), ("armed_night", False), ("disarmed", False)])
def test_with_nobody_tracked_only_an_away_style_alarm_means_away(
        make_safety, fake_hass, alarm, expected):
    safety = make_safety(security_alarm_entity=ALARM)
    fake_hass.states.set(ALARM, alarm)
    assert safety._residents_away() is expected


def test_nothing_tracked_and_no_alarm_is_not_away(safety):
    assert safety._residents_away() is False                        # absence of tracking is not absence of people


def test_tracked_and_reading_away_is_away(safety, fake_hass):
    fake_hass.states.set("device_tracker.phone", "not_home")
    assert safety._residents_away() is True


def test_a_device_tracker_in_an_odd_state_does_not_count_as_tracked(safety, fake_hass):
    fake_hass.states.set("device_tracker.phone", "unavailable")
    assert safety._residents_away() is False


def test_an_unknown_person_is_never_away(safety, fake_hass):
    """Bug 4 (8.21.0): a person reading unavailable or unknown is not away,
    even when another person is. A zone name such as "work" is away."""
    fake_hass.states.set("person.other", "not_home")
    for state in ("unavailable", "unknown"):
        fake_hass.states.set("person.username", state)
        assert safety._residents_away() is False
    fake_hass.states.set("person.username", "work")
    assert safety._residents_away() is True


# ── _open_entry / _ground_floor_open_entry ──────────────────────────────────

@pytest.mark.parametrize("domain,eid,state,dc,expected", [
    ("binary_sensor", "binary_sensor.front", "on", "door", True),
    ("binary_sensor", "binary_sensor.kitchen_win", "on", "window", True),
    ("binary_sensor", "binary_sensor.side", "on", "garage_door", True),
    ("binary_sensor", "binary_sensor.hatch", "on", "opening", True),
    ("binary_sensor", "binary_sensor.front", "off", "door", False),
    ("binary_sensor", "binary_sensor.hall_motion", "on", "motion", False),
    ("cover", "cover.garage", "open", "garage", True),
    ("cover", "cover.garage", "opening", "garage_door", True),
    ("cover", "cover.front_door", "open", "door", True),
    ("cover", "cover.garden_gate", "open", "gate", False),          # outdoor name: property perimeter
    ("cover", "cover.garage", "closed", "garage", False),
    ("cover", "cover.blind", "open", "blind", False),
])
def test_open_entry_recognises_each_kind_of_exterior_opening(
        safety, fake_hass, domain, eid, state, dc, expected):
    fake_hass.states.set(eid, state, device_class=dc)
    assert safety._open_entry() == (eid if expected else None)


def test_a_garage_opening_stays_envelope_even_when_it_sounds_outdoor(safety, fake_hass):
    fake_hass.states.set("binary_sensor.driveway_garage_door", "on", device_class="garage_door")
    assert safety._open_entry() == "binary_sensor.driveway_garage_door"


def test_a_yard_gate_and_a_shed_are_not_the_house_envelope(safety, fake_hass):
    fake_hass.states.set("binary_sensor.side_gate", "on", device_class="door")
    fake_hass.states.set("binary_sensor.garden_shed_door", "on", device_class="door")
    assert safety._open_entry() is None


def test_open_entry_prefers_a_binary_sensor_over_a_cover(safety, fake_hass):
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    fake_hass.states.set("binary_sensor.front", "on", device_class="door")
    assert safety._open_entry() == "binary_sensor.front"


def test_open_entry_can_be_limited_to_a_set_of_areas(safety, fake_hass, monkeypatch):
    fake_hass.states.set("binary_sensor.upstairs_window", "on", device_class="window")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    areas = {"binary_sensor.upstairs_window": "landing", "binary_sensor.back_door": "kitchen"}
    monkeypatch.setattr(safety, "_breach_area", lambda eid: areas.get(eid))
    assert safety._open_entry(areas=["kitchen"]) == "binary_sensor.back_door"
    assert safety._open_entry(areas=["landing"]) == "binary_sensor.upstairs_window"
    assert safety._open_entry(areas=["cellar"]) is None


def test_ground_floor_scope_falls_back_to_every_door_when_unconfigured(
        make_safety, fake_hass, monkeypatch):
    """Unconfigured must never mean less protection than before."""
    fake_hass.states.set("binary_sensor.upstairs_window", "on", device_class="window")
    safety = make_safety()
    assert safety._ground_floor_open_entry() == ("binary_sensor.upstairs_window", False)
    safety = make_safety(ground_floor_areas=["kitchen"])
    monkeypatch.setattr(safety, "_breach_area", lambda eid: "landing")
    assert safety._ground_floor_open_entry() == (None, True)


# ── _breach_area / _motion_key ──────────────────────────────────────────────

@pytest.fixture
def registries(monkeypatch):
    """Entity and device registries the way audio_routing and _motion_key read
    them: through homeassistant.helpers.entity_registry / device_registry."""
    er = sys.modules["homeassistant.helpers.entity_registry"]
    dr = sys.modules["homeassistant.helpers.device_registry"]
    entities, devices = {}, {}
    monkeypatch.setattr(er, "async_get", lambda hass: type("R", (), {
        "async_get": staticmethod(lambda eid: entities.get(eid))})())
    monkeypatch.setattr(dr, "async_get", lambda hass: type("D", (), {
        "async_get": staticmethod(lambda did: devices.get(did))})())
    return entities, devices


def test_breach_area_resolves_an_entity_then_falls_back_to_its_device(
        safety, registries):
    entities, devices = registries
    entities["binary_sensor.door1"] = FakeRegistryEntry("binary_sensor.door1", "x", area_id="hall")
    entities["binary_sensor.door2"] = FakeRegistryEntry("binary_sensor.door2", "x", device_id="dev1")
    devices["dev1"] = type("Dev", (), {"area_id": "kitchen"})()
    assert safety._breach_area("binary_sensor.door1") == "hall"
    assert safety._breach_area("binary_sensor.door2") == "kitchen"
    assert safety._breach_area("binary_sensor.unknown") is None
    assert safety._breach_area(None) is None


def test_breach_area_never_raises_when_the_registry_breaks(safety, monkeypatch):
    er = sys.modules["homeassistant.helpers.entity_registry"]

    def boom(hass):
        raise RuntimeError("registry offline")
    monkeypatch.setattr(er, "async_get", boom)
    assert safety._breach_area("binary_sensor.door") is None


def test_motion_key_is_the_area_else_the_device_area_else_the_entity_id(
        safety, registries):
    entities, devices = registries
    entities["binary_sensor.a"] = FakeRegistryEntry("binary_sensor.a", "x", area_id="lounge")
    entities["binary_sensor.b"] = FakeRegistryEntry("binary_sensor.b", "x", device_id="dev9")
    entities["binary_sensor.c"] = FakeRegistryEntry("binary_sensor.c", "x", device_id="gone")
    devices["dev9"] = type("Dev", (), {"area_id": "study"})()
    assert safety._motion_key("binary_sensor.a") == "lounge"
    assert safety._motion_key("binary_sensor.b") == "study"
    assert safety._motion_key("binary_sensor.c") == "binary_sensor.c"     # device missing
    assert safety._motion_key("binary_sensor.zzz") == "binary_sensor.zzz"  # unknown entity


def test_motion_key_falls_back_to_the_entity_id_when_the_registry_breaks(safety, monkeypatch):
    er = sys.modules["homeassistant.helpers.entity_registry"]
    monkeypatch.setattr(er, "async_get", lambda hass: (_ for _ in ()).throw(RuntimeError("x")))
    assert safety._motion_key("binary_sensor.a") == "binary_sensor.a"


# ── _qualifying_motion ──────────────────────────────────────────────────────

def test_qualifying_motion_takes_indoor_active_motion_occupancy_and_presence(
        safety, fake_hass):
    fake_hass.states.set("binary_sensor.m", "on", device_class="motion", friendly_name="Hall")
    fake_hass.states.set("binary_sensor.o", "on", device_class="occupancy")
    fake_hass.states.set("binary_sensor.p", "on", device_class="presence")
    fake_hass.states.set("binary_sensor.off_motion", "off", device_class="motion")
    fake_hass.states.set("binary_sensor.door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.no_class", "on")
    got = dict(safety._qualifying_motion(False))
    assert got == {"binary_sensor.m": "Hall", "binary_sensor.o": "binary_sensor.o",
                   "binary_sensor.p": "binary_sensor.p"}            # name falls back to the id


def test_qualifying_motion_skips_outdoor_sensors(safety, fake_hass):
    fake_hass.states.set("binary_sensor.driveway_motion", "on", device_class="motion")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert [e for e, _ in safety._qualifying_motion(False)] == ["binary_sensor.hall_motion"]


def test_qualifying_motion_skips_bedrooms_only_while_asleep(
        make_safety, fake_hass, monkeypatch):
    safety = make_safety(bedroom_areas=["bedroom"])
    fake_hass.states.set("binary_sensor.bed_motion", "on", device_class="motion")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    keys = {"binary_sensor.bed_motion": "bedroom", "binary_sensor.hall_motion": "hall"}
    monkeypatch.setattr(safety, "_motion_key", lambda eid: keys[eid])
    assert sorted(e for e, _ in safety._qualifying_motion(False)) == [
        "binary_sensor.bed_motion", "binary_sensor.hall_motion"]
    assert [e for e, _ in safety._qualifying_motion(True)] == ["binary_sensor.hall_motion"]


def test_qualifying_motion_ignores_a_bedroom_list_when_none_is_configured(safety, fake_hass):
    fake_hass.states.set("binary_sensor.bed_motion", "on", device_class="motion")
    assert [e for e, _ in safety._qualifying_motion(True)] == ["binary_sensor.bed_motion"]


# ── _person_camera_entity ───────────────────────────────────────────────────

def test_person_camera_maps_the_sensor_to_its_camera_by_slug(safety, fake_hass):
    fake_hass.states.set("binary_sensor.den_person", "on", device_class="occupancy")
    fake_hass.states.set("camera.den", "idle")
    assert safety._person_camera_entity() == "camera.den"
    assert safety._person_on_camera() is True


@pytest.mark.parametrize("suffix", ["_person", "_person_occupancy"])
def test_person_camera_strips_each_known_suffix(safety, fake_hass, suffix):
    fake_hass.states.set(f"binary_sensor.attic{suffix}", "on", device_class="occupancy")
    fake_hass.states.set("camera.attic", "idle")
    assert safety._person_camera_entity() == "camera.attic"


def test_person_camera_falls_back_to_a_camera_sharing_the_room_word(safety, fake_hass):
    fake_hass.states.set("binary_sensor.dining_room_person", "on", device_class="occupancy")
    fake_hass.states.set("camera.dining_nook", "idle")
    assert safety._person_camera_entity() == "camera.dining_nook"


def test_person_camera_returns_a_best_effort_handle_when_no_camera_exists(safety, fake_hass):
    fake_hass.states.set("binary_sensor.loft_person", "on", device_class="motion")
    assert safety._person_camera_entity() == "camera.loft"


def test_person_camera_ignores_idle_wrong_class_and_outdoor_sensors(safety, fake_hass):
    fake_hass.states.set("binary_sensor.den_person", "off", device_class="occupancy")
    fake_hass.states.set("binary_sensor.hall_person", "on", device_class="door")
    fake_hass.states.set("binary_sensor.garden_person", "on", device_class="occupancy")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")   # no "person" in it
    assert safety._person_camera_entity() is None
    assert safety._person_camera_entity(indoor_only=False) == "camera.garden"        # outdoor allowed on request
    assert safety._person_on_camera() is False
