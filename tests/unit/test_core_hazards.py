"""Smoke, CO, gas and leak alarms from the cognitive core (8.41.0).

The core runs on every install, so these alarms no longer depend on the
Observer, which is off by default. A new alarm is a critical action with the
fixed hazard sentence; an all-clear, an ordinary sensor or an excluded entity
gives nothing, and ignore rules never drop one.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest


def _event(eid, dc, old, new, name="Kitchen detector"):
    t0 = datetime(2026, 10, 12, 12, 0, 0)
    mk = lambda s: SimpleNamespace(state=s, last_changed=t0,
                                   attributes={"device_class": dc, "friendly_name": name})
    return SimpleNamespace(data={"entity_id": eid, "old_state": mk(old), "new_state": mk(new)})


class _Hass:
    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro, name=None):
        self.tasks.append(coro)
        coro.close()


@pytest.fixture
def hz(load, monkeypatch):
    mod = load("core_hazards")
    monkeypatch.setattr(load("entity_filter"), "is_excluded", lambda h, e, *a: False)
    monkeypatch.setattr(load("core_common"), "_live_honorific", lambda h: "")
    return mod


@pytest.mark.parametrize("dc,new", [("smoke", "on"), ("carbon_monoxide", "on"),
                                    ("gas", "detected"), ("moisture", "wet")])
def test_a_new_alarm_is_a_critical_action(hz, dc, new):
    action = hz.hazard_action(_Hass(), _event(f"binary_sensor.x_{dc}", dc, "off", new))
    assert action["type"] == "critical_hazard"
    assert action["urgency"] == "critical"
    assert action["entity_id"] == f"binary_sensor.x_{dc}"
    assert "Kitchen detector" in action["message"]


@pytest.mark.parametrize("dc,old,new", [
    ("smoke", "on", "off"),          # all clear
    ("smoke", "on", "on"),           # still on: not a new alarm
    ("smoke", "off", "unavailable"),
    ("door", "off", "on"),           # not a hazard sensor
])
def test_anything_else_is_not_an_alarm(hz, dc, old, new):
    assert hz.hazard_action(_Hass(), _event("binary_sensor.x", dc, old, new)) is None


def test_the_users_exclusion_wins(hz, load, monkeypatch):
    monkeypatch.setattr(load("entity_filter"), "is_excluded", lambda h, e, *a: True)
    assert hz.hazard_action(_Hass(), _event("binary_sensor.x", "smoke", "off", "on")) is None


def test_on_state_changed_schedules_one_announcement(hz):
    hass = _Hass()
    assert hz.on_state_changed(hass, {}, _event("binary_sensor.x", "smoke", "off", "on"))
    assert len(hass.tasks) == 1
    assert not hz.on_state_changed(hass, {}, _event("binary_sensor.y", "door", "off", "on"))
    assert len(hass.tasks) == 1


async def test_delivery_is_the_core_alert_path(hz, load, monkeypatch):
    seen = []

    async def _emit(hass, config, action, sleeping):
        seen.append((action["type"], action["urgency"]))
    monkeypatch.setattr(load("core_delivery"), "_emit_action", _emit)
    monkeypatch.setattr(load("sleep_detection"), "is_sleeping", lambda *a, **k: (True, "night"))
    await hz.async_announce(_Hass(), {}, {"type": "critical_hazard", "urgency": "critical",
                                          "message": "m", "entity_id": "binary_sensor.x"})
    assert seen == [("critical_hazard", "critical")]


def test_the_core_listener_announces_before_ignore_rules(load, monkeypatch):
    rt = load("core_runtime")
    state = load("core_state")._CORE
    hass = _Hass()
    monkeypatch.setattr(state, "running", True)
    monkeypatch.setattr(state, "hass", hass)
    monkeypatch.setattr(state, "config", {})
    ignored = SimpleNamespace(is_ignored=lambda e: True)
    monkeypatch.setattr(state, "ignore_mgr", ignored)
    monkeypatch.setattr(load("entity_filter"), "is_excluded", lambda h, e, *a: False)
    monkeypatch.setattr(load("core_common"), "_live_honorific", lambda h: "")
    rt._on_state_changed(_event("binary_sensor.x", "smoke", "off", "on"))
    assert len(hass.tasks) == 1
