"""Fresh behaviour tests for classifier.py (Observer tier 1).

Written from the spec: rules first, a provider call only for events no rule
covers. The provider seam is llm_provider.chat_with_activity, replaced here
through sys.modules so no network or real provider is involved."""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def clf(load):
    return load("classifier")


@pytest.fixture
def llm(monkeypatch):
    """A stand-in llm_provider. `reply` is what chat_with_activity returns
    (or raises, when it is an exception); `calls` records each call."""
    state = types.SimpleNamespace(calls=[], reply={"text": '{"worth_considering": false}'})

    async def chat_with_activity(hass, provider, messages, **kw):
        state.calls.append({"hass": hass, "provider": provider,
                            "messages": messages, "kw": kw})
        if isinstance(state.reply, BaseException):
            raise state.reply
        return state.reply

    fake = types.ModuleType("jc.llm_provider")
    fake.chat_with_activity = chat_with_activity
    monkeypatch.setitem(sys.modules, "jc.llm_provider", fake)
    return state


def _broken_panel_log(monkeypatch):
    """A panel log that raises, so the caller's own work must carry on."""
    def nova_log(*_a, **_k):
        raise RuntimeError("panel log down")
    fake = types.ModuleType("jc.websocket")
    fake.nova_log = nova_log
    monkeypatch.setitem(sys.modules, "jc.websocket", fake)


async def _run(clf, eid, old, new, dc=None, hhmm="12:00", **kw):
    return await clf.classify(None, None, entity_id=eid, old_state=old, new_state=new,
                              device_class=dc, now_hhmm=hhmm, **kw)


# ── Rule 1: noise prefixes come first ──────────────────────────────────────

@pytest.mark.parametrize("eid", [
    "sensor.sun_next_dawn", "sensor.time_utc", "sensor.date_today", "sensor.uptime",
    "sensor.last_boot", "sensor.cpu_temp", "sensor.memory_use", "sensor.disk_free",
    "sensor.network_in", "sensor.processor_load", "sensor.load_5m", "sensor.swap_used",
    "button.restart", "number.volume", "input_number.target", "input_select.scene",
    "input_boolean.guest", "input_text.note", "input_datetime.alarm", "update.core",
    "automation.morning", "script.bedtime", "scene.evening", "zone.home",
    "device_tracker.alex_phone", "persistent_notification.x", "counter.visits",
    "timer.oven",
])
async def test_noise_prefixes_are_dropped(clf, llm, eid):
    out = await _run(clf, eid, "a", "b", dc="smoke")
    assert out == {"worth_considering": False, "rule": "noise_prefix"}
    assert llm.calls == []


async def test_prefix_match_is_case_insensitive_and_beats_everything(clf, llm):
    out = await _run(clf, "Sensor.CPU_temp", "1", "1")
    assert out["rule"] == "noise_prefix"


async def test_prefix_is_a_plain_startswith(clf, llm):
    # "sensor.time" also swallows sensor.timestamp_x / sensor.timer_left
    out = await _run(clf, "sensor.timer_left", "10", "9")
    assert out["rule"] == "noise_prefix"


# ── Rule 2: junk transitions ───────────────────────────────────────────────

@pytest.mark.parametrize("old,new", [
    ("unavailable", "unknown"), ("unknown", "unavailable"),
    ("unavailable", "unavailable"), ("unknown", "unknown"), ("on", "on"),
])
async def test_junk_transitions(clf, llm, old, new):
    out = await _run(clf, "binary_sensor.kitchen_smoke", old, new, dc="smoke")
    assert out == {"worth_considering": False, "rule": "noise_transition"}


async def test_state_comparison_is_case_sensitive(clf, llm):
    out = await _run(clf, "lock.front_door", "unlocked", "Unlocked")
    # not a same-state transition and not exactly "unlocked" → locked rule
    assert out == {"worth_considering": False, "rule": "locked"}


# ── Rule 3: noise device classes ───────────────────────────────────────────

@pytest.mark.parametrize("dc", ["connectivity", "plug", "power", "update", "running",
                                "battery_charging", "POWER"])
async def test_noise_device_classes(clf, llm, dc):
    out = await _run(clf, "binary_sensor.washer_plug", "off", "on", dc=dc)
    assert out == {"worth_considering": False, "rule": "noise_dc"}


async def test_power_device_class_hides_the_power_spike_rule(clf, llm):
    out = await _run(clf, "sensor.house_power", "100", "5000", dc="power")
    assert out["rule"] == "noise_dc"


# ── Rule 4: safety-critical device classes ─────────────────────────────────

@pytest.mark.parametrize("dc", ["smoke", "gas", "moisture", "carbon_monoxide", "Smoke"])
@pytest.mark.parametrize("old,new", [("off", "on"), ("on", "off"), ("off", "unavailable")])
async def test_critical_device_classes_always_critical(clf, llm, dc, old, new):
    out = await _run(clf, "binary_sensor.hall_detector", old, new, dc=dc)
    assert out == {"worth_considering": True, "urgency": "critical",
                   "category": "security", "rule": "safety"}


@pytest.mark.parametrize("dc", ["tamper", "safety", "sound", "vibration", "problem"])
async def test_non_critical_classes_on_binary_sensors_go_to_the_provider(clf, llm, dc):
    out = await _run(clf, "binary_sensor.hall_box", "off", "on", dc=dc)
    assert len(llm.calls) == 1
    assert out == {"worth_considering": False}


# ── Rule 5: openings ───────────────────────────────────────────────────────

@pytest.mark.parametrize("dc", ["door", "window", "garage_door", "opening"])
@pytest.mark.parametrize("hhmm,urg", [
    ("22:59", "medium"), ("23:00", "high"), ("02:30", "high"), ("05:59", "high"),
    ("06:00", "medium"), ("14:00", "medium"), ("", "medium"), ("noon", "medium"),
])
async def test_opening_urgency_by_hour(clf, llm, dc, hhmm, urg):
    out = await _run(clf, "binary_sensor.back_door", "off", "on", dc=dc, hhmm=hhmm)
    assert out == {"worth_considering": True, "urgency": urg,
                   "category": "doors_windows", "rule": "door_opened"}


@pytest.mark.parametrize("new", ["off", "open", "unavailable"])
async def test_opening_anything_but_on_is_closed(clf, llm, new):
    out = await _run(clf, "cover.garage_door", "closed", new, dc="garage_door")
    assert out == {"worth_considering": False, "rule": "door_closed"}


# ── Rule 6: motion / occupancy ─────────────────────────────────────────────

@pytest.mark.parametrize("dc", ["motion", "occupancy"])
async def test_motion_clear(clf, llm, dc):
    out = await _run(clf, "binary_sensor.hall_motion", "on", "off", dc=dc, hhmm="02:00")
    assert out == {"worth_considering": False, "rule": "motion_clear"}


@pytest.mark.parametrize("hhmm", ["23:00", "23:59", "00:10", "04:59"])
async def test_motion_at_night(clf, llm, hhmm):
    out = await _run(clf, "binary_sensor.hall_motion", "off", "on", dc="motion", hhmm=hhmm)
    assert out == {"worth_considering": True, "urgency": "low",
                   "category": "presence", "rule": "motion_night"}


@pytest.mark.parametrize("hhmm", ["05:00", "12:00", "22:59", "", "x"])
async def test_motion_by_day(clf, llm, hhmm):
    out = await _run(clf, "binary_sensor.hall_motion", "off", "on", dc="occupancy", hhmm=hhmm)
    assert out == {"worth_considering": False, "rule": "motion_routine"}


# ── Rule 7: person entities ────────────────────────────────────────────────

async def test_person_arrives(clf, llm):
    out = await _run(clf, "person.alex", "not_home", "home")
    assert out == {"worth_considering": True, "urgency": "medium",
                   "category": "presence", "rule": "arrived"}


async def test_person_leaves(clf, llm):
    out = await _run(clf, "person.alex", "home", "Work")
    assert out == {"worth_considering": True, "urgency": "medium",
                   "category": "presence", "rule": "left"}


async def test_person_zone_to_zone(clf, llm):
    out = await _run(clf, "person.alex", "Work", "not_home")
    assert out == {"worth_considering": False, "rule": "person_same"}


# ── Rule 8: locks ──────────────────────────────────────────────────────────

async def test_lock_unlocked(clf, llm):
    out = await _run(clf, "lock.front_door", "locked", "unlocked")
    assert out == {"worth_considering": True, "urgency": "medium",
                   "category": "security", "rule": "unlocked"}


@pytest.mark.parametrize("new", ["locked", "jammed", "open", "unlocking"])
async def test_lock_anything_else(clf, llm, new):
    out = await _run(clf, "lock.front_door", "unlocked", new)
    assert out == {"worth_considering": False, "rule": "locked"}


# ── Rule 9: alarm panels ───────────────────────────────────────────────────

async def test_alarm_triggered(clf, llm):
    out = await _run(clf, "alarm_control_panel.house", "armed_away", "triggered")
    assert out == {"worth_considering": True, "urgency": "critical",
                   "category": "security", "rule": "alarm"}


# "pending" is the entry countdown as someone comes home, not an alarm (8.83.0).
@pytest.mark.parametrize("new", ["disarmed", "armed_home", "arming", "pending"])
async def test_alarm_other_change(clf, llm, new):
    out = await _run(clf, "alarm_control_panel.house", "armed_away", new)
    assert out == {"worth_considering": True, "urgency": "low",
                   "category": "security", "rule": "alarm_change"}


# ── Rule 10: routine domains ───────────────────────────────────────────────

@pytest.mark.parametrize("eid,rule", [
    ("light.kitchen", "light_routine"), ("switch.shed_heater", "switch_routine"),
    ("climate.lounge", "climate_routine"), ("media_player.tv", "media_player_routine"),
])
async def test_routine_domains(clf, llm, eid, rule):
    out = await _run(clf, eid, "off", "on")
    assert out == {"worth_considering": False, "rule": rule}


# ── Rule 11: sensors ───────────────────────────────────────────────────────

@pytest.mark.parametrize("eid", ["sensor.house_power", "sensor.oven_watts"])
async def test_power_spike(clf, llm, eid):
    out = await _run(clf, eid, "400", "1400.5")
    assert out == {"worth_considering": True, "urgency": "low",
                   "category": "energy", "rule": "power_spike"}


async def test_power_drop_counts_too(clf, llm):
    out = await _run(clf, "sensor.house_power", "3000", "100")
    assert out["rule"] == "power_spike"


async def test_power_change_of_exactly_1000_is_drift(clf, llm):
    out = await _run(clf, "sensor.house_power", "400", "1400")
    assert out == {"worth_considering": False, "rule": "sensor_drift"}


@pytest.mark.parametrize("eid,old,new", [
    ("sensor.lounge_temperature", "20", "5000"),     # no power/watt in id
    ("sensor.house_power", "400", "unavailable"),     # not numeric
    ("sensor.washer_status", "running", "idle"),
])
async def test_sensor_drift(clf, llm, eid, old, new):
    out = await _run(clf, eid, old, new)
    assert out == {"worth_considering": False, "rule": "sensor_drift"}


# ── Rule 12: binary sensors without a device class ─────────────────────────

@pytest.mark.parametrize("dc", [None, ""])
async def test_binary_without_class(clf, llm, dc):
    out = await _run(clf, "binary_sensor.mystery", "off", "on", dc=dc)
    assert out == {"worth_considering": False, "rule": "binary_no_dc"}
    assert llm.calls == []


# ── Rule 13: anything else asks the provider ───────────────────────────────

async def test_ambiguous_event_calls_the_provider_once(clf, llm):
    hass, provider = object(), object()
    llm.reply = {"text": '{"worth_considering": true, "urgency": "high", "category": "climate"}'}
    out = await clf.classify(hass, provider, entity_id="cover.awning", old_state="closed",
                             new_state="open", friendly_name="Awning",
                             device_class="awning", now_hhmm="10:15")
    assert out == {"worth_considering": True, "urgency": "high", "category": "climate"}
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["hass"] is hass and call["provider"] is provider
    assert call["kw"]["role"] == "classifier"
    assert call["kw"]["data_category"] == "text"
    assert call["kw"]["temperature"] == 0.0
    assert call["kw"]["max_tokens"] == 80
    msgs = call["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]
    system = msgs[0]["content"]
    assert "JSON" in system
    for cat in ("appliances", "doors_windows", "presence", "energy", "security",
                "climate", "other"):
        assert cat in system
    user = msgs[1]["content"]
    for fact in ("10:15", "cover.awning", "Awning", "awning", "closed", "open"):
        assert fact in user


async def test_a_failing_panel_log_does_not_stop_the_provider_call(clf, llm, monkeypatch):
    _broken_panel_log(monkeypatch)
    llm.reply = {"text": '{"worth_considering": true, "urgency": "low", "category": "climate"}'}
    out = await _run(clf, "cover.awning", "closed", "open", dc="awning")
    assert out == {"worth_considering": True, "urgency": "low", "category": "climate"}
    assert len(llm.calls) == 1


async def test_user_message_line_format(clf, llm):
    await clf.classify(None, None, entity_id="fan.attic", old_state="off", new_state="on",
                       now_hhmm="09:05")
    assert llm.calls[0]["messages"][1]["content"] == (
        "Time:09:05 Entity:fan.attic Name: Class:None off→on")


@pytest.mark.parametrize("text,expected", [
    ('{"worth_considering": true}',
     {"worth_considering": True, "urgency": "low", "category": "other"}),
    ('{"worth_considering": true, "urgency": "extreme", "category": "x"}',
     {"worth_considering": True, "urgency": "low", "category": "x"}),
    ('{"worth_considering": true, "urgency": "critical", "category": "security"}',
     {"worth_considering": True, "urgency": "critical", "category": "security"}),
    ('```json\n{"worth_considering": true, "urgency": "medium", "category": "energy"}\n```',
     {"worth_considering": True, "urgency": "medium", "category": "energy"}),
    ('```\n{"worth_considering": true, "urgency": "medium"}\n```',
     {"worth_considering": True, "urgency": "medium", "category": "other"}),
    ('Sure! {"worth_considering": true, "urgency": "high", "category": "appliances"} hope that helps',
     {"worth_considering": True, "urgency": "high", "category": "appliances"}),
    ('{"worth_considering": false, "urgency": "critical"}', {"worth_considering": False}),
    ('not json at all', {"worth_considering": False}),
    ('', {"worth_considering": False}),
    ('{"worth_considering": true, "detail": {"a": 1}} trailing', {"worth_considering": False}),
    ('[1, 2]', {"worth_considering": False}),
])
async def test_provider_reply_parsing(clf, llm, text, expected):
    llm.reply = {"text": text}
    out = await _run(clf, "cover.awning", "closed", "open")
    assert out == expected


async def test_non_dict_reply_is_stringified(clf, llm):
    llm.reply = '{"worth_considering": true, "urgency": "medium", "category": "other"}'
    out = await _run(clf, "cover.awning", "closed", "open")
    assert out == {"worth_considering": True, "urgency": "medium", "category": "other"}


async def test_dict_reply_without_text(clf, llm):
    llm.reply = {"tool_calls": []}
    assert await _run(clf, "cover.awning", "closed", "open") == {"worth_considering": False}


async def test_provider_failure_is_not_worth_considering(clf, llm):
    llm.reply = RuntimeError("provider down")
    out = await _run(clf, "cover.awning", "closed", "open")
    assert out == {"worth_considering": False}


async def test_provider_results_carry_no_rule_key(clf, llm):
    llm.reply = {"text": '{"worth_considering": true, "urgency": "low", "category": "other"}'}
    out = await _run(clf, "vacuum.downstairs", "docked", "cleaning")
    assert "rule" not in out


async def test_rules_never_reach_the_provider(clf, llm):
    await _run(clf, "binary_sensor.back_door", "off", "on", dc="door")
    await _run(clf, "person.alex", "home", "not_home")
    await _run(clf, "light.kitchen", "off", "on")
    assert llm.calls == []


# ── parse helper (spec'd private name) ─────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("", {"worth_considering": False}),
    (None, {"worth_considering": False}),
    ('  {"a": 1}  ', {"a": 1}),
    ('```json {"a": 2} ```', {"a": 2}),
    ('x {"a": 3} y', {"a": 3}),
    ("nothing", {"worth_considering": False}),
])
def test_parse_json_helper(clf, raw, expected):
    assert clf._parse_json(raw) == expected
