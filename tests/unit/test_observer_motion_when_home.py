"""Motion, occupancy and presence while the household is home (v8.7.1).

These sensors change all day while people are in, and sending each change
to the AI classifier used up the hourly budget (100 calls) on a real home.
While the household is home they are kept as recent context but not
classified. When everyone is away, or presence is unknown, they still are,
and a local cognition anomaly still escalates.

Focused run:
    python -m pytest tests/unit/test_observer_motion_when_home.py -q
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


def _event(eid, dc, old="off", new="on"):
    t0 = datetime(2026, 10, 4, 12, 0, 0)
    mk = lambda s, t: SimpleNamespace(state=s, last_changed=t,
                                      attributes={"device_class": dc, "friendly_name": "Hall"})
    return SimpleNamespace(data={"entity_id": eid, "old_state": mk(old, t0),
                                 "new_state": mk(new, t0 + timedelta(seconds=600))})


@pytest.fixture
def obs(load, monkeypatch):
    o = load("observer")
    core = load("cognitive_core")
    ef = load("entity_filter")
    hass = _Hass()
    context = []
    presence = {"value": "home"}
    monkeypatch.setattr(o._STATE, "running", True)
    monkeypatch.setattr(o._STATE, "hass", hass)
    monkeypatch.setattr(o, "_cognition_enabled", lambda: False)
    monkeypatch.setattr(o, "_record_for_context", lambda e: context.append(e))
    monkeypatch.setattr(o, "_record_classifier_call", lambda: None)
    monkeypatch.setattr(ef, "is_excluded", lambda h, e, *a: False)
    monkeypatch.setattr(core, "is_ignored", lambda e: False)
    monkeypatch.setattr(o, "_group_debounced", lambda e, i: False)
    monkeypatch.setattr(o, "_debounced", lambda e, i=30.0: False)
    monkeypatch.setattr(o, "_classifier_rate_limited", lambda: False)
    monkeypatch.setattr(o, "_household_presence", lambda h: presence["value"])
    return o, hass, context, presence


@pytest.mark.parametrize("dc", ["motion", "occupancy", "presence", "moving"])
def test_motion_while_home_is_context_only(obs, dc):
    o, hass, context, _ = obs
    o._on_state_changed(_event("binary_sensor.hall_ms", dc))
    assert hass.tasks == []          # not sent to the classifier
    assert len(context) == 1         # still recent context


@pytest.mark.parametrize("presence", ["away", "unknown"])
def test_motion_while_away_or_unknown_is_classified(obs, presence):
    o, hass, _, state = obs
    state["value"] = presence
    o._on_state_changed(_event("binary_sensor.hall_ms", "motion"))
    assert len(hass.tasks) == 1


def test_a_door_while_home_is_still_classified(obs):
    o, hass, _, _ = obs
    o._on_state_changed(_event("binary_sensor.hall_door", "door"))
    assert len(hass.tasks) == 1


def test_a_presence_error_keeps_classifying(obs, monkeypatch):
    o, hass, _, _ = obs

    def _boom(h):
        raise RuntimeError("alarm source unreadable")

    monkeypatch.setattr(o, "_household_presence", _boom)
    o._on_state_changed(_event("binary_sensor.hall_ms", "motion"))
    assert len(hass.tasks) == 1


def test_a_cognition_anomaly_still_escalates_motion_while_home(obs, monkeypatch, load):
    o, hass, _, _ = obs
    cognition = load("cognition")
    monkeypatch.setattr(o, "_cognition_enabled", lambda: True)
    monkeypatch.setattr(o, "_cognition_threshold", lambda: 0.5)
    monkeypatch.setattr(cognition, "process",
                        lambda e, t: SimpleNamespace(escalate=True, reason="unusual hour"))
    o._on_state_changed(_event("binary_sensor.hall_ms", "motion"))
    assert len(hass.tasks) == 1
