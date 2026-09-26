"""v7.120.1: restart/recovery noise and ordinary openings while occupied.

Live defect: straight after a Home Assistant restart Nova said "The first
floor windows have registered as open ... the sensor was unavailable until
now." The window sensor had only come back from `unavailable` into its
current `on` state. The observer's pre-filter drops such startup
transitions, but the local cognition layer re-admitted them as an
"access-point" anomaly, and the classifier then treated them as a real
opening.

Covered here:
  * an ordinary door, window, opening, garage door or lock recovering from
    unknown/unavailable is never classified, reasoned about, voiced,
    notified or learned from;
  * a real transition still goes through the normal pipeline;
  * a critical hazard or a triggered alarm recovering into its active state
    is still critical;
  * the generic `opening` device class is classified deterministically;
  * an ordinary opening while the household is present stays silent,
    decided from structured evidence without the provider.

Alarm, lock and garage behaviour use fakes only.

Focused run:
    python -m pytest tests/unit/test_recovery_and_occupied_openings.py -q
"""
import asyncio
import sys
import types
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


# ── Pure rules ──────────────────────────────────────────────────────────────

@pytest.fixture
def ev(load):
    return load("cognitive.evaluators")


@pytest.mark.parametrize("eid,dc,old,new", [
    ("binary_sensor.front_door", "door", "unavailable", "on"),
    ("binary_sensor.first_floor_windows", "window", "unknown", "on"),
    ("binary_sensor.loft_hatch", "opening", "unavailable", "on"),
    ("binary_sensor.garage_contact", "garage_door", "unavailable", "on"),
    ("cover.garage_door", "garage", "unavailable", "open"),
    ("lock.front_door", "", "unavailable", "unlocked"),
    ("lock.front_door", "", "unknown", "locked"),
    ("binary_sensor.front_door", "door", "unavailable", "off"),
])
def test_entry_recovery_is_recognised(ev, eid, dc, old, new):
    assert ev.is_entry_state_recovery(eid, dc, old, new)


@pytest.mark.parametrize("eid,dc,old,new", [
    ("binary_sensor.front_door", "door", "off", "on"),        # real opening
    ("binary_sensor.loft_hatch", "opening", "off", "on"),
    ("lock.front_door", "", "locked", "unlocked"),            # real unlock
    ("binary_sensor.front_door", "door", "on", "unavailable"),
    ("binary_sensor.kitchen_smoke", "smoke", "unavailable", "on"),
    ("binary_sensor.boiler_co", "carbon_monoxide", "unavailable", "on"),
    ("binary_sensor.hob_gas", "gas", "unknown", "on"),
    ("binary_sensor.sink_leak", "moisture", "unavailable", "on"),
    ("alarm_control_panel.house", "", "unavailable", "triggered"),
    ("cover.lounge_blind", "shade", "unavailable", "open"),
])
def test_other_transitions_are_not_entry_recovery(ev, eid, dc, old, new):
    assert not ev.is_entry_state_recovery(eid, dc, old, new)


@pytest.mark.parametrize("people,alarms,occupied,expected", [
    (["home"], [], [], "home"),                                  # person home
    (["home", "not_home"], ["armed_away"], [], "home"),          # person home stays authoritative
    (["unknown"], [], ["kitchen"], "home"),                      # inconclusive + occupancy
    (["unavailable", "unknown"], ["disarmed"], ["lounge"], "home"),
    ([], [], ["kitchen"], "home"),                               # no registered people
    (["not_home"], [], ["kitchen"], "away"),                     # tracked away beats occupancy
    (["work"], [], ["kitchen"], "away"),                         # a zone is away too
    (["not_home", "unknown"], [], ["kitchen"], "away"),
    (["unknown"], ["armed_away"], ["kitchen"], "away"),          # armed away beats occupancy
    (["unknown"], ["armed_vacation"], ["kitchen"], "away"),
    ([], ["armed_away"], ["kitchen"], "away"),
    (["unknown"], ["disarmed"], [], "unknown"),                  # disarmed alone proves nothing
    ([], ["disarmed"], [], "unknown"),
])
def test_household_presence(ev, people, alarms, occupied, expected):
    assert ev.household_presence(people, alarms, occupied) == expected


# ── Classifier ──────────────────────────────────────────────────────────────

@pytest.fixture
def clf(load, monkeypatch):
    c = load("classifier")
    llm = load("llm_provider")
    calls = []

    async def _no_llm(*a, **k):
        calls.append(a)
        raise AssertionError("the provider must not be asked")
    monkeypatch.setattr(llm, "chat_with_activity", _no_llm)
    return c, calls


@pytest.mark.parametrize("eid,dc", [
    ("binary_sensor.front_door", "door"),
    ("binary_sensor.first_floor_windows", "window"),
    ("binary_sensor.loft_hatch", "opening"),
])
def test_real_opening_classifies_deterministically(clf, eid, dc):
    c, calls = clf
    out = asyncio.run(c.classify(None, None, entity_id=eid, old_state="off",
                                 new_state="on", device_class=dc, now_hhmm="14:00"))
    assert out["rule"] == "door_opened" and out["category"] == "doors_windows"
    assert calls == []


def test_opening_closing_is_deterministic(clf):
    c, calls = clf
    out = asyncio.run(c.classify(None, None, entity_id="binary_sensor.loft_hatch",
                                 old_state="on", new_state="off",
                                 device_class="opening", now_hhmm="14:00"))
    assert out == {"worth_considering": False, "rule": "door_closed"}
    assert calls == []


def test_real_unlock_is_still_handled(clf):
    c, _ = clf
    out = asyncio.run(c.classify(None, None, entity_id="lock.front_door",
                                 old_state="locked", new_state="unlocked"))
    assert out["rule"] == "unlocked" and out["worth_considering"]


@pytest.mark.parametrize("eid,dc,old,new", [
    ("binary_sensor.kitchen_smoke", "smoke", "unavailable", "on"),
    ("binary_sensor.boiler_co", "carbon_monoxide", "unavailable", "on"),
    ("binary_sensor.hob_gas", "gas", "unknown", "on"),
    ("binary_sensor.sink_leak", "moisture", "unavailable", "on"),
    ("alarm_control_panel.house", None, "unavailable", "triggered"),
])
def test_safety_recovery_classifies_critical(clf, eid, dc, old, new):
    c, _ = clf
    out = asyncio.run(c.classify(None, None, entity_id=eid, old_state=old,
                                 new_state=new, device_class=dc))
    assert out["urgency"] == "critical"


# ── Observer front door: recovery never enters the pipeline ────────────────

class _Hass:
    def __init__(self):
        self.tasks = []
        self.states = SimpleNamespace(async_all=lambda d=None: [], get=lambda e: None)

    def async_create_task(self, coro, name=None):
        self.tasks.append(coro)
        coro.close()


def _event(eid, dc, old, new, held_s=600.0):
    t0 = datetime(2026, 9, 26, 12, 0, 0)
    mk = lambda s, t: SimpleNamespace(state=s, last_changed=t, attributes={
        "device_class": dc, "friendly_name": "First Floor Windows"})
    return SimpleNamespace(data={"entity_id": eid, "old_state": mk(old, t0),
                                 "new_state": mk(new, t0 + timedelta(seconds=held_s))})


@pytest.fixture
def front(load, monkeypatch):
    """The observer's real state-change handler with the real cognition
    layer (the path that re-admitted the recovered window)."""
    o = load("observer")
    cog = load("cognition")
    core = load("cognitive_core")
    ef = load("entity_filter")
    alarm = load("alarm_source")
    cog.reset()
    hass = _Hass()
    monkeypatch.setattr(o._STATE, "running", True)
    monkeypatch.setattr(o._STATE, "hass", hass)
    monkeypatch.setattr(o, "_cognition_enabled", lambda: True)
    monkeypatch.setattr(o, "_cognition_threshold", lambda: 0.6)
    monkeypatch.setattr(o, "_record_for_context", lambda e: None)
    monkeypatch.setattr(o, "_record_classifier_call", lambda: None)
    monkeypatch.setattr(o, "_group_debounced", lambda e, i: False)
    monkeypatch.setattr(o, "_debounced", lambda e, i=30.0: False)
    monkeypatch.setattr(o, "_classifier_rate_limited", lambda: False)
    monkeypatch.setattr(ef, "is_excluded", lambda h, e, *a: False)
    monkeypatch.setattr(core, "is_ignored", lambda e: False)
    monkeypatch.setattr(alarm, "is_selected", lambda h, e, c=None: True)
    return o, hass, cog


RECOVERIES = [
    ("binary_sensor.front_door", "door", "unavailable", "on"),
    ("binary_sensor.first_floor_windows", "window", "unknown", "on"),
    ("binary_sensor.loft_hatch", "opening", "unavailable", "on"),
    ("binary_sensor.garage_contact", "garage_door", "unavailable", "on"),
    ("cover.garage_door", "garage", "unavailable", "open"),
    ("lock.front_door", None, "unavailable", "unlocked"),
]


@pytest.mark.parametrize("eid,dc,old,new", RECOVERIES)
def test_entry_recovery_is_ignored_by_the_observer(front, eid, dc, old, new):
    o, hass, cog = front
    o._on_state_changed(_event(eid, dc, old, new))
    assert hass.tasks == []
    # Nothing is learned as a transition either.
    assert eid not in cog._MODEL or not cog._MODEL[eid].transitions


@pytest.mark.parametrize("eid,dc,old,new", [
    ("binary_sensor.front_door", "door", "off", "on"),
    ("binary_sensor.landing_casement", "window", "off", "on"),
    ("binary_sensor.loft_hatch", "opening", "off", "on"),
    ("lock.front_door", None, "locked", "unlocked"),
])
def test_real_transitions_still_reach_the_pipeline(front, eid, dc, old, new):
    o, hass, _ = front
    o._on_state_changed(_event(eid, dc, old, new))
    assert len(hass.tasks) == 1


@pytest.mark.parametrize("cognition_on", [True, False])
@pytest.mark.parametrize("eid,dc,old,new", [
    ("binary_sensor.kitchen_smoke", "smoke", "unavailable", "on"),
    ("binary_sensor.boiler_co", "carbon_monoxide", "unavailable", "on"),
    ("binary_sensor.hob_gas", "gas", "unknown", "on"),
    ("binary_sensor.sink_leak", "moisture", "unavailable", "on"),
    ("alarm_control_panel.house", None, "unavailable", "triggered"),
])
def test_safety_recovery_still_reaches_the_pipeline(front, monkeypatch, cognition_on,
                                                    eid, dc, old, new):
    o, hass, _ = front
    monkeypatch.setattr(o, "_cognition_enabled", lambda: cognition_on)
    o._on_state_changed(_event(eid, dc, old, new))
    assert len(hass.tasks) == 1


# ── Full pipeline: suppressed events cause no effect at all ─────────────────

@pytest.fixture
def pipeline(load, monkeypatch):
    """_process_event with every effect recorded: classifier provider,
    reasoning, speech, notifications, service calls and cache writes."""
    o = load("observer")
    alarm = load("alarm_source")
    rc = load("reasoning_cache")
    db = load("database")
    rec = {"classify": [], "decide": [], "speak": [], "notify": [], "cache": [],
           "services": [], "activity": []}
    people, alarms, occupied = [], [], []

    class _States:
        def async_all(self, domain=None):
            if domain == "person":
                return [SimpleNamespace(state=s, attributes={}) for s in people]
            return []

        def get(self, entity_id):
            return None

    async def _call(domain, service, data=None, **k):
        rec["services"].append((domain, service))

    hass = SimpleNamespace(states=_States(),
                           services=SimpleNamespace(async_call=_call))
    real_classify = o.classifier.classify

    async def _classify(*a, **kw):
        rec["classify"].append(kw.get("entity_id"))
        return await real_classify(*a, **kw)

    async def _decide(*a, **kw):
        rec["decide"].append(kw.get("entity_id"))
        return {"speak": True, "message": "The door is open.", "urgency": "medium"}

    async def _speak(message, *, targets):
        rec["speak"].append(message)
        return targets

    async def _notify(message, *, urgency):
        rec["notify"].append(message)

    llm = load("llm_provider")

    async def _no_llm(*a, **k):
        raise AssertionError("the provider must not be asked")

    monkeypatch.setattr(llm, "chat_with_activity", _no_llm)
    monkeypatch.setattr(o.classifier, "classify", _classify)
    monkeypatch.setattr(o.reasoning_loop, "decide", _decide)
    monkeypatch.setattr(o, "_speak", _speak)
    monkeypatch.setattr(o, "_send_notification", _notify)
    monkeypatch.setattr(o, "_is_announcements_enabled", lambda: True)
    monkeypatch.setattr(o, "_get_announcement_speakers", lambda: None)
    monkeypatch.setattr(rc, "remember", lambda *a, **k: rec["cache"].append(a))
    monkeypatch.setattr(db, "save_activity", lambda **k: rec["activity"].append(k))
    monkeypatch.setattr(alarm, "states", lambda h, c=None: [
        SimpleNamespace(state=s) for s in alarms])
    monkeypatch.setattr(o.audio_routing, "entity_area", lambda h, e: None)
    monkeypatch.setattr(o.audio_routing, "currently_occupied_areas", lambda h: list(occupied))
    monkeypatch.setattr(o.audio_routing, "observer_speak_target",
                        lambda *a, **k: (["media_player.hall"], "local"))
    monkeypatch.setattr(o.sleep_detection, "is_sleeping", lambda *a, **k: (False, ""))
    monkeypatch.setattr(o.sleep_detection, "_in_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(o.output_gate, "can_announce", lambda **kw: (True, ""))
    monkeypatch.setattr(o.output_gate, "record_announcement", lambda **kw: None)
    monkeypatch.setattr(o.output_gate, "recent_announcements", lambda n=5: [])
    monkeypatch.setattr(o._STATE, "hass", hass)
    monkeypatch.setattr(o._STATE, "config", {})

    def run(event, *, persons=(), alarm_states=(), areas=()):
        people[:] = persons
        alarms[:] = alarm_states
        occupied[:] = areas
        asyncio.run(o._process_event(event))
        return rec

    return o, run, rec


def _no_effects(rec):
    return (rec["decide"] == [] and rec["speak"] == [] and rec["notify"] == []
            and rec["cache"] == [] and rec["services"] == [])


@pytest.mark.parametrize("eid,dc,old,new", RECOVERIES)
def test_recovery_has_no_effect_end_to_end(front, pipeline, eid, dc, old, new):
    """Drive the state-change handler and run whatever it schedules: a
    recovery schedules nothing, so no classification, provider call,
    speech, notification, service call or cache write can follow."""
    o, hass, _ = front
    _, run, rec = pipeline
    scheduled = []
    hass.async_create_task = lambda coro, name=None: scheduled.append(coro)
    o._on_state_changed(_event(eid, dc, old, new))
    assert scheduled == []
    assert rec["classify"] == [] and _no_effects(rec)


DOOR_OPEN = ("binary_sensor.front_door", "door", "off", "on")
HATCH_OPEN = ("binary_sensor.loft_hatch", "opening", "off", "on")


@pytest.mark.parametrize("opening", [DOOR_OPEN, HATCH_OPEN])
def test_person_home_silences_an_ordinary_opening(pipeline, opening):
    _, run, rec = pipeline
    run(_event(*opening), persons=["home", "not_home"])
    assert _no_effects(rec)
    assert "household present" in rec["activity"][-1]["message"]


def test_inconclusive_person_plus_occupied_area_silences_it(pipeline):
    _, run, rec = pipeline
    run(_event(*DOOR_OPEN), persons=["unknown"], alarm_states=["disarmed"],
        areas=["kitchen"])
    assert _no_effects(rec)


def test_tracked_away_is_not_overridden_by_occupancy(pipeline):
    _, run, rec = pipeline
    run(_event(*DOOR_OPEN), persons=["not_home"], areas=["kitchen"])
    assert rec["decide"] == ["binary_sensor.front_door"]
    assert rec["speak"]


@pytest.mark.parametrize("mode", ["armed_away", "armed_vacation"])
def test_armed_away_is_not_overridden_by_occupancy(pipeline, mode):
    _, run, rec = pipeline
    run(_event(*DOOR_OPEN), persons=["unknown"], alarm_states=[mode], areas=["kitchen"])
    assert rec["decide"] == ["binary_sensor.front_door"]


def test_disarmed_alarm_alone_does_not_claim_someone_is_home(pipeline):
    _, run, rec = pipeline
    run(_event(*DOOR_OPEN), persons=["unknown"], alarm_states=["disarmed"])
    assert rec["decide"] == ["binary_sensor.front_door"]


def test_hazard_is_never_silenced_by_presence(pipeline):
    _, run, rec = pipeline
    run(_event("binary_sensor.kitchen_smoke", "smoke", "unavailable", "on"),
        persons=["home"], areas=["kitchen"])
    assert rec["decide"] == ["binary_sensor.kitchen_smoke"]


def test_triggered_alarm_is_never_silenced_by_presence(pipeline):
    _, run, rec = pipeline
    run(_event("alarm_control_panel.house", None, "unavailable", "triggered"),
        persons=["home"], areas=["kitchen"])
    assert rec["decide"] == ["alarm_control_panel.house"]


# ── Pattern learning and Sentinel ignore recoveries too ─────────────────────

@pytest.fixture
def core_state(load, fake_hass, monkeypatch):
    cc = load("cognitive_core")
    core = cc._CoreState()
    core.hass = fake_hass
    core.running = True
    logged = []
    core.state_logger = SimpleNamespace(log_state_change=lambda *a, **k: logged.append(a))
    monkeypatch.setattr(cc, "_CORE", core)
    cc._PATTERN_LOG_LAST.clear()
    return cc, logged


def _core_event(cc, eid, dc, old, new):
    from homeassistant.core import State
    return cc.Event("state_changed", {
        "entity_id": eid,
        "old_state": State(eid, old, {"device_class": dc} if dc else {}),
        "new_state": State(eid, new, {"device_class": dc} if dc else {}),
    })


@pytest.mark.parametrize("eid,dc,old,new", RECOVERIES)
def test_recovery_is_never_logged_as_a_pattern(core_state, eid, dc, old, new):
    cc, logged = core_state
    cc._on_state_changed(_core_event(cc, eid, dc, old, new))
    assert logged == []


def test_a_real_opening_is_still_logged_as_a_pattern(core_state):
    cc, logged = core_state
    cc._on_state_changed(_core_event(cc, *DOOR_OPEN))
    assert len(logged) == 1


@pytest.fixture
def sentinel_mod(load, monkeypatch):
    evm = types.ModuleType("homeassistant.helpers.event")
    evm.async_track_state_change_event = lambda *a, **k: (lambda: None)
    evm.async_track_time_interval = lambda *a, **k: (lambda: None)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.event", evm)
    return load("sentinel")


def _night_door_sentinel(sentinel_mod, fake_hass):
    rule = {"id": "door_night", "domain": "binary_sensor", "device_class": "door",
            "state": "on", "time_window": ["00:00", "23:59"],
            "message": "{friendly_name} opened."}
    s = sentinel_mod.NovaSentinel(fake_hass, groq_client=None, honorific="sir",
                                  rules=[rule], entry=None)
    s._entity_matches_rule = lambda eid, r: True
    s._in_time_window = lambda w: True
    return s


def test_sentinel_does_not_alert_on_recovery(sentinel_mod, fake_hass):
    s = _night_door_sentinel(sentinel_mod, fake_hass)
    s._handle_state_change(_event("binary_sensor.front_door", "door", "unavailable", "on"))
    assert fake_hass._tasks == []
    # The door is genuinely open now, so duration rules still see it.
    assert "binary_sensor.front_door:door_night" in s._state_start


def test_sentinel_still_alerts_on_a_real_opening(sentinel_mod, fake_hass):
    s = _night_door_sentinel(sentinel_mod, fake_hass)
    s._handle_state_change(_event(*DOOR_OPEN))
    assert len(fake_hass._tasks) == 1
    fake_hass._tasks.pop().close()
