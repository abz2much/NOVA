"""A lock unlocked twice during lockdown, end to end (8.7.19 pinned it,
8.7.20 fixed it).

During an active lockdown, a lock someone unlocks is locked again. 8.7.19
pinned a second unlock being "adopted" with the cover rule and left unlocked:
announced at high as "I'll leave it open", exempt after a restart, with the
relock check then silent. Since 8.7.20 the second unlock is a critical alert
through the real event listener, the lock is never exempt (not even from an
old state file), and nothing ever sends it an unlock.
"""
from __future__ import annotations

import types

import pytest

from cognitive_safety_kit import _isolated_core, cc, clock, service_calls  # noqa: F401
from fakes import FakeState

LOCK = "lock.front_door"


@pytest.fixture(autouse=True)
def audit_db(load, tmp_path, monkeypatch):
    monkeypatch.setattr(load("action_log"), "_DEFAULT_DB", str(tmp_path / "audit.db"))


@pytest.fixture
def announced(cc, monkeypatch):
    seen = []

    async def fake_emit(hass, config, action, sleeping):
        seen.append(action)
    monkeypatch.setattr(cc, "_emit_action", fake_emit)
    monkeypatch.setattr(cc, "_live_honorific", lambda hass: "sir")
    return seen


@pytest.fixture
def no_wait(cc, monkeypatch):
    async def fake_sleep(_s):
        return None
    monkeypatch.setattr(cc.asyncio, "sleep", fake_sleep)


def _event(old, new):
    return types.SimpleNamespace(data={
        "entity_id": LOCK, "old_state": FakeState(LOCK, old, {"friendly_name": "Front Door"}),
        "new_state": FakeState(LOCK, new, {"friendly_name": "Front Door"})})


async def _lockdown_with_the_door_locked_by_nova(cc, hass):
    hass.states.set(LOCK, "unlocked", friendly_name="Front Door")
    cc._CORE.hass = hass
    cc._CORE.config = {}
    mgr = cc._ensure_lockdown_mgr(hass)
    await mgr.engage("night", auto=False)
    hass.close_pending()
    hass.states.set(LOCK, "locked", friendly_name="Front Door")
    hass.service_calls.clear()
    return mgr


async def test_the_second_unlock_is_a_critical_alert_and_the_lock_is_never_adopted(
        cc, fake_hass, announced):
    mgr = await _lockdown_with_the_door_locked_by_nova(cc, fake_hass)

    await cc._on_lockdown_state(_event("locked", "unlocked"))
    assert service_calls(fake_hass, "lock", "lock") == [("lock", "lock", {"entity_id": LOCK})]
    assert announced == []
    fake_hass.close_pending()

    await cc._on_lockdown_state(_event("locked", "unlocked"))
    assert announced == [{"type": "lockdown_breach", "urgency": "critical", "auto_act": True,
                          "message": "Sir, Front Door was unlocked again during lockdown "
                                     "after I locked it. Please check it."}]
    assert "leave it open" not in announced[0]["message"]
    assert LOCK not in mgr.exempt_windows
    assert len(service_calls(fake_hass, "lock", "lock")) == 1        # not locked a second time
    assert service_calls(fake_hass, "lock", "unlock") == []          # and never unlocked
    assert mgr.active is True


async def test_a_lock_is_never_exempt_after_a_restart(cc, fake_hass, announced, tmp_path):
    mgr = await _lockdown_with_the_door_locked_by_nova(cc, fake_hass)
    await cc._on_lockdown_state(_event("locked", "unlocked"))
    fake_hass.close_pending()
    await cc._on_lockdown_state(_event("locked", "unlocked"))
    assert LOCK not in mgr.exempt_windows

    # A state file written by 8.7.19 or earlier, with the lock adopted.
    import json
    path = cc._lockdown_state_path()
    state = json.load(open(path))
    state["exempt_windows"] = [LOCK, "binary_sensor.bathroom_window"]
    json.dump(state, open(path, "w"))
    restored = cc.LockdownManager(fake_hass, {})                      # Nova starts again
    assert restored.active is True
    assert restored.exempt_windows == {"binary_sensor.bathroom_window"}   # windows still kept
    fake_hass.service_calls.clear()
    alert = await restored.handle_state_change(
        LOCK, FakeState(LOCK, "locked"), FakeState(LOCK, "unlocked", {"friendly_name": "Front Door"}))
    assert alert is None                                               # relocked like any lock
    assert service_calls(fake_hass, "lock", "lock") == [("lock", "lock", {"entity_id": LOCK})]
    fake_hass.close_pending()


async def test_the_relock_check_raises_one_critical_alert_and_sends_nothing(
        cc, fake_hass, announced, no_wait):
    mgr = await _lockdown_with_the_door_locked_by_nova(cc, fake_hass)
    await cc._on_lockdown_state(_event("locked", "unlocked"))          # relocked, check scheduled
    pending_checks = list(fake_hass._tasks)
    fake_hass._tasks = []
    fake_hass.states.set(LOCK, "unlocked", friendly_name="Front Door")
    for check in pending_checks:                                       # 25 s later: still unlocked
        await check
    assert [a["urgency"] for a in announced] == ["critical"]
    await cc._on_lockdown_state(_event("locked", "unlocked"))          # the second unlock event
    assert [a["urgency"] for a in announced] == ["critical"]           # one alert, not two
    assert len(service_calls(fake_hass, "lock", "lock")) == 1
    assert service_calls(fake_hass, "lock", "unlock") == []
    assert mgr.active is True


async def test_a_new_lockdown_starts_with_no_adopted_locks(cc, fake_hass, announced):
    mgr = await _lockdown_with_the_door_locked_by_nova(cc, fake_hass)
    await cc._on_lockdown_state(_event("locked", "unlocked"))
    fake_hass.close_pending()
    await cc._on_lockdown_state(_event("locked", "unlocked"))
    await mgr.disengage("morning", manual=True)
    fake_hass.states.set(LOCK, "unlocked", friendly_name="Front Door")
    fake_hass.service_calls.clear()
    await mgr.engage("night again")
    fake_hass.close_pending()
    assert LOCK not in mgr.exempt_windows
    assert service_calls(fake_hass, "lock", "lock") == [("lock", "lock", {"entity_id": LOCK})]
