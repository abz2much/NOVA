"""Stage E (8.24.0): situations survive a restart or reload.

A "restart" here is: stop situations.py, throw away the old managers and
package state, start situations.py again from the same file, and build new
ones, exactly as setup does. All state is fake; the file is a temporary one.
"""
import json
import sys
import types

import pytest

from cognitive_safety_kit import _isolated_core, cc, clock  # noqa: F401

ALARM = "alarm_control_panel.home_security"


@pytest.fixture
def sit(load, tmp_path, monkeypatch):
    s = load("situations")
    monkeypatch.setattr(s, "STATE_FILE", str(tmp_path / "situations.json"))
    # Write straight away so a restart in the same test reads it back.
    monkeypatch.setattr(s, "_save", lambda: s._write(
        json.loads(json.dumps(s._state, default=str))))
    s.stop()
    yield s
    s.stop()


def restart(sit):
    sit.stop()
    sit.start()


def _manager(cc, fake_hass, **cfg):
    m = cc.SafetyManager(fake_hass, dict({"honorific": "sir",
                                          "security_alarm_entity": ALARM}, **cfg))
    m.restore_situations()
    return m


async def _tick(m, hass, sleeping=False, anyone_home=False):
    actions = await m.tick(sleeping=sleeping, anyone_home=anyone_home)
    hass.close_pending()
    return [a["type"] for a in actions]


def _away(hass):
    hass.states.set("person.abi", "not_home")
    hass.states.set(ALARM, "armed_away")
    hass.states.set("binary_sensor.front_door", "on", device_class="door")


# ── intrusion ───────────────────────────────────────────────────────────────

async def test_an_investigation_survives_a_reload_and_still_confirms(sit, cc, fake_hass, clock):
    sit.start()
    _away(fake_hass)
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    first = _manager(cc, fake_hass)
    assert await _tick(first, fake_hass) == ["intrusion_investigating"]
    clock["now"] += 40
    fake_hass.states.set("binary_sensor.kitchen_motion", "on", device_class="motion")
    await _tick(first, fake_hass)

    restart(sit)                                  # the Configure dialog reloads Nova
    second = _manager(cc, fake_hass)
    assert second._investigation is not None
    assert second._investigation["zones"] == first._investigation["zones"]
    seen = []
    for eid in ("binary_sensor.landing_motion", "binary_sensor.hall_motion"):
        clock["now"] += 40
        fake_hass.states.set(eid, "on", device_class="motion")
        seen += await _tick(second, fake_hass)
    assert "intrusion_investigating" not in seen          # no second first alert
    assert seen.count("intrusion_confirmed") == 1


async def test_a_stale_investigation_is_dropped(sit, cc, fake_hass, clock):
    sit.start()
    _away(fake_hass)
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    await _tick(_manager(cc, fake_hass), fake_hass)
    clock["now"] += 11 * 60                               # down for eleven minutes
    restart(sit)
    m = _manager(cc, fake_hass)
    assert m._investigation is None and m._last_intrusion_alert == 0.0


async def test_an_ended_investigation_is_not_restored(sit, cc, fake_hass, clock):
    sit.start()
    _away(fake_hass)
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    m = _manager(cc, fake_hass)
    await _tick(m, fake_hass)
    m._investigation = None                               # e.g. called off
    await _tick(m, fake_hass)
    restart(sit)
    assert _manager(cc, fake_hass)._investigation is None


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", ""])
async def test_a_corrupt_file_starts_fresh(sit, cc, fake_hass, clock, content):
    with open(sit._file(), "w") as f:
        f.write(content)
    sit.start()
    m = _manager(cc, fake_hass)
    assert m._investigation is None and m._freeze_warned is False
    # and the next save writes a valid file again
    _away(fake_hass)
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    await _tick(m, fake_hass)
    with open(sit._file()) as f:
        assert json.load(f)["intrusion"]["investigation"]["trigger"] == "away"


async def test_nothing_is_saved_before_nova_starts(sit, cc, fake_hass, clock):
    _away(fake_hass)
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    m = cc.SafetyManager(fake_hass, {"honorific": "sir", "security_alarm_entity": ALARM})
    m.restore_situations()                                # not started: a no-op
    await _tick(m, fake_hass)
    import os
    assert not os.path.exists(sit._file())


# ── hazard: the freeze warning ──────────────────────────────────────────────

async def test_the_freeze_warning_does_not_repeat_after_a_restart(sit, cc, fake_hass, clock):
    sit.start()
    fake_hass.states.set("weather.home", "cloudy", temperature=30)
    first = _manager(cc, fake_hass)
    assert await _tick(first, fake_hass) == ["freeze_warning"]
    clock["now"] += 2 * 3600                              # past the one hour cooldown
    restart(sit)
    second = _manager(cc, fake_hass)
    assert await _tick(second, fake_hass) == []


async def test_without_situations_the_freeze_warning_would_repeat(sit, cc, fake_hass, clock):
    # The bug this fixes: a fresh manager has forgotten it already warned.
    fake_hass.states.set("weather.home", "cloudy", temperature=30)
    m = cc.SafetyManager(fake_hass, {"honorific": "sir"})
    assert await _tick(m, fake_hass) == ["freeze_warning"]
    clock["now"] += 2 * 3600
    assert await _tick(cc.SafetyManager(fake_hass, {"honorific": "sir"}), fake_hass) == [
        "freeze_warning"]


# ── delivery ────────────────────────────────────────────────────────────────

@pytest.fixture
def pkg(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    pm = load("package_monitor")
    for d in (pm._STATE, pm._LAST_SPOKEN, pm._TRIGGER_LAST):
        d.clear()
    out = {"spoken": [], "pushed": []}
    monkeypatch.setattr(pm, "_in_quiet_hours", lambda h: False)
    monkeypatch.setattr(pm, "_announcements_on", lambda h: True)
    monkeypatch.setattr(pm, "_log", lambda *a, **k: None)

    async def _announce(hass, msg, *a, **k):
        out["spoken"].append(msg)
    monkeypatch.setattr(load("tts_helper"), "async_announce", _announce)

    async def _push(hass, msg):
        out["pushed"].append(msg)
    monkeypatch.setattr(pm, "_push_phones", _push)
    out["pm"] = pm
    yield out
    for d in (pm._STATE, pm._LAST_SPOKEN, pm._TRIGGER_LAST):
        d.clear()


async def _seen(pkg, hass, present):
    await pkg["pm"].evaluate(hass, None, "Sir", "tts.x", ["media_player.x"],
                             "camera.front_door",
                             {"package": present, "mail": False,
                              "count": 1 if present else 0, "description": ""})


def _restart_packages(sit, pkg):
    restart(sit)
    for d in (pkg["pm"]._STATE, pkg["pm"]._LAST_SPOKEN):   # memory is gone
        d.clear()
    return pkg["pm"].restore_from_situations()


async def test_a_parcel_is_not_announced_again_after_a_restart(sit, pkg, fake_hass):
    sit.start()
    fake_hass.states.set("person.abi", "home")
    await _seen(pkg, fake_hass, True)
    assert pkg["spoken"] == ["Sir, a package has been delivered to the front door."]
    assert _restart_packages(sit, pkg) == 1
    await _seen(pkg, fake_hass, True)                     # still on the step
    assert len(pkg["spoken"]) == 1


async def test_its_pickup_is_still_noticed_after_a_restart(sit, pkg, fake_hass):
    sit.start()
    fake_hass.states.set("person.abi", "not_home")
    await _seen(pkg, fake_hass, True)
    _restart_packages(sit, pkg)
    await _seen(pkg, fake_hass, False)                    # taken while away
    assert pkg["pushed"] == [
        "Sir, a package was just removed from the front door while no one is home."]


async def test_a_day_old_parcel_is_not_restored(sit, pkg, fake_hass, monkeypatch):
    sit.start()
    await _seen(pkg, fake_hass, True)
    import time as _t
    later = _t.time() + 25 * 3600
    monkeypatch.setattr(_t, "time", lambda: later)
    assert _restart_packages(sit, pkg) == 0


# ── open situations reach the world model ───────────────────────────────────

async def test_open_situations_reach_the_world_snapshot(sit, cc, load, fake_hass, clock):
    world = load("world")
    sit.start()
    assert world.read(fake_hass, {}).open_situations == ()
    _away(fake_hass)
    fake_hass.states.set("binary_sensor.hall_motion", "on", device_class="motion")
    await _tick(_manager(cc, fake_hass), fake_hass)
    assert world.read(fake_hass, {}).open_situations == ("intrusion",)
