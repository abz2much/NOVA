"""Regression tests for the 8.21.0 safety review, one per bug.

Each test is the probe that showed the bug, kept for good. All state here is
fake. Nothing touches a real alarm, lock, speaker or phone.
"""
import sys
import types

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


async def _tick(safety, hass, sleeping=False, anyone_home=False):
    actions = await safety.tick(sleeping=sleeping, anyone_home=anyone_home)
    hass.close_pending()
    return [a for a in actions if str(a.get("type", "")).startswith("intrusion")]


async def _walk(safety, hass, clock):
    """A resident walks hall, kitchen, landing and back, 40 seconds apart."""
    seen = []
    for eid in ("binary_sensor.hall_motion", "binary_sensor.kitchen_motion",
                "binary_sensor.landing_motion", "binary_sensor.hall_motion",
                "binary_sensor.kitchen_motion"):
        hass.states.set(eid, "on", device_class="motion")
        seen += await _tick(safety, hass, anyone_home=True)
        clock["now"] += 40
    return seen


# ── bug 1: a tracker linked to nobody switched off away detection ───────────

async def test_bug1_a_tv_tracker_reading_home_does_not_stop_armed_away(cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("device_tracker.living_room_tv", "home")
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    seen = await _tick(safety, fake_hass)
    assert [a["type"] for a in seen] == ["intrusion_investigating"]
    assert not seen[0].get("phone_only")


# ── bug 2: custom bypass let a resident's walk confirm an intrusion ─────────

async def test_bug2_custom_bypass_with_residents_home_never_confirms_on_motion(
        cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {
        "honorific": "sir", "security_alarm_entity": ALARM,
        "intrusion_requires_confinement": True})
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set(ALARM, "armed_custom_bypass")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    seen = await _walk(safety, fake_hass, clock)
    assert seen and not [a for a in seen if a["type"] == "intrusion_confirmed"], seen
    first = [a for a in seen if a["type"] == "intrusion_investigating"]
    assert first and first[0].get("phone_only") is True


async def test_bug2_custom_bypass_still_confirms_when_the_alarm_goes_off(cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {
        "honorific": "sir", "security_alarm_entity": ALARM,
        "intrusion_requires_confinement": True})
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set(ALARM, "armed_custom_bypass")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    await _tick(safety, fake_hass, anyone_home=True)
    clock["now"] += 30
    fake_hass.states.set(ALARM, "triggered")
    seen = await _tick(safety, fake_hass, anyone_home=True)
    assert [a["type"] for a in seen] == ["intrusion_confirmed"]


# ── bug 3: the "couldn't reach you" notice used the speakers ────────────────

@pytest.fixture
def routed(cc, load, monkeypatch):
    """Delivery with fake speakers and a fake phone push."""
    hab = load("habituation")
    monkeypatch.setattr(hab, "is_quiet", lambda k: False)
    monkeypatch.setattr(hab, "record", lambda *a, **k: None)
    pushes, spoken = [], []

    async def _push(hass, config, message, action_type, snap=None, *,
                    request_id=None, extra_data=None):
        pushes.append(action_type)
    monkeypatch.setattr(cc, "_push_notification", _push)

    async def _push_all(hass, config, message, action_type, snap=None, *,
                        request_id=None, **kw):
        pushes.append(action_type)
    monkeypatch.setattr(cc, "_notify_all_devices", _push_all)

    tts = types.ModuleType("jc.tts_helper")
    tts.resolve_tts_for_context = lambda *a, **k: "tts.x"

    async def _announce(hass, message, *a, **k):
        spoken.append(message)
    tts.async_announce = _announce
    ar = types.ModuleType("jc.audio_routing")
    ar.observer_speak_target = lambda *a, **k: (["media_player.x"], "broadcast")
    monkeypatch.setitem(sys.modules, "jc.tts_helper", tts)
    monkeypatch.setitem(sys.modules, "jc.audio_routing", ar)
    sd = types.ModuleType("jc.sleep_detection")
    sd._in_quiet_hours = lambda *a: False
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sd)
    return pushes, spoken


async def test_bug3_the_unresolved_notice_with_residents_home_goes_to_phones_only(
        cc, routed, fake_hass, clock):
    pushes, spoken = routed
    safety = cc.SafetyManager(fake_hass, {
        "honorific": "sir", "security_alarm_entity": ALARM,
        "intrusion_requires_confinement": True})
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set(ALARM, "armed_home")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    seen = await _walk(safety, fake_hass, clock)
    unresolved = [a for a in seen if a["type"] == "intrusion_unresolved"]
    assert len(unresolved) == 1 and unresolved[0].get("phone_only") is True
    await cc._emit_action(fake_hass, {}, unresolved[0], sleeping=False)
    assert spoken == [] and pushes == ["intrusion_unresolved"]


async def test_bug3_the_unresolved_notice_when_away_still_speaks(cc, routed, fake_hass, clock):
    pushes, spoken = routed
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    # Motion lingers in one room: no route through the house, so after the
    # response timeout it is the unresolved notice, not a confirmation.
    seen = []
    for _ in range(6):
        fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
        seen += await _tick(safety, fake_hass)
        clock["now"] += 30
    unresolved = [a for a in seen if a["type"] == "intrusion_unresolved"]
    assert len(unresolved) == 1 and not unresolved[0].get("phone_only"), seen
    await cc._emit_action(fake_hass, {}, unresolved[0], sleeping=False)
    assert len(spoken) == 1 and pushes == ["intrusion_unresolved"]


# ── bug 4: a person reading unknown counted as away ─────────────────────────

async def test_bug4_an_unknown_person_does_not_start_an_away_alert(cc, fake_hass, clock):
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("person.rachel", "unknown")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert await _tick(safety, fake_hass) == []


# ── bug 5: an excluded motion sensor started intrusion checks ───────────────

async def test_bug5_an_excluded_motion_sensor_never_starts_an_investigation(
        cc, load, fake_hass, clock, monkeypatch):
    ef = load("entity_filter")
    monkeypatch.setattr(ef, "_exclusion_config",
                        lambda hass: ({"binary_sensor.virtual_occupancy"}, set(), set()))
    safety = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    fake_hass.states.set("binary_sensor.virtual_occupancy", "on", device_class="occupancy")
    assert await _tick(safety, fake_hass) == []
    # A real sensor still does.
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    assert [a["type"] for a in await _tick(safety, fake_hass)] == ["intrusion_investigating"]


# ── bug 7: a package taken while away was only spoken ───────────────────────

@pytest.fixture
def pm(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    mod = load("package_monitor")
    for d in (mod._STATE, mod._LAST_SPOKEN, mod._TRIGGER_LAST):
        d.clear()
    yield mod
    for d in (mod._STATE, mod._LAST_SPOKEN, mod._TRIGGER_LAST):
        d.clear()


@pytest.fixture
def pkg(pm, load, monkeypatch):
    sw = {"quiet": False, "spoken": [], "pushed": []}
    monkeypatch.setattr(pm, "_in_quiet_hours", lambda h: sw["quiet"])
    monkeypatch.setattr(pm, "_announcements_on", lambda h: True)
    monkeypatch.setattr(pm, "_log", lambda *a, **k: None)
    monkeypatch.setattr(pm, "_now_mono", lambda: 1000.0)

    async def _announce(hass, msg, *a, **k):
        sw["spoken"].append(msg)
    monkeypatch.setattr(load("tts_helper"), "async_announce", _announce)

    async def _push(hass, msg):
        sw["pushed"].append(msg)
    monkeypatch.setattr(pm, "_push_phones", _push)
    return sw


async def _package_then_gone(pm, hass):
    cam = "camera.front_door"
    for present in (True, False):
        await pm.evaluate(hass, None, "Sir", "tts.x", ["media_player.y"], cam,
                          {"package": present, "mail": False,
                           "count": 1 if present else 0, "description": ""})


async def test_bug7_a_package_taken_while_away_reaches_the_phones(pm, pkg, fake_hass):
    fake_hass.states.set("person.abi", "not_home")
    await _package_then_gone(pm, fake_hass)
    assert len(pkg["pushed"]) == 1 and "while no one is home" in pkg["pushed"][0]
    # The spoken line is unchanged.
    assert pkg["spoken"][-1] == pkg["pushed"][0]


async def test_bug7_in_quiet_hours_it_is_pushed_but_not_spoken(pm, pkg, fake_hass):
    pkg["quiet"] = True
    fake_hass.states.set("person.abi", "not_home")
    await _package_then_gone(pm, fake_hass)
    assert len(pkg["pushed"]) == 1 and pkg["spoken"] == []


@pytest.mark.parametrize("state", ["home", "unknown"])
async def test_bug7_nothing_is_pushed_unless_the_residents_are_away(pm, pkg, fake_hass, state):
    fake_hass.states.set("person.abi", state)
    await _package_then_gone(pm, fake_hass)
    assert pkg["pushed"] == []


# ── bug 8: the heating offer said "no one's home" when that wasn't known ────

async def test_bug8_the_heating_offer_waits_until_nobody_is_known_to_be_home(load, fake_hass):
    cp = load("core_proactive")
    mgr = cp.ProactiveManager(fake_hass, {"honorific": "sir"})
    fake_hass.states.set("climate.hall", "heat", hvac_action="heating")
    offers = await mgr.tick(sleeping=False, anyone_home=False, nobody_home=False)
    assert not [o for o in offers if o["type"] == "proactive_hvac"]
    mgr._last_check = 0
    offers = await mgr.tick(sleeping=False, anyone_home=False, nobody_home=True)
    assert [o["type"] for o in offers if o["type"] == "proactive_hvac"] == ["proactive_hvac"]


# ── bug 9: arming named the fridge as a gap to close by hand ────────────────

async def test_bug9_arming_with_the_fridge_open_does_not_name_the_fridge(cc, fake_hass):
    mgr = cc.LockdownManager(fake_hass, {})
    fake_hass.states.set("binary_sensor.fridge_door", "on", device_class="door",
                         friendly_name="Fridge door")
    fake_hass.states.set("binary_sensor.kitchen_window", "on", device_class="window",
                         friendly_name="Kitchen window")
    action = await mgr.engage("test")
    assert "Kitchen window" in action["message"]
    assert "Fridge" not in action["message"]
    assert "binary_sensor.fridge_door" not in mgr.exempt_windows
