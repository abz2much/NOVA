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
        # Runs the hand-off at once; LoopHass checks the real thread rule.
        self.loop = SimpleNamespace(call_soon_threadsafe=lambda cb, *a: cb(*a))

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


def test_an_alarm_seen_on_a_worker_thread_is_still_announced(hz, loop_hass, in_worker):
    # HA 2026.11 refuses async_create_task off its loop; the listener runs
    # on a worker thread, so every leak/smoke alarm was dropped (8.85.0).
    assert in_worker(hz.on_state_changed, loop_hass, {},
                     _event("binary_sensor.x", "moisture", "off", "on"))
    loop_hass.settle()
    assert loop_hass.tasks == ["nova_hazard_announce"]


def test_run_on_loop_after_shutdown_does_not_raise(load, loop_hass):
    common = load("core_common")
    loop_hass.close()

    async def _never():
        raise AssertionError("must not run")
    assert common.run_on_loop(loop_hass, _never, "nova_hazard_announce") is False


def test_the_whole_path_from_a_worker_thread_announces_once(load, monkeypatch,
                                                            loop_hass, in_worker):
    rt = load("core_runtime")
    state = load("core_state")._CORE
    monkeypatch.setattr(state, "running", True)
    monkeypatch.setattr(state, "hass", loop_hass)
    monkeypatch.setattr(state, "config", {})
    monkeypatch.setattr(state, "ignore_mgr", SimpleNamespace(is_ignored=lambda e: True))
    monkeypatch.setattr(load("entity_filter"), "is_excluded", lambda h, e, *a: False)
    monkeypatch.setattr(load("core_common"), "_live_honorific", lambda h: "")
    in_worker(rt._on_state_changed, _event("binary_sensor.x", "smoke", "off", "on"))
    loop_hass.settle()
    assert loop_hass.tasks == ["nova_hazard_announce"]


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


# ── still on, repeats and all clear (8.85.10) ───────────────────────────────

class _House(_Hass):
    """A house whose sensors a test can change between ticks."""
    def __init__(self):
        super().__init__()
        self.sensors = {}
        self.states = SimpleNamespace(async_all=lambda domain=None: list(self.sensors.values()))

    def set(self, entity_id="binary_sensor.kitchen_smoke", state="on", dc="smoke",
            name="Kitchen smoke"):
        self.sensors[entity_id] = SimpleNamespace(
            entity_id=entity_id, state=state,
            attributes={"device_class": dc, "friendly_name": name})


@pytest.fixture
def follow(hz, monkeypatch):
    """follow_up() against a house and a clock the test moves."""
    monkeypatch.setattr(hz, "_active", {}, raising=False)
    clock = {"now": 10_000.0}
    monkeypatch.setattr(hz, "_now", lambda: clock["now"], raising=False)
    house = _House()

    def tick(advance=0):
        clock["now"] += advance
        return hz.follow_up(house, {})
    return types.SimpleNamespace(house=house, tick=tick, clock=clock)


import types  # noqa: E402


def test_an_alarm_already_on_when_nova_starts_is_announced_once(follow):
    follow.house.set()
    (action,) = follow.tick()
    assert action["type"] == "critical_hazard" and action["urgency"] == "critical"
    assert action["entity_id"] == "binary_sensor.kitchen_smoke"
    assert "Kitchen smoke" in action["message"] and "still active" in action["message"]
    assert follow.tick(10) == []            # not again straight away


def test_an_alarm_that_was_just_announced_is_not_announced_again_by_the_next_tick(hz, follow):
    follow.house.set(state="on")
    event = _event("binary_sensor.kitchen_smoke", "smoke", "off", "on", name="Kitchen smoke")
    assert hz.on_state_changed(follow.house, {}, event)
    assert follow.tick(5) == []


def test_an_alarm_that_stays_on_repeats_every_five_minutes_up_to_six_times(follow):
    follow.house.set()
    follow.tick()                                   # the start announcement
    assert follow.tick(299) == []                   # not yet five minutes
    told = follow.tick(1)                           # five minutes: the first repeat
    for _ in range(7):
        told += follow.tick(300)
    assert len(told) == 6                           # six repeats, then it stops
    assert all(a["urgency"] == "critical" for a in told)
    assert "5 minutes" in told[0]["message"] and "30 minutes" in told[-1]["message"]


def test_a_cleared_alarm_is_announced_once_when_it_had_been_on_for_a_minute(follow):
    follow.house.set()
    follow.tick()
    follow.house.set(state="off")
    (action,) = follow.tick(61)
    assert action["type"] == "hazard_cleared" and action["urgency"] == "high"
    assert "Kitchen smoke" in action["message"] and "normal" in action["message"]
    assert follow.tick(30) == []


def test_a_blip_shorter_than_a_minute_has_no_cleared_message(follow):
    follow.house.set()
    follow.tick()
    follow.house.set(state="off")
    assert follow.tick(30) == []


@pytest.mark.parametrize("gone", ["unavailable", "unknown"])
def test_an_alarm_that_goes_unavailable_is_dropped_without_a_cleared_message(follow, gone):
    """A dead battery is not an all clear."""
    follow.house.set()
    follow.tick()
    follow.house.set(state=gone)
    assert follow.tick(120) == []
    follow.house.set(state="off")
    assert follow.tick(120) == []


def test_the_users_exclusion_is_respected_by_the_follow_up(follow, load, monkeypatch):
    monkeypatch.setattr(load("entity_filter"), "is_excluded", lambda h, e, *a: True)
    follow.house.set()
    assert follow.tick() == []


@pytest.mark.parametrize("dc", ["door", "motion", "tamper"])
def test_only_smoke_co_gas_and_leak_sensors_are_followed(follow, dc):
    follow.house.set(entity_id="binary_sensor.x", dc=dc)
    assert follow.tick() == []
