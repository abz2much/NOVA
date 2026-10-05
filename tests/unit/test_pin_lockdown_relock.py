"""Pin the known lockdown finding end to end (8.7.19, tests only).

During an active lockdown, a lock someone unlocks is locked again; if it is
unlocked a second time it is "adopted" with the cover rule ("I secured it and
you reopened it, so you meant it") and left unlocked. The basic case is pinned
in test_cognitive_lockdown_manager.py. This file pins what follows from it in
a real home: the adoption reaches the person through the event listener as a
high (not critical) announcement worded for a door, it survives a restart,
and the background check scheduled for the first relock stays silent. All
tests here are current behaviour that looks wrong.
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


async def test_current_behaviour_the_second_unlock_is_announced_as_left_open_not_as_a_breach(
        cc, fake_hass, announced):
    # Looks wrong: the first unlock during lockdown is locked again silently.
    # The second is announced at "high" urgency (pushed, not a critical alarm)
    # with wording meant for a garage door ("reopened ... I'll leave it open")
    # and the front door stays unlocked for the rest of the lockdown.
    mgr = await _lockdown_with_the_door_locked_by_nova(cc, fake_hass)

    await cc._on_lockdown_state(_event("locked", "unlocked"))
    assert service_calls(fake_hass, "lock", "lock") == [("lock", "lock", {"entity_id": LOCK})]
    assert announced == []
    fake_hass.close_pending()

    await cc._on_lockdown_state(_event("locked", "unlocked"))
    assert announced == [{"type": "lockdown_breach", "urgency": "high", "auto_act": True,
                          "message": "Sir, Front Door reopened after I secured it — "
                                     "I'll leave it open."}]
    assert len(service_calls(fake_hass, "lock", "lock")) == 1        # not locked a second time

    await cc._on_lockdown_state(_event("locked", "unlocked"))        # a third unlock
    assert len(announced) == 1 and len(service_calls(fake_hass, "lock", "lock")) == 1
    assert mgr.active is True


async def test_current_behaviour_the_adopted_lock_stays_exempt_after_a_restart(cc, fake_hass, announced):
    # Looks wrong: the exemption is saved with the lockdown state, so after a
    # Home Assistant restart in the middle of the night the restored lockdown
    # still ignores the front door being unlocked.
    mgr = await _lockdown_with_the_door_locked_by_nova(cc, fake_hass)
    await cc._on_lockdown_state(_event("locked", "unlocked"))
    fake_hass.close_pending()
    await cc._on_lockdown_state(_event("locked", "unlocked"))
    assert LOCK in mgr.exempt_windows

    restored = cc.LockdownManager(fake_hass, {})                      # Nova starts again
    assert restored.active is True and LOCK in restored.exempt_windows
    fake_hass.service_calls.clear()
    assert await restored.handle_state_change(
        LOCK, FakeState(LOCK, "locked"), FakeState(LOCK, "unlocked")) is None
    assert fake_hass.service_calls == []


async def test_current_behaviour_the_relock_check_stays_silent_once_the_lock_is_adopted(
        cc, fake_hass, announced, no_wait):
    # Looks wrong: the first relock schedules a background check that should
    # raise a critical alert if the lock is still unlocked 25 s later. If the
    # second unlock lands first, the check sees the lock as adopted and says
    # nothing: no critical alert is ever raised for an unlocked front door.
    mgr = await _lockdown_with_the_door_locked_by_nova(cc, fake_hass)
    await cc._on_lockdown_state(_event("locked", "unlocked"))          # relocked, check scheduled
    pending_checks = list(fake_hass._tasks)
    fake_hass._tasks = []
    await cc._on_lockdown_state(_event("locked", "unlocked"))          # adopted
    fake_hass.states.set(LOCK, "unlocked", friendly_name="Front Door")
    announced.clear()
    for check in pending_checks:
        await check
    assert announced == []
    assert fake_hass.states.get(LOCK).state == "unlocked" and mgr.active is True


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
