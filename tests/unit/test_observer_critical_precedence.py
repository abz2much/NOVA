"""Critical safety takes precedence in the observer's pre-filters (Phase 8).

A critical hazard turning on (smoke, gas, carbon monoxide, water leak) is
never dropped by a targeted mute, a debounce, the sibling-group debounce,
the classifier rate limit or the flap hold. The user's own entity exclusion
still applies, and ordinary events keep every shortcut.

Focused run:
    python -m pytest tests/unit/test_observer_critical_precedence.py -q
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


class _Hass:
    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro, name=None):
        self.tasks.append(coro)
        coro.close()


def _event(eid, dc, old, new, held_s=600.0):
    t0 = datetime(2026, 9, 26, 12, 0, 0)
    mk = lambda s, t: SimpleNamespace(state=s, last_changed=t,
                                      attributes={"device_class": dc, "friendly_name": "Hall"})
    return SimpleNamespace(data={"entity_id": eid, "old_state": mk(old, t0),
                                 "new_state": mk(new, t0 + timedelta(seconds=held_s))})


@pytest.fixture
def obs(load, monkeypatch):
    o = load("observer")
    core = load("cognitive_core")
    ef = load("entity_filter")
    hass = _Hass()
    monkeypatch.setattr(o._STATE, "running", True)
    monkeypatch.setattr(o._STATE, "hass", hass)
    monkeypatch.setattr(o, "_cognition_enabled", lambda: False)
    monkeypatch.setattr(o, "_record_for_context", lambda e: None)
    monkeypatch.setattr(o, "_record_classifier_call", lambda: None)
    monkeypatch.setattr(ef, "is_excluded", lambda h, e, *a: False)
    blocks = {"ignored": False, "group": False, "debounce": False, "rate": False}
    monkeypatch.setattr(core, "is_ignored", lambda e: blocks["ignored"])
    monkeypatch.setattr(o, "_group_debounced", lambda e, i: blocks["group"])
    monkeypatch.setattr(o, "_debounced", lambda e, i=30.0: blocks["debounce"])
    monkeypatch.setattr(o, "_classifier_rate_limited", lambda: blocks["rate"])
    return o, hass, blocks, ef


SMOKE = ("binary_sensor.hall_smoke", "smoke", "off", "on")
DOOR = ("binary_sensor.hall_door", "door", "off", "on")


@pytest.mark.parametrize("block", ["ignored", "group", "debounce", "rate"])
def test_critical_hazard_passes_every_shortcut(obs, block):
    o, hass, blocks, _ = obs
    blocks[block] = True
    o._on_state_changed(_event(*SMOKE))
    assert len(hass.tasks) == 1


@pytest.mark.parametrize("block", ["ignored", "group", "debounce", "rate"])
def test_ordinary_events_keep_every_shortcut(obs, block):
    o, hass, blocks, _ = obs
    blocks[block] = True
    o._on_state_changed(_event(*DOOR))
    assert hass.tasks == []


def test_a_quick_retrip_is_not_dropped_as_flapping(obs):
    o, hass, _, _ = obs
    o._on_state_changed(_event(*SMOKE, held_s=1.0))
    assert len(hass.tasks) == 1
    o._on_state_changed(_event(*DOOR, held_s=1.0))
    assert len(hass.tasks) == 1


def test_all_clear_is_not_critical(obs):
    o, hass, blocks, _ = obs
    blocks["rate"] = True
    o._on_state_changed(_event("binary_sensor.hall_smoke", "smoke", "on", "off"))
    assert hass.tasks == []


def test_the_users_exclusion_still_wins(obs, monkeypatch):
    o, hass, _, ef = obs
    monkeypatch.setattr(ef, "is_excluded", lambda h, e, *a: True)
    o._on_state_changed(_event(*SMOKE))
    assert hass.tasks == []
