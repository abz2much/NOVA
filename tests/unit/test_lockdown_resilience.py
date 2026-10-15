"""Lockdown keeps working when the rest of the cognitive core does not.

The manager is made on demand if the core's start never set it up, and
lockdown follows the alarm by events, not by the loop: arming engages it, a
confirmed disarm lifts it. An alarm that reads unavailable or unknown (its
cloud dropped) is neither, and holds whatever lockdown there is.
"""
import sys

import pytest

from fakes import FakeState

PANEL = "alarm_control_panel.home"


@pytest.fixture(autouse=True)
def _isolate_lockdown(tmp_path, monkeypatch, cognitive_core):
    """A private lockdown state file and a fresh core for every test."""
    monkeypatch.setattr(cognitive_core, "LOCKDOWN_STATE_PATH",
                        str(tmp_path / "lockdown_state.json"))
    _reset(cognitive_core)
    yield


def _reset(cc):
    core = cc._CORE
    core.lockdown_mgr, core.hass, core.alarm_unsub = None, None, None
    core.config = {"honorific": "sir", "lockdown_auto_on_arm": True}


def _select_alarm(cc, entity_id):
    cc._CORE.config["security_alarm_entity"] = entity_id


async def _armed_start(cc, hass, state="armed_night"):
    """The selected panel in ``state`` when lockdown is set up."""
    _select_alarm(cc, PANEL)
    hass.states.set(PANEL, state)
    await cc.ensure_lockdown(hass, cc._CORE.config)
    hass.close_pending()


async def _alarm_now(cc, hass, state, why="test", **kw):
    hass.states.set(PANEL, state)
    await cc._sync_lockdown_to_alarm(why, **kw)
    hass.close_pending()


# ── the manager on demand ───────────────────────────────────────────────────

async def test_a_manual_request_makes_the_manager_when_start_did_not(cognitive_core, fake_hass):
    for on in (True, False):
        assert await cognitive_core.request_lockdown(on, reason="test", hass=fake_hass) is True
        fake_hass.close_pending()
        assert cognitive_core._CORE.lockdown_mgr is not None
        assert cognitive_core.is_lockdown() is on


async def test_a_request_with_no_hass_anywhere_is_refused_safely(cognitive_core):
    assert await cognitive_core.request_lockdown(True, reason="test") is False
    assert cognitive_core.is_lockdown() is False


# ── following the alarm ─────────────────────────────────────────────────────

async def test_armed_at_setup_comes_back_locked_down(cognitive_core, fake_hass):
    await _armed_start(cognitive_core, fake_hass, "armed_away")
    assert cognitive_core.is_lockdown() is True
    assert cognitive_core._CORE.lockdown_mgr.auto is True


async def test_a_disarm_lifts_the_alarms_lockdown(cognitive_core, fake_hass):
    await _armed_start(cognitive_core, fake_hass, "armed_home")
    await _alarm_now(cognitive_core, fake_hass, "disarmed")
    assert cognitive_core.is_lockdown() is False


@pytest.mark.parametrize("lost", ["unavailable", "unknown"])
async def test_a_dropped_alarm_holds_the_lockdown(cognitive_core, fake_hass, monkeypatch, lost):
    # The live bug: a cloud alarm going unavailable lifted an armed night
    # lockdown as if it had been disarmed.
    monkeypatch.setattr(cognitive_core, "_ALARM_INDET_LOG_TS", 0.0)
    await _armed_start(cognitive_core, fake_hass)
    await _alarm_now(cognitive_core, fake_hass, lost)
    assert cognitive_core.is_lockdown() is True
    assert cognitive_core._CORE.lockdown_mgr.auto is True


async def test_coming_back_armed_after_a_drop_stays_locked(cognitive_core, fake_hass, monkeypatch):
    monkeypatch.setattr(cognitive_core, "_ALARM_INDET_LOG_TS", 0.0)
    await _armed_start(cognitive_core, fake_hass)
    await _alarm_now(cognitive_core, fake_hass, "unavailable")
    await _alarm_now(cognitive_core, fake_hass, "armed_night", announce=False)
    assert cognitive_core.is_lockdown() is True


async def test_a_real_disarm_after_a_drop_still_lifts(cognitive_core, fake_hass, monkeypatch):
    monkeypatch.setattr(cognitive_core, "_ALARM_INDET_LOG_TS", 0.0)
    await _armed_start(cognitive_core, fake_hass, "armed_away")
    await _alarm_now(cognitive_core, fake_hass, "unavailable")
    assert cognitive_core.is_lockdown() is True
    await _alarm_now(cognitive_core, fake_hass, "disarmed")
    assert cognitive_core.is_lockdown() is False


async def test_unavailable_from_the_start_engages_nothing(cognitive_core, fake_hass, monkeypatch):
    monkeypatch.setattr(cognitive_core, "_ALARM_INDET_LOG_TS", 0.0)
    await _armed_start(cognitive_core, fake_hass, "unavailable")
    assert cognitive_core.is_lockdown() is False


async def test_a_drop_is_logged_once_per_ten_minutes(cognitive_core, fake_hass, monkeypatch):
    monkeypatch.setattr(cognitive_core, "_ALARM_INDET_LOG_TS", 0.0)
    lines = []
    monkeypatch.setattr(sys.modules["jc.websocket"], "nova_log",
                        lambda category, text: lines.append((category, text)))
    await _armed_start(cognitive_core, fake_hass, "armed_home")
    await _alarm_now(cognitive_core, fake_hass, "unavailable", "first drop")
    await _alarm_now(cognitive_core, fake_hass, "unavailable", "second drop")
    safety = [text for category, text in lines
              if category == "SAFETY" and "unavailable" in text]
    assert len(safety) == 1 and "holding lockdown" in safety[0]


@pytest.mark.parametrize("state, view", [
    ("armed_home", (True, False, False)),
    ("disarmed", (False, True, False)),
    ("unknown", (False, False, True)),
])
async def test_the_alarm_view(cognitive_core, fake_hass, state, view):
    _select_alarm(cognitive_core, PANEL)
    fake_hass.states.set(PANEL, state)
    assert cognitive_core._alarm_state_view(fake_hass) == view


async def test_switching_off_lockdown_on_arm_is_respected(cognitive_core, fake_hass):
    cognitive_core._CORE.config["lockdown_auto_on_arm"] = False
    fake_hass.states.set(PANEL, "armed_away")
    await cognitive_core.ensure_lockdown(fake_hass, cognitive_core._CORE.config)
    fake_hass.close_pending()
    assert cognitive_core.is_lockdown() is False


async def test_auto_on_arm_is_disabled_by_default(cognitive_core, fake_hass):
    _reset(cognitive_core)
    cognitive_core._CORE.config = {
        "security_alarm_entity": "alarm_control_panel.home",
    }
    fake_hass.states.set("alarm_control_panel.home", "armed_away")

    await cognitive_core.ensure_lockdown(fake_hass, cognitive_core._CORE.config)

    assert cognitive_core.is_lockdown() is False
    assert fake_hass.service_calls == []


async def test_sleeping_does_not_lock_or_close_devices_by_default(
        cognitive_core, fake_hass):
    fake_hass.states.set("lock.front_door", "unlocked")
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    safety = cognitive_core.SafetyManager(fake_hass, {})

    actions = await safety.tick(sleeping=True, anyone_home=True)

    assert actions == []
    assert fake_hass.service_calls == []


async def test_legacy_automatic_lockdown_state_is_cleared_silently(
        cognitive_core, fake_hass, tmp_path):
    import json

    path = tmp_path / "lockdown_state.json"
    path.write_text(json.dumps({
        "active": True,
        "since": 123.0,
        "reason": "alarm armed",
        "auto": True,
        "exempt_windows": ["binary_sensor.window"],
        "auto_suppressed": False,
    }))
    cognitive_core.LOCKDOWN_STATE_PATH = str(path)

    mgr = cognitive_core.LockdownManager(fake_hass, {})

    assert mgr.active is False
    assert mgr.auto is False
    assert mgr.exempt_windows == set()
    assert fake_hass.service_calls == []
    assert json.loads(path.read_text())["active"] is False


async def test_manual_lockdown_state_survives_restart_when_auto_is_off(
        cognitive_core, fake_hass, tmp_path):
    import json

    path = tmp_path / "lockdown_state.json"
    path.write_text(json.dumps({
        "active": True,
        "since": 123.0,
        "reason": "manual request",
        "auto": False,
        "exempt_windows": [],
        "auto_suppressed": False,
    }))
    cognitive_core.LOCKDOWN_STATE_PATH = str(path)

    mgr = cognitive_core.LockdownManager(fake_hass, {})

    assert mgr.active is True
    assert mgr.auto is False


async def test_turning_off_automatic_lockdown_clears_only_nova_state(
        cognitive_core, fake_hass):
    _reset(cognitive_core)
    mgr = cognitive_core.LockdownManager(
        fake_hass, {"lockdown_auto_on_arm": True})
    mgr.active = True
    mgr.auto = True
    mgr.reason = "alarm armed"
    cognitive_core._CORE.lockdown_mgr = mgr
    cognitive_core._CORE.hass = fake_hass
    cognitive_core._CORE.config = {"lockdown_auto_on_arm": True}

    await cognitive_core.apply_runtime_config(
        "lockdown_auto_on_arm", False)

    assert mgr.active is False
    assert mgr.auto is False
    assert fake_hass.service_calls == []


async def test_periodic_tick_holds_auto_lockdown_when_alarm_source_missing(
        cognitive_core, fake_hass):
    mgr = cognitive_core.LockdownManager(fake_hass, {
        "lockdown_auto_on_arm": True,
        "security_alarm_entity": "alarm_control_panel.missing",
    })
    mgr.active = True
    mgr.auto = True

    actions = await mgr.tick()

    assert mgr.active is True
    assert mgr.auto is True
    assert actions == []


async def test_disabling_auto_cancels_remaining_alarm_engage_work(
        cognitive_core, fake_hass, monkeypatch):
    import asyncio

    fake_hass.states.set("cover.garage", "open", device_class="garage")
    mgr = cognitive_core.LockdownManager(
        fake_hass, {"lockdown_auto_on_arm": True})
    cognitive_core._CORE.lockdown_mgr = mgr
    cognitive_core._CORE.hass = fake_hass
    cognitive_core._CORE.config = {"lockdown_auto_on_arm": True}
    started = asyncio.Event()
    release = asyncio.Event()

    async def paused_lock_all(request_id=None, automatic_generation=None):
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(mgr, "_lock_all", paused_lock_all)
    task = asyncio.create_task(mgr.engage("alarm armed", auto=True))
    await started.wait()
    await cognitive_core.apply_runtime_config("lockdown_auto_on_arm", False)
    release.set()
    action = await task

    assert action is None
    assert mgr.active is False
    assert fake_hass.service_calls == []


async def test_disabling_auto_cancels_remaining_sleep_sweep(
        cognitive_core, fake_hass):
    import asyncio

    fake_hass.states.set("lock.front", "unlocked")
    fake_hass.states.set("lock.back", "unlocked")
    fake_hass.states.set("cover.garage", "open", device_class="garage")
    safety = cognitive_core.SafetyManager(
        fake_hass, {"lockdown_auto_on_arm": True})
    cognitive_core._CORE.safety_mgr = safety
    cognitive_core._CORE.config = {"lockdown_auto_on_arm": True}
    started = asyncio.Event()
    release = asyncio.Event()
    real_call = fake_hass.services.async_call
    calls = 0

    async def paused_call(domain, service, data=None, blocking=False, **kwargs):
        nonlocal calls
        calls += 1
        await real_call(domain, service, data, blocking=blocking, **kwargs)
        if calls == 1:
            started.set()
            await release.wait()

    fake_hass.services.async_call = paused_call
    # 8.24.0: the sweep runs in the background, not inside tick(), so it is
    # driven directly here; the setting is switched off mid sweep as before.
    safety.sweep_verify_delay = 0
    task = asyncio.create_task(safety._nighttime_lockdown(safety._automatic_generation))
    await started.wait()
    await cognitive_core.apply_runtime_config("lockdown_auto_on_arm", False)
    release.set()
    actions = await task

    assert actions == []
    assert len(fake_hass.service_calls) == 1



# ── open things during a lockdown: closed if Nova can, else left alone ──────

async def _engage(cc, hass):
    await cc.request_lockdown(True, reason="test", hass=hass)
    hass.close_pending()
    return cc._CORE.lockdown_mgr


def _change(entity_id, before, after, device_class):
    attrs = {"device_class": device_class}
    return FakeState(entity_id, before, attrs), FakeState(entity_id, after, attrs)


async def test_a_cover_that_opens_is_closed_again(cognitive_core, fake_hass):
    mgr = await _engage(cognitive_core, fake_hass)
    fake_hass.service_calls.clear()
    fake_hass.states.set("cover.garage_door", "open", device_class="garage")
    action = await mgr.handle_state_change(
        "cover.garage_door", *_change("cover.garage_door", "closed", "open", "garage"))
    fake_hass.close_pending()   # the delayed check is not under test
    assert ("cover", "close_cover", {"entity_id": "cover.garage_door"}) \
        in fake_hass.service_calls
    assert action is None and "cover.garage_door" in mgr._secured_by_us


async def test_a_window_contact_that_opens_is_taken_as_meant(cognitive_core, fake_hass):
    mgr = await _engage(cognitive_core, fake_hass)
    fake_hass.service_calls.clear()
    eid = "binary_sensor.kitchen_window"
    action = await mgr.handle_state_change(eid, *_change(eid, "off", "on", "window"))
    fake_hass.close_pending()
    assert action is None and fake_hass.service_calls == []
    assert eid in mgr.exempt_windows


async def test_a_window_already_open_at_engage_is_left_alone(cognitive_core, fake_hass):
    # A bare contact cannot be closed remotely; a closeable cover would be
    # (see test_lockdown_engage).
    eid = "binary_sensor.window"
    fake_hass.states.set(eid, "on", device_class="window")
    mgr = await _engage(cognitive_core, fake_hass)
    assert eid in mgr.exempt_windows
    fake_hass.service_calls.clear()
    assert await mgr.handle_state_change(eid, *_change(eid, "on", "on", "window")) is None
    assert fake_hass.service_calls == []


async def test_reopened_after_nova_closed_it_raises_a_breach(cognitive_core, fake_hass):
    mgr = await _engage(cognitive_core, fake_hass)
    mgr._secured_by_us.add("cover.garage_door")
    action = await mgr.handle_state_change(
        "cover.garage_door", *_change("cover.garage_door", "closed", "open", "garage"))
    fake_hass.close_pending()
    assert action is not None and action["type"] == "lockdown_breach"
    assert "cover.garage_door" in mgr.exempt_windows
    assert "cover.garage_door" not in mgr._secured_by_us
