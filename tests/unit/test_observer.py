"""The observer: the cheap pre filter, debounces and rate limit, the recent
events buffer, the state change handler's order of checks, the start of the
classify and reason pipeline, and its lifecycle. Everything is fake: no
model, speaker or phone is used."""
import sys
import time
import types
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def ob(load, monkeypatch, fake_hass):
    o = load("observer")
    monkeypatch.setattr(o._STATE, "hass", fake_hass)
    monkeypatch.setattr(o._STATE, "config", {})
    monkeypatch.setattr(o._STATE, "entry", None)
    monkeypatch.setattr(load("entity_filter"), "is_excluded", lambda h, e, *a: False)
    o._GROUP_LAST.clear()
    o._STATE.last_seen.clear()
    o._STATE.recent_events.clear()
    o._STATE.classifier_timestamps.clear()
    o._STATE.rate_limit_warn_logged = False
    yield o
    o._STATE.recent_events.clear()
    o._STATE.classifier_timestamps.clear()


T0 = datetime(2026, 10, 1, 12, 0, 0)


def _st(state, dc=None, at=T0, name=None):
    attrs = {}
    if dc:
        attrs["device_class"] = dc
    if name:
        attrs["friendly_name"] = name
    return types.SimpleNamespace(state=state, attributes=attrs, last_changed=at)


def _ev(eid, old, new, dc=None, held=600.0, name=None):
    data = {"entity_id": eid,
            "old_state": None if old is None else _st(old, dc, T0, name),
            "new_state": None if new is None else _st(new, dc, T0 + timedelta(seconds=held),
                                                       name)}
    return types.SimpleNamespace(data=data, time_fired=T0)


# ── noisy ids ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("eid,noisy", [
    ("sensor.heater_w", True), ("binary_sensor.kitchen_window", False),
    ("binary_sensor.kitchen_water_leak", False), ("sensor.solar_KWH", True),
    ("sensor.hall_temp_probe", True), ("sensor.hall_temperature", True),
    ("binary_sensor.phone_bluetooth", True), ("lock.front", False),
    ("sensor.door_count", True), ("sensor.washer_runtime", True),
])
def test_noisy_ids(ob, eid, noisy):
    assert ob._entity_id_looks_noisy(eid) is noisy


# ── person transitions ──────────────────────────────────────────────────────

@pytest.mark.parametrize("eid,old,new,want", [
    ("person.a", "not_home", "home", True),
    ("person.a", "home", "work", True),
    ("person.a", "work", "school", False),
    ("person.a", None, "home", False),
    ("person.a", "home", None, False),
    ("device_tracker.p", "not_home", "home", False),
])
def test_person_home_transition(ob, eid, old, new, want):
    assert ob._is_person_home_transition(_ev(eid, old, new)) is want


# ── pre filter ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("event,drop", [
    (_ev("", "off", "on"), True),
    (_ev("sensor.heater_power", "1", "2"), True),
    (_ev("light.hall", "off", "on"), True),
    (_ev("sensor.leak", "dry", "wet", "moisture"), False),
    (_ev("sensor.leak", "dry", None, "moisture"), True),
    (_ev("sensor.level", "1", "2", "distance"), True),
    (_ev("person.a", "not_home", "home"), False),
    (_ev("person.a", "work", "gym"), True),
    (_ev("fan.hall", "off", "on"), True),
    (_ev("binary_sensor.door", None, "on", "door"), True),
    (_ev("binary_sensor.door", "on", "on", "door"), True),
    (_ev("binary_sensor.door", "off", "unavailable", "door"), True),
    (_ev("binary_sensor.door", "unknown", "on", "door"), True),
    (_ev("binary_sensor.door", "none", "on", "door"), True),
    (_ev("binary_sensor.door", "off", "on", "door", held=3), True),
    (_ev("binary_sensor.door", "off", "on", "door"), False),
    (_ev("lock.front", "locked", "unlocked"), False),
    (_ev("cover.garage", "closed", "open"), False),
    (_ev("vacuum.robot", "docked", "cleaning"), False),
    # a hazard turning on passes even from unknown or after a short hold
    (_ev("binary_sensor.smoke", "unknown", "on", "smoke"), False),
    (_ev("binary_sensor.smoke", "off", "on", "smoke", held=1), False),
])
def test_pre_filter(ob, event, drop):
    assert ob._should_pre_filter(event) is drop


def test_pre_filter_without_last_changed_keeps_the_event(ob):
    event = _ev("binary_sensor.door", "off", "on", "door")
    event.data["old_state"].last_changed = None
    assert ob._should_pre_filter(event) is False


def test_pre_filter_drops_excluded_entities(ob, load, monkeypatch):
    monkeypatch.setattr(load("entity_filter"), "is_excluded", lambda h, e, *a: e == "lock.front")
    assert ob._should_pre_filter(_ev("lock.front", "locked", "unlocked")) is True
    assert ob._should_pre_filter(_ev("lock.back", "locked", "unlocked")) is False


def test_pre_filter_survives_a_broken_exclusion_check(ob, load, monkeypatch):
    def broken(*a):
        raise RuntimeError("config")
    monkeypatch.setattr(load("entity_filter"), "is_excluded", broken)
    assert ob._should_pre_filter(_ev("lock.back", "locked", "unlocked")) is False


def test_pre_filter_alarm_panels(ob, load, monkeypatch):
    alarm = load("alarm_source")
    picked = {"alarm_control_panel.main"}
    monkeypatch.setattr(alarm, "is_selected", lambda h, eid, cfg: eid in picked)
    assert ob._should_pre_filter(_ev("alarm_control_panel.other", "disarmed", "armed_away"))
    assert not ob._should_pre_filter(_ev("alarm_control_panel.main", "disarmed",
                                         "armed_away"))
    # triggered passes even from unavailable and after a short hold
    assert not ob._should_pre_filter(_ev("alarm_control_panel.main", "unavailable",
                                         "triggered", held=1))


# ── small readers ───────────────────────────────────────────────────────────

def test_critical_hazard(ob):
    assert ob._critical_hazard(_ev("binary_sensor.s", "off", "on", "smoke"))
    assert not ob._critical_hazard(_ev("binary_sensor.s", "on", "off", "smoke"))
    assert not ob._critical_hazard(_ev("binary_sensor.d", "off", "on", "door"))
    assert not ob._critical_hazard(_ev("binary_sensor.s", None, "on", "smoke"))
    assert not ob._critical_hazard(types.SimpleNamespace(data=None))


def test_entry_state_recovery(ob):
    assert ob._entry_state_recovery(_ev("binary_sensor.d", "unavailable", "off", "door"))
    assert ob._entry_state_recovery(_ev("lock.f", None, "locked"))
    assert not ob._entry_state_recovery(_ev("binary_sensor.d", "off", "on", "door"))
    assert not ob._entry_state_recovery(_ev("binary_sensor.d", "off", None, "door"))


def test_core_running(ob, load, monkeypatch):
    core = load("core_state")._CORE
    monkeypatch.setattr(core, "running", True)
    assert ob._core_running() is True
    monkeypatch.setattr(core, "running", 0)
    assert ob._core_running() is False


def test_household_readers(ob, load, fake_hass, monkeypatch):
    monkeypatch.setattr(load("alarm_source"), "states", lambda hass, cfg: [])
    monkeypatch.setattr(ob.audio_routing, "currently_occupied_areas", lambda h: ["den"])
    fake_hass.states.set("person.a", "unknown")
    rules = ob.cognitive_rules
    # an occupied room counts for openings, never for the home check
    assert ob._household_presence(fake_hass) == rules.PRESENCE_HOME
    assert ob._household_home() is False
    fake_hass.states.set("person.a", "home")
    assert ob._household_home() is True


def test_household_home_error_reads_as_not_home(ob, monkeypatch):
    monkeypatch.setattr(ob._STATE, "hass", None)
    assert ob._household_home() is False


def test_live_runtime_config_without_an_entry(ob):
    assert ob._live_runtime_config() == {}


def test_live_runtime_config_reads_the_entry(ob, load, monkeypatch):
    entry = object()
    monkeypatch.setattr(ob._STATE, "entry", entry)
    monkeypatch.setattr(load("runtime"), "lifecycle_runtime_config",
                        lambda e: {"x": 1} if e is entry else {})
    assert ob._live_runtime_config() == {"x": 1}


def test_live_honorific(ob, load, fake_hass, monkeypatch):
    hon = load("honorific")
    monkeypatch.setattr(hon, "effective_honorific", lambda hass: "ma'am")
    assert ob._live_honorific(fake_hass) == "ma'am"

    def broken(hass):
        raise RuntimeError("x")
    monkeypatch.setattr(hon, "effective_honorific", broken)
    assert ob._live_honorific(fake_hass) == "sir"


# ── settings ────────────────────────────────────────────────────────────────

@pytest.fixture
def live(ob, monkeypatch):
    values = {}
    monkeypatch.setattr(ob, "_live_runtime_config", lambda: values)
    return values


def test_rate_limit_setting(ob, live, monkeypatch):
    assert ob._effective_rate_limit() == 30
    monkeypatch.setattr(ob._STATE, "config", {"classifier_rate_limit": "12"})
    assert ob._effective_rate_limit() == 12
    live["classifier_rate_limit"] = 5
    assert ob._effective_rate_limit() == 5
    live["classifier_rate_limit"] = "bad"
    assert ob._effective_rate_limit() == 12
    monkeypatch.setattr(ob._STATE, "config", {"classifier_rate_limit": None})
    assert ob._effective_rate_limit() == 30


def test_cognition_settings(ob, live, monkeypatch):
    assert ob._cognition_enabled() is True
    assert ob._cognition_threshold() == 0.6
    monkeypatch.setattr(ob._STATE, "config", {"cognition_enabled": False,
                                              "cognition_threshold": "0.8"})
    assert ob._cognition_enabled() is False
    assert ob._cognition_threshold() == 0.8
    live.update(cognition_enabled=1, cognition_threshold="x")
    assert ob._cognition_enabled() is True
    assert ob._cognition_threshold() == 0.8
    live["cognition_threshold"] = 0.3
    assert ob._cognition_threshold() == 0.3
    monkeypatch.setattr(ob._STATE, "config", {"cognition_threshold": "nope"})
    live.clear()
    assert ob._cognition_threshold() == 0.6


def test_group_debounce_setting(ob, live, monkeypatch):
    assert ob._group_debounce_s() == 90.0
    monkeypatch.setattr(ob._STATE, "config", {"observer_group_debounce": "30"})
    assert ob._group_debounce_s() == 30.0
    live["observer_group_debounce"] = 0
    assert ob._group_debounce_s() == 0.0
    live["observer_group_debounce"] = "?"
    assert ob._group_debounce_s() == 30.0
    monkeypatch.setattr(ob._STATE, "config", {"observer_group_debounce": "?"})
    assert ob._group_debounce_s() == 90.0


# ── rate limit and debounces ────────────────────────────────────────────────

def test_rate_limiter(ob, live):
    live["classifier_rate_limit"] = 2
    assert not ob._classifier_rate_limited()
    ob._record_classifier_call()
    ob._record_classifier_call()
    assert ob._classifier_rate_limited()
    live["classifier_rate_limit"] = 0
    assert not ob._classifier_rate_limited()


def test_rate_limiter_forgets_calls_older_than_an_hour(ob, live):
    live["classifier_rate_limit"] = 1
    ob._STATE.classifier_timestamps.append(time.time() - 3700)
    assert not ob._classifier_rate_limited()
    assert len(ob._STATE.classifier_timestamps) == 0


def test_debounce(ob):
    assert ob._debounced("lock.a", 30) is False
    assert ob._debounced("lock.a", 30) is True
    assert ob._debounced("lock.b") is False
    ob._STATE.last_seen["lock.a"] = time.time() - 31
    assert ob._debounced("lock.a", 30) is False


@pytest.mark.parametrize("eid,key", [
    ("binary_sensor.alarm_zone_49", "binary_sensor.alarm_zone"),
    ("light.kitchen_2", "light.kitchen"),
    ("binary_sensor.front_door", None),
    ("sensor.abc_1", None),
    ("noentity", None),
])
def test_group_key(ob, eid, key):
    assert ob._group_key(eid) == key


def test_group_debounce(ob):
    assert ob._group_debounced("binary_sensor.alarm_zone_1", 90) is False
    assert ob._group_debounced("binary_sensor.alarm_zone_2", 90) is True
    assert ob._group_debounced("cover.garage_door_1", 90) is False
    assert ob._group_debounced("binary_sensor.front_door", 90) is False
    assert ob._group_debounced("binary_sensor.front_door", 90) is False
    assert ob._group_debounced("binary_sensor.alarm_zone_3", 0) is False
    ob._GROUP_LAST["binary_sensor.alarm_zone"] = time.time() - 100
    assert ob._group_debounced("binary_sensor.alarm_zone_4", 90) is False


# ── recent events ───────────────────────────────────────────────────────────

def test_recent_context(ob, monkeypatch):
    monkeypatch.setattr(ob.audio_routing, "entity_area", lambda h, e: "den" if "den" in e else None)
    assert ob.get_recent_context() == "quiet — no notable recent activity"
    ob._record_for_context(_ev("binary_sensor.den_door", "off", "on", name="Den Door"))
    ob._record_for_context(_ev("lock.front", None, "unlocked"))
    ob._record_for_context(_ev("lock.front", "locked", None))
    ob.record_camera_event("Drive", "A van stopped", notable=True)
    ob.record_camera_event("Yard", "A cat")
    ob._STATE.recent_events.appendleft({"ts": time.time() - 4000, "entity_id": "x",
                                        "old": "a", "new": "b", "fname": "Old", "area": None})
    ob._STATE.recent_events[2]["ts"] = time.time() - 125
    assert ob.get_recent_context().split("\n") == [
        "  [0s ago] [den] Den Door: off → on",
        "  [2m ago] lock.front: None → unlocked",
        "  [0s ago] ⚠ Camera Drive: A van stopped",
        "  [0s ago] Camera Yard: A cat",
    ]
    assert ob.get_recent_context(60).count("\n") == 2


def test_recent_context_keeps_the_last_fifteen(ob, monkeypatch):
    monkeypatch.setattr(ob.audio_routing, "entity_area", lambda h, e: None)
    for i in range(20):
        ob._record_for_context(_ev(f"lock.l{i}", "locked", "unlocked"))
    lines = ob.get_recent_context().split("\n")
    assert len(lines) == 15 and "lock.l19" in lines[-1] and "lock.l5:" in lines[0]


def test_camera_event_shape(ob):
    ob.record_camera_event("Drive", "Parcel", category="delivery", notable=1)
    entry = ob._STATE.recent_events[-1]
    assert {k: entry[k] for k in ("kind", "camera", "summary", "category", "notable",
                                  "area")} == {
        "kind": "camera", "camera": "Drive", "summary": "Parcel", "category": "delivery",
        "notable": True, "area": None}


# ── the state change handler ────────────────────────────────────────────────

@pytest.fixture
def flow(ob, load, monkeypatch, fake_hass):
    """_on_state_changed with every check controllable and the pipeline
    task recorded instead of run."""
    seen = {"tasks": [], "context": [], "calls": 0, "log": []}
    knobs = {"cognition": False, "escalate": False, "drop": False, "critical": False,
             "core": False, "recovery": False, "ignored": False, "group": False,
             "debounce": False, "home": False, "rate": False, "watching": False,
             "alarm_core": False}
    monkeypatch.setattr(ob._STATE, "running", True)
    monkeypatch.setattr(ob, "_cognition_enabled", lambda: knobs["cognition"])
    monkeypatch.setattr(ob, "_should_pre_filter", lambda e: knobs["drop"])
    monkeypatch.setattr(ob, "_critical_hazard", lambda e: knobs["critical"])
    monkeypatch.setattr(ob, "_core_running", lambda: knobs["core"])
    monkeypatch.setattr(ob, "_entry_state_recovery", lambda e: knobs["recovery"])
    monkeypatch.setattr(load("cognitive_core"), "is_ignored", lambda e: knobs["ignored"])
    monkeypatch.setattr(ob, "_group_debounced", lambda e, i: knobs["group"])
    monkeypatch.setattr(ob, "_debounced", lambda e, i=30.0: seen.setdefault("interval", i)
                        and knobs["debounce"])
    monkeypatch.setattr(ob, "_household_home", lambda: knobs["home"])
    monkeypatch.setattr(ob, "_alarm_watching", lambda: knobs["watching"])
    monkeypatch.setattr(ob, "_alarm_left_to_core", lambda eid: knobs["alarm_core"])
    monkeypatch.setattr(ob, "_classifier_rate_limited", lambda: knobs["rate"])
    monkeypatch.setattr(ob, "_record_classifier_call",
                        lambda: seen.__setitem__("calls", seen["calls"] + 1))
    monkeypatch.setattr(ob, "_record_for_context", lambda e: seen["context"].append(e))
    cog = load("cognition")
    monkeypatch.setattr(cog, "process", lambda e, t: types.SimpleNamespace(
        escalate=knobs["escalate"], reason="odd hour"))
    monkeypatch.setattr(cog, "mark_unobserved", lambda: seen.setdefault("unobserved", True))
    ws = types.ModuleType("jc.websocket")
    ws.nova_log = lambda *a: seen["log"].append(a)
    monkeypatch.setitem(sys.modules, "jc.websocket", ws)

    async def process(event):
        return None

    def task(coro, name=None):
        seen["tasks"].append(coro)
        coro.close()
    monkeypatch.setattr(ob, "_process_event", process)
    monkeypatch.setattr(fake_hass, "async_create_task", task)
    return ob, knobs, seen


DOOR = _ev("binary_sensor.door", "off", "on", "door")
MOTION = _ev("binary_sensor.hall", "off", "on", "motion")


def test_handler_ignores_events_while_stopped(flow, monkeypatch):
    ob, knobs, seen = flow
    monkeypatch.setattr(ob._STATE, "running", False)
    ob._on_state_changed(DOOR)
    assert seen["tasks"] == [] and seen["context"] == []


def test_handler_sends_an_ordinary_event_to_the_pipeline(flow):
    ob, knobs, seen = flow
    ob._on_state_changed(DOOR)
    assert len(seen["tasks"]) == 1 and seen["context"] == [DOOR] and seen["calls"] == 1
    assert seen["interval"] == ob.DEBOUNCE_DEFAULT_S
    assert seen.get("unobserved") is True


@pytest.mark.parametrize("knob", ["drop", "recovery", "ignored", "group", "debounce"])
def test_handler_shortcuts(flow, knob):
    ob, knobs, seen = flow
    knobs[knob] = True
    ob._on_state_changed(DOOR)
    assert seen["tasks"] == [] and seen["calls"] == 0


def test_a_cognition_anomaly_rescues_a_filtered_event(flow):
    ob, knobs, seen = flow
    knobs.update(drop=True, cognition=True, escalate=True)
    ob._on_state_changed(DOOR)
    assert len(seen["tasks"]) == 1
    assert seen["log"] == [("CLASSIFY", "cognition escalated binary_sensor.door: odd hour")]
    assert "unobserved" not in seen


def test_a_failing_cognition_never_escalates(flow, load, monkeypatch):
    ob, knobs, seen = flow
    knobs.update(drop=True, cognition=True)

    def broken(e, t):
        raise RuntimeError("model")
    monkeypatch.setattr(load("cognition"), "process", broken)
    ob._on_state_changed(DOOR)
    assert seen["tasks"] == []


def test_critical_is_left_to_a_running_core(flow):
    ob, knobs, seen = flow
    knobs.update(critical=True, core=True)
    ob._on_state_changed(DOOR)
    assert seen["tasks"] == []


@pytest.mark.parametrize("knob", ["recovery", "ignored", "group", "debounce", "rate"])
def test_critical_skips_every_shortcut_without_the_core(flow, knob):
    ob, knobs, seen = flow
    knobs.update(critical=True, **{knob: True})
    ob._on_state_changed(DOOR)
    assert len(seen["tasks"]) == 1


def test_motion_while_home_is_context_only(flow):
    ob, knobs, seen = flow
    knobs["home"] = True
    ob._on_state_changed(MOTION)
    assert seen["tasks"] == [] and seen["context"] == [MOTION] and seen["calls"] == 0
    assert seen["interval"] == ob.DEBOUNCE_MOTION_S


def test_motion_while_home_with_an_anomaly_is_classified(flow):
    ob, knobs, seen = flow
    knobs.update(home=True, cognition=True, escalate=True)
    ob._on_state_changed(MOTION)
    assert len(seen["tasks"]) == 1


# Armed away the alarm watches the house (8.81.0): motion is context only,
# even an unusual one, and never becomes "motion while no one is home".
@pytest.mark.parametrize("anomaly", [False, True])
def test_motion_while_the_alarm_is_armed_away_is_context_only(flow, anomaly):
    ob, knobs, seen = flow
    knobs.update(watching=True, cognition=anomaly, escalate=anomaly)
    ob._on_state_changed(MOTION)
    assert seen["tasks"] == [] and seen["context"] == [MOTION] and seen["calls"] == 0


def test_a_door_while_the_alarm_is_armed_away_still_goes_through(flow):
    ob, knobs, seen = flow
    knobs["watching"] = True
    ob._on_state_changed(DOOR)
    assert len(seen["tasks"]) == 1


ALARM_GOES_OFF = _ev("alarm_control_panel.home_security", "armed_away", "triggered", None)


def test_an_alarm_the_core_handles_is_context_only(flow):
    ob, knobs, seen = flow
    knobs["alarm_core"] = True
    ob._on_state_changed(ALARM_GOES_OFF)
    assert seen["tasks"] == [] and seen["context"] == [ALARM_GOES_OFF]


def test_an_alarm_without_the_core_still_goes_through(flow):
    ob, knobs, seen = flow
    ob._on_state_changed(ALARM_GOES_OFF)
    assert len(seen["tasks"]) == 1


@pytest.mark.parametrize("eid,selected,running,left", [
    ("alarm_control_panel.home_security", ["alarm_control_panel.home_security"], True, True),
    ("alarm_control_panel.back_homebase", ["alarm_control_panel.home_security"], True, True),
    ("alarm_control_panel.home_security", ["alarm_control_panel.home_security"], False, False),
    ("alarm_control_panel.back_homebase", [], True, False),       # no chosen alarm: as before
])
def test_alarm_left_to_core(ob, load, fake_hass, monkeypatch, eid, selected, running, left):
    monkeypatch.setattr(ob._STATE, "hass", fake_hass)
    monkeypatch.setattr(load("alarm_source"), "entity_ids", lambda hass, cfg=None: tuple(selected))
    monkeypatch.setattr(ob, "_core_running", lambda: running)
    assert ob._alarm_left_to_core(eid) is left


@pytest.mark.parametrize("state,watching", [
    ("armed_away", True), ("armed_vacation", True), ("armed_home", False),
    ("armed_night", False), ("arming", False), ("triggered", False), ("disarmed", False),
])
def test_alarm_watching_reads_the_selected_alarm(ob, load, fake_hass, monkeypatch, state, watching):
    monkeypatch.setattr(ob._STATE, "hass", fake_hass)
    monkeypatch.setattr(load("alarm_source"), "states",
                        lambda hass, cfg: [types.SimpleNamespace(state=state)])
    assert ob._alarm_watching() is watching


def test_alarm_watching_without_an_alarm_or_on_error_is_false(ob, load, fake_hass, monkeypatch):
    monkeypatch.setattr(ob._STATE, "hass", fake_hass)
    monkeypatch.setattr(load("alarm_source"), "states", lambda hass, cfg: [])
    assert ob._alarm_watching() is False
    monkeypatch.setattr(ob._STATE, "hass", None)
    assert ob._alarm_watching() is False


def test_rate_limit_warns_once_and_spares_arrivals(flow, caplog):
    ob, knobs, seen = flow
    knobs["rate"] = True
    with caplog.at_level("WARNING"):
        ob._on_state_changed(DOOR)
        ob._on_state_changed(DOOR)
    assert caplog.text.count("hit rate limit") == 1
    assert seen["tasks"] == [] and ob._STATE.rate_limit_warn_logged is True
    ob._on_state_changed(_ev("person.a", "not_home", "home"))
    assert len(seen["tasks"]) == 1 and ob._STATE.rate_limit_warn_logged is False


# ── the pipeline's first steps ──────────────────────────────────────────────

@pytest.fixture
def pipe(ob, load, monkeypatch):
    seen = {"activity": [], "audits": [], "decide": [], "classify": {}}
    monkeypatch.setattr(load("database"), "save_activity",
                        lambda **kw: seen["activity"].append(kw))
    monkeypatch.setattr(ob.audio_routing, "entity_area", lambda h, e: None)

    async def classify(hass, provider, **kw):
        seen["classify_kw"] = kw
        return dict(seen["classify"])
    monkeypatch.setattr(ob.classifier, "classify", classify)

    async def decide(*a, **kw):
        seen["decide"].append(kw)
        return {"speak": False, "reason": "nothing to say", "urgency": "low"}
    monkeypatch.setattr(ob.reasoning_loop, "decide", decide)

    async def audit(event, **kw):
        seen["audits"].append(kw)
    monkeypatch.setattr(ob, "_audit", audit)
    return seen


async def test_pipeline_skips_events_without_both_states(ob, pipe):
    await ob._process_event(_ev("lock.a", None, "unlocked"))
    assert pipe["activity"] == [] and "classify_kw" not in pipe


async def test_not_worth_considering_is_logged(ob, pipe):
    pipe["classify"] = {"worth_considering": False}
    await ob._process_event(_ev("lock.a", "locked", "unlocked", name="Front"))
    assert pipe["activity"] == [{
        "entity_id": "lock.a", "category": "classified", "urgency": "low",
        "message": "Front: locked → unlocked — not worth considering", "was_spoken": False,
        "source": "observer"}]
    assert pipe["audits"] == []
    kw = pipe["classify_kw"]
    assert (kw["entity_id"], kw["old_state"], kw["new_state"], kw["friendly_name"]) == (
        "lock.a", "locked", "unlocked", "Front")


async def test_a_passed_over_hazard_is_audited(ob, pipe):
    pipe["classify"] = {"worth_considering": False, "category": "safety", "urgency": "low"}
    await ob._process_event(_ev("binary_sensor.s", "off", "on", "smoke"))
    assert pipe["audits"] == [{"result": "ignored", "category": "safety",
                               "classifier_urgency": "low",
                               "reason": "the classifier judged it not worth considering"}]


async def test_an_opening_with_the_household_home_is_silent(ob, pipe, monkeypatch):
    pipe["classify"] = {"worth_considering": True, "urgency": "medium",
                        "category": "doors_windows"}
    monkeypatch.setattr(ob, "_household_presence", lambda h: ob.cognitive_rules.PRESENCE_HOME)
    await ob._process_event(_ev("binary_sensor.d", "off", "on", "door", name="Door"))
    assert pipe["activity"][-1]["message"] == (
        "Door: off → on — household present, not announced")
    assert pipe["decide"] == []


async def test_a_silent_decision_is_audited(ob, pipe, load, monkeypatch, fake_hass):
    pipe["classify"] = {"worth_considering": True, "urgency": "medium", "category": "locks"}
    monkeypatch.setattr(ob.audio_routing, "currently_occupied_areas", lambda h: [])
    monkeypatch.setattr(ob.output_gate, "recent_announcements", lambda n=5: ["x"])
    await ob._process_event(_ev("lock.a", "locked", "unlocked", name="Front"))
    assert pipe["activity"][-1]["message"] == "Front: locked → unlocked — flagged for reasoning"
    (kw,) = pipe["decide"]
    assert kw["event_summary"] == "Front (lock.a) changed from locked to unlocked"
    assert kw["recent_announcements"] == ["x"]
    assert (kw["classifier_urgency"], kw["classifier_category"]) == ("medium", "locks")
    (audit,) = pipe["audits"]
    assert audit["result"] == "ignored" and audit["reason"] == "nothing to say"


async def test_pipeline_errors_are_contained(ob, pipe, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("provider")
    monkeypatch.setattr(ob.classifier, "classify", boom)
    await ob._process_event(_ev("lock.a", "locked", "unlocked"))


def test_event_time(ob):
    assert ob._event_at(types.SimpleNamespace(time_fired=T0)).startswith("2026-10-01T")
    assert ob._event_at(types.SimpleNamespace(time_fired="bad"))


# ── lifecycle ───────────────────────────────────────────────────────────────

def test_state_reset(ob):
    s = ob._ObserverState()
    s.running, s.unsub, s.entry, s.providers, s.owns_providers = True, 1, 2, 3, True
    s.classifier_provider = s.reasoning_provider = 4
    s.last_seen["a"] = 1
    s.recent_events.append(1)
    s.classifier_timestamps.append(1)
    s.rate_limit_warn_logged = True
    s.reset()
    assert (s.running, s.unsub, s.entry, s.providers, s.owns_providers,
            s.classifier_provider, s.reasoning_provider, s.rate_limit_warn_logged) == (
        False, None, None, None, False, None, None, False)
    assert not s.last_seen and not s.recent_events and not s.classifier_timestamps
    assert s.recent_events.maxlen == 50


def test_tier_requests_name_both_bindings(ob, monkeypatch):
    made = []
    monkeypatch.setattr(ob, "create_tier_provider", lambda cfg, tier: made.append(tier) or tier)
    reqs = ob._tier_requests({})
    assert set(reqs) == {"observer_classifier", "observer_reasoning"}
    assert reqs["observer_classifier"][1]() == "classifier"
    assert reqs["observer_reasoning"][1]() == "reasoning"


async def test_release_closes_an_owned_manager_or_releases_bindings(ob, monkeypatch):
    log = []

    class Mgr:
        async def async_close(self):
            log.append("close")

        async def async_release(self, name):
            log.append(name)
    monkeypatch.setattr(ob._STATE, "providers", Mgr())
    monkeypatch.setattr(ob._STATE, "owns_providers", True)
    await ob._release_tier_providers()
    assert log == ["close"] and ob._STATE.providers is None
    monkeypatch.setattr(ob._STATE, "providers", Mgr())
    await ob._release_tier_providers()
    assert log == ["close", "observer_classifier", "observer_reasoning"]
    await ob._release_tier_providers()   # nothing held: no error


async def test_start_and_stop(ob, load, fake_hass, monkeypatch):
    class Mgr:
        closed = False

        async def async_acquire_all(self, reqs):
            return {name: f"client:{name}" for name in reqs}

        async def async_release(self, name):
            pass
    pm = load("providers.manager")
    monkeypatch.setattr(pm, "runtime_manager", lambda hass: Mgr())
    calls = []
    for mod in ("appliance_monitor", "proactive_briefing"):
        m = load(mod)

        async def started(*a, _m=mod, **k):
            calls.append(("start", _m))

        async def stopped(*a, _m=mod, **k):
            calls.append(("stop", _m))
        monkeypatch.setattr(m, "start", started)
        monkeypatch.setattr(m, "stop", stopped)
    monkeypatch.setattr(load("cognition"), "mark_unobserved", lambda: calls.append("gap"))
    await ob.start(fake_hass, {"classifier_model": "m"})
    try:
        assert ob.is_running() is True
        assert ob._STATE.classifier_provider == "client:observer_classifier"
        assert ob._STATE.reasoning_provider == "client:observer_reasoning"
        assert calls == [("start", "appliance_monitor"), ("start", "proactive_briefing")]
    finally:
        await ob.stop()
    assert ob.is_running() is False
    assert calls[2:] == [("stop", "appliance_monitor"), ("stop", "proactive_briefing"), "gap"]


async def test_start_gives_up_when_providers_fail(ob, load, fake_hass, monkeypatch):
    class Mgr:
        async def async_acquire_all(self, reqs):
            raise RuntimeError("no key")

        async def async_release(self, name):
            pass
    monkeypatch.setattr(load("providers.manager"), "runtime_manager", lambda hass: Mgr())
    await ob.start(fake_hass, {})
    assert ob.is_running() is False and ob._STATE.providers is None


# ── characterisation (8.66): pinned before the rewrite of the state and stop ─

def test_state_reset_keeps_hass_config_and_buffers(ob):
    s = ob._ObserverState()
    assert (s.hass, s.config, s.running, s.unsub, s.entry, s.providers) == (
        None, {}, False, None, None, None)
    assert s.classifier_timestamps.maxlen == 5000 and s.recent_events.maxlen == 50
    buffers = (s.last_seen, s.recent_events, s.classifier_timestamps)
    s.hass, s.config = "hass", {"k": 1}
    s.recent_events.append(1)
    s.reset()
    assert (s.hass, s.config) == ("hass", {"k": 1})
    assert (s.last_seen, s.recent_events, s.classifier_timestamps) == buffers
    assert all(a is b for a, b in zip(
        (s.last_seen, s.recent_events, s.classifier_timestamps), buffers))


async def test_stop_survives_every_failure_and_logs(ob, load, monkeypatch, caplog):
    import logging
    calls = []

    def bad_unsub():
        calls.append("unsub")
        raise RuntimeError("gone")
    for mod in ("appliance_monitor", "proactive_briefing"):
        async def stopped(*a, _m=mod, **k):
            calls.append(_m)
            raise RuntimeError(_m)
        monkeypatch.setattr(load(mod), "stop", stopped)

    def gap():
        calls.append("gap")
        raise RuntimeError("cognition")
    monkeypatch.setattr(load("cognition"), "mark_unobserved", gap)
    monkeypatch.setattr(ob._STATE, "unsub", bad_unsub)
    monkeypatch.setattr(ob._STATE, "running", True)
    caplog.set_level(logging.INFO, logger=ob.__name__)
    await ob.stop()
    assert calls == ["unsub", "appliance_monitor", "proactive_briefing", "gap"]
    assert ob.is_running() is False and ob._STATE.unsub is None
    assert ("INFO", "Nova Observer stopped") in [
        (r.levelname, r.getMessage()) for r in caplog.records]
