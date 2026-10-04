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


class _States:
    def __init__(self):
        self.people = []

    def async_all(self, domain=None):
        assert domain == "person"
        return [SimpleNamespace(state=s) for s in self.people]


class _Hass:
    def __init__(self):
        self.tasks = []
        self.states = _States()

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
    alarm = {"states": []}
    hass.states.people = ["home"]
    alarm_source = load("alarm_source")
    monkeypatch.setattr(alarm_source, "states",
                        lambda h, cfg: [SimpleNamespace(state=s) for s in alarm["states"]])
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
    return o, hass, context, alarm


@pytest.mark.parametrize("dc", ["motion", "occupancy", "presence", "moving"])
def test_motion_while_home_is_context_only(obs, dc):
    o, hass, context, _ = obs
    o._on_state_changed(_event("binary_sensor.hall_ms", dc))
    assert hass.tasks == []          # not sent to the classifier
    assert len(context) == 1         # still recent context


@pytest.mark.parametrize("people", [
    ["not_home"],               # everyone away
    [],                         # no person entities at all
    ["unavailable", "unknown"], # people unreadable, e.g. just after a restart
])
def test_motion_while_away_or_unknown_is_classified(obs, people, monkeypatch):
    # Occupied areas must not count here: the motion being judged occupies
    # its own area, so it would always read as home.
    o, hass, _, _ = obs
    hass.states.people = people
    audio_routing = o.audio_routing
    monkeypatch.setattr(audio_routing, "currently_occupied_areas", lambda h: ["hall"])
    o._on_state_changed(_event("binary_sensor.hall_ms", "motion"))
    assert len(hass.tasks) == 1


def test_motion_while_armed_away_is_classified_even_if_a_person_reads_home(obs):
    o, hass, _, alarm = obs
    alarm["states"] = ["armed_away"]
    o._on_state_changed(_event("binary_sensor.hall_ms", "motion"))
    assert len(hass.tasks) == 1


def test_a_door_while_home_is_still_classified(obs):
    o, hass, _, _ = obs
    o._on_state_changed(_event("binary_sensor.hall_door", "door"))
    assert len(hass.tasks) == 1


def load_alarm_source(o):
    import sys
    return sys.modules[o.__name__.rpartition(".")[0] + ".alarm_source"]


def test_a_presence_error_keeps_classifying(obs, monkeypatch):
    o, hass, _, _ = obs

    def _boom(h, cfg):
        raise RuntimeError("alarm source unreadable")

    monkeypatch.setattr(load_alarm_source(o), "states", _boom)
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
