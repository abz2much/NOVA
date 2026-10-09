"""The first intrusion alert goes to phones only when residents are home.

With a person or phone reading home and the alarm armed home or night (or the
household asleep), the first "Motion at ... while the house is secured" alert
is a phone notification, not a spoken one. Armed away, vacation, everyone away
and the confirmed intrusion alert still use the speakers.

All state here is fake. Nothing touches a real alarm, lock or speaker.
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
    ar.observer_speak_target = lambda *a, **k: (["media_player.x"], "normal")
    monkeypatch.setitem(sys.modules, "jc.tts_helper", tts)
    monkeypatch.setitem(sys.modules, "jc.audio_routing", ar)
    sd = types.ModuleType("jc.sleep_detection")
    sd._in_quiet_hours = lambda *a: False
    monkeypatch.setitem(sys.modules, "jc.sleep_detection", sd)
    return pushes, spoken


def _safety(cc, hass, **extra):
    cfg = {"honorific": "sir", "security_alarm_entity": ALARM}
    cfg.update(extra)
    return cc.SafetyManager(hass, cfg)


async def _first_alert(safety, hass, sleeping, anyone_home):
    hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    actions = await safety.tick(sleeping=sleeping, anyone_home=anyone_home)
    hass.close_pending()
    first = [a for a in actions if a["type"] == "intrusion_investigating"]
    assert len(first) == 1, actions
    return first[0]


async def _deliver(cc, hass, action, sleeping):
    await cc._emit_action(hass, {}, action, sleeping=sleeping)


# ── residents home: phone only ──────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["armed_home", "armed_night"])
async def test_armed_home_or_night_with_residents_home_is_phone_only(
        cc, routed, fake_hass, mode):
    pushes, spoken = routed
    safety = _safety(cc, fake_hass, intrusion_requires_confinement=True)
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set(ALARM, mode)
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    first = await _first_alert(safety, fake_hass, sleeping=False, anyone_home=True)
    await _deliver(cc, fake_hass, first, sleeping=False)
    assert spoken == []
    assert pushes == ["intrusion_investigating"]


async def test_a_phone_reading_home_counts_as_a_resident(cc, routed, fake_hass):
    pushes, spoken = routed
    safety = _safety(cc, fake_hass, intrusion_requires_confinement=True)
    fake_hass.states.set("device_tracker.abi_phone", "home")
    fake_hass.states.set(ALARM, "armed_home")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    first = await _first_alert(safety, fake_hass, sleeping=False, anyone_home=True)
    await _deliver(cc, fake_hass, first, sleeping=False)
    assert spoken == [] and pushes == ["intrusion_investigating"]


async def test_asleep_with_residents_home_is_phone_only(cc, routed, fake_hass):
    pushes, spoken = routed
    safety = _safety(cc, fake_hass)
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    first = await _first_alert(safety, fake_hass, sleeping=True, anyone_home=True)
    assert first.get("phone_only") is True
    # Delivered as if awake, so the flag alone keeps it off the speakers.
    await _deliver(cc, fake_hass, first, sleeping=False)
    assert spoken == [] and pushes == ["intrusion_investigating"]


# ── unchanged: the speakers are still used ──────────────────────────────────

async def test_armed_away_still_speaks(cc, routed, fake_hass):
    pushes, spoken = routed
    safety = _safety(cc, fake_hass)
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set(ALARM, "armed_away")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    first = await _first_alert(safety, fake_hass, sleeping=False, anyone_home=False)
    assert not first.get("phone_only")
    await _deliver(cc, fake_hass, first, sleeping=False)
    assert len(spoken) == 1 and pushes == ["intrusion_investigating"]


async def test_armed_vacation_still_speaks(cc, routed, fake_hass):
    pushes, spoken = routed
    safety = _safety(cc, fake_hass)
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set(ALARM, "armed_vacation")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    first = await _first_alert(safety, fake_hass, sleeping=False, anyone_home=False)
    assert not first.get("phone_only")
    await _deliver(cc, fake_hass, first, sleeping=False)
    assert len(spoken) == 1


async def test_everyone_away_still_speaks(cc, routed, fake_hass):
    pushes, spoken = routed
    safety = _safety(cc, fake_hass)
    fake_hass.states.set("person.abi", "not_home")
    fake_hass.states.set("binary_sensor.front_door", "on", device_class="door")
    first = await _first_alert(safety, fake_hass, sleeping=False, anyone_home=False)
    assert not first.get("phone_only")
    await _deliver(cc, fake_hass, first, sleeping=False)
    assert len(spoken) == 1 and pushes == ["intrusion_investigating"]


async def test_confirmed_intrusion_with_residents_home_still_speaks(
        cc, routed, clock, fake_hass):
    pushes, spoken = routed
    safety = _safety(cc, fake_hass, intrusion_requires_confinement=True)
    fake_hass.states.set("person.abi", "home")
    fake_hass.states.set(ALARM, "armed_home")
    fake_hass.states.set("binary_sensor.back_door", "on", device_class="door")
    await _first_alert(safety, fake_hass, sleeping=False, anyone_home=True)
    clock["now"] += 30
    fake_hass.states.set(ALARM, "triggered")
    actions = await safety.tick(sleeping=False, anyone_home=True)
    fake_hass.close_pending()
    confirmed = [a for a in actions if a["type"] == "intrusion_confirmed"]
    assert len(confirmed) == 1 and not confirmed[0].get("phone_only")
    await _deliver(cc, fake_hass, confirmed[0], sleeping=False)
    assert len(spoken) == 1 and pushes == ["intrusion_confirmed"]
