"""Characterization of Nova's proactive decision rules, pinned before Phase 8
moved them into the cognitive package.

These are not a design. They record, over a broad synthetic grid, exactly
what the local templates (reasoning_loop._try_local_reasoning) and the Local
Mind core (local_mind.assess_core) decided at v7.119.0: speak or silent,
urgency, reason and the words spoken (persona variety off, so phrasing is
deterministic). The structural move must reproduce every case.

An intentional behaviour change updates the fixture in the same change:
    NOVA_WRITE_COGNITIVE_GOLDEN=1 python -m pytest tests/unit/test_cognitive_characterization.py
and the diff it produces is reviewed like code.

Focused run:
    python -m pytest tests/unit/test_cognitive_characterization.py -q
"""
import itertools
import json
import os
import pathlib

import pytest

GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "cognitive"
_WRITE = os.environ.get("NOVA_WRITE_COGNITIVE_GOLDEN") == "1"

# Event archetypes: (key, entity_id, friendly_name, device_class, from, to)
EVENTS = [
    ("smoke_on", "binary_sensor.hall_detector", "Hall Detector", "smoke", "off", "on"),
    ("smoke_clear", "binary_sensor.hall_detector", "Hall Detector", "smoke", "on", "off"),
    ("smoke_unavail", "binary_sensor.hall_detector", "Hall Detector", "smoke", "off", "unavailable"),
    ("leak_named", "binary_sensor.sink_leak", "Sink Leak", "", "off", "on"),
    ("leak_named_clear", "binary_sensor.sink_leak", "Sink Leak", "", "on", "off"),
    ("moisture_on", "binary_sensor.utility_water", "Utility Water", "moisture", "off", "on"),
    ("door_open", "binary_sensor.back_door", "Back Door", "door", "off", "on"),
    ("door_close", "binary_sensor.back_door", "Back Door", "door", "on", "off"),
    ("window_open", "binary_sensor.cellar_window", "Cellar Window", "window", "off", "on"),
    ("lock_unlocked", "lock.front_door", "Front Door", "", "locked", "unlocked"),
    ("lock_locked", "lock.front_door", "Front Door", "", "unlocked", "locked"),
    ("alarm_triggered", "alarm_control_panel.home", "Home Alarm", "", "armed_away", "triggered"),
    ("motion_on", "binary_sensor.hall_motion", "Hall Motion", "motion", "off", "on"),
    ("person_arrive", "person.alex", "Alex", "", "not_home", "home"),
    ("person_leave", "person.alex", "Alex", "", "home", "not_home"),
    ("person_zone", "person.alex", "Alex", "", "not_home", "School"),
    ("garage_open", "cover.garage_door", "Garage Door", "garage_door", "closed", "open"),
    ("garage_close", "cover.garage_door", "Garage Door", "garage_door", "open", "closed"),
    ("battery_low", "sensor.remote_battery", "Remote Battery", "battery", "20", "5"),
    ("washer_idle", "sensor.washer_status", "Washer Status", "", "running", "idle"),
    ("temp_hot", "sensor.lounge_temperature", "Lounge Temperature", "temperature", "80", "95"),
    ("temp_cold", "sensor.lounge_temperature", "Lounge Temperature", "temperature", "60", "50"),
    ("temp_ok", "sensor.lounge_temperature", "Lounge Temperature", "temperature", "68", "70"),
    ("power_spike", "sensor.house_power", "House Power", "power", "400", "3400"),
    ("humidity", "sensor.basement_humidity", "Basement Humidity", "humidity", "50", "71"),
    ("switch_on", "switch.shed_heater", "Shed Heater", "", "off", "on"),
]
CATEGORIES = ["security", "doors_windows", "presence", "climate", "energy", "other", "general"]
URGENCIES = ["low", "medium", "high", "critical"]
RECENT = {
    "none": [],
    "same": ["Back Door (binary_sensor.back_door) changed from off to on"],
    "other": ["Sir, the washer cycle appears to be complete."],
}


def _summary(eid, fname, frm, to):
    return f"{fname} ({eid}) changed from {frm} to {to}"


def local_cases():
    for ev, cat, urg, home in itertools.product(EVENTS, CATEGORIES, URGENCIES, (True, False)):
        yield f"{ev[0]}|{cat}|{urg}|home={int(home)}|recent=none", ev, cat, urg, home, "none"
    # Recent-announcement dedup interacts with every rule that follows it.
    for ev, urg, rec in itertools.product(EVENTS, URGENCIES, ("same", "other")):
        yield f"{ev[0]}|doors_windows|{urg}|home=0|recent={rec}", ev, "doors_windows", urg, False, rec


def _deterministic(load, monkeypatch):
    persona = load("persona")
    monkeypatch.setattr(persona, "_VARIETY", False)
    monkeypatch.setattr(persona, "_recent", {})
    return persona


def run_local(rl, case):
    _key, (_, eid, fname, dc, frm, to), cat, urg, home, rec = case
    out = rl._try_local_reasoning(
        _summary(eid, fname, frm, to), urg, cat, "sir", list(RECENT[rec]), home,
        from_state=frm, to_state=to, entity_id=eid, friendly_name=fname,
        device_class=dc)
    if out is None:
        return None
    return {k: out[k] for k in sorted(out)}


GRADES = ["unknown", "novel", "unusual_hour", "occasional", "common", "routine"]
PRIORS = [(0, 0), (2, 0), (0, 2), (1, 1)]
LM_EVENTS = [
    ("window", "binary_sensor.cellar_window", "Cellar Window", "window", "on"),
    ("lock", "lock.front_door", "Front Door", "", "unlocked"),
    ("humidity", "sensor.basement_humidity", "Basement Humidity", "humidity", "71"),
    ("smoke", "binary_sensor.kitchen_smoke", "Kitchen Smoke", "smoke", "on"),
    ("battery", "sensor.remote_battery", "Remote Battery", "battery", "5"),
]


def lm_cases():
    for ev, urg, grade, prior, home in itertools.product(
            LM_EVENTS, URGENCIES, GRADES, PRIORS, (True, False)):
        yield (f"{ev[0]}|{urg}|{grade}|prior={prior[0]}-{prior[1]}|home={int(home)}",
               ev, urg, grade, prior, home, [])
    for ev, urg in itertools.product(LM_EVENTS, URGENCIES):
        yield (f"{ev[0]}|{urg}|dup", ev, urg, "unknown", (0, 0), False,
               [f"Sir, {ev[2]} is open."])


def run_lm(lm, case):
    _key, (_, eid, fname, dc, to), urg, grade, prior, home, recent = case
    lm._recent_events.clear()
    out = lm.assess_core(
        honorific="sir", entity_id=eid, domain=eid.split(".")[0], device_class=dc,
        category="general", from_state="off", to_state=to, friendly_name=fname,
        urgency=urg, anyone_home=home, recent_announcements=list(recent), hour=14,
        history={"grade": grade, "total": 3, "at_hour": 1, "days": 12.0}, prior=prior)
    return {k: out[k] for k in sorted(out)}


def _golden(name, current):
    path = GOLDEN / f"{name}.json"
    if _WRITE:
        path.parent.mkdir(parents=True, exist_ok=True)
        # One case per line, so a reviewed change shows as a readable diff.
        lines = [f"{json.dumps(k)}: {json.dumps(current[k], sort_keys=True, ensure_ascii=False)}"
                 for k in sorted(current)]
        path.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))


def test_local_templates_match_the_v7_119_characterization(load, monkeypatch):
    _deterministic(load, monkeypatch)
    rl = load("reasoning_loop")
    current = {c[0]: run_local(rl, c) for c in local_cases()}
    pinned = _golden("local_templates", current)
    assert sorted(pinned) == sorted(current)
    drift = {k: {"pinned": pinned[k], "now": current[k]}
             for k in sorted(current) if pinned[k] != current[k]}
    assert drift == {}, json.dumps(dict(list(drift.items())[:5]), indent=1)


def test_local_mind_core_matches_the_v7_119_characterization(load, monkeypatch):
    _deterministic(load, monkeypatch)
    lm = load("local_mind")
    monkeypatch.setattr(lm, "_recent_events", {})
    monkeypatch.setattr(lm, "_stats", {"decisions": 0, "spoke": 0, "silent": 0})
    current = {c[0]: run_lm(lm, c) for c in lm_cases()}
    pinned = _golden("local_mind_core", current)
    assert sorted(pinned) == sorted(current)
    drift = {k: {"pinned": pinned[k], "now": current[k]}
             for k in sorted(current) if pinned[k] != current[k]}
    assert drift == {}, json.dumps(dict(list(drift.items())[:5]), indent=1)


def test_grid_is_broad(load):
    assert len(list(local_cases())) > 1400
    assert len(list(lm_cases())) > 900


def test_local_mind_flap_detection_still_suppresses_the_third_event(load, monkeypatch):
    """Self-awareness: three events for one entity inside the flap window
    silence the third, whatever else the event says."""
    _deterministic(load, monkeypatch)
    lm = load("local_mind")
    monkeypatch.setattr(lm, "_recent_events", {})
    kw = dict(honorific="sir", entity_id="binary_sensor.cellar_window", domain="binary_sensor",
              device_class="window", category="general", from_state="off", to_state="on",
              friendly_name="Cellar Window", urgency="medium", anyone_home=False,
              recent_announcements=[], hour=14, history={"grade": "unknown"}, prior=(0, 0))
    first, second, third = (lm.assess_core(**kw) for _ in range(3))
    assert first["speak"] and second["speak"]
    assert third["speak"] is False and "flapping" in third["reason"]
    kw["urgency"] = "critical"
    assert lm.assess_core(**kw)["speak"] is True    # critical ignores flapping


@pytest.mark.parametrize("raw,expected", [
    ('{"speak": true, "message": "Sir, hello.", "urgency": "high"}',
     {"speak": True, "message": "Sir, hello.", "urgency": "high"}),
    ('```json\n{"speak": false, "reason": "routine"}\n```', {"speak": False, "reason": "routine"}),
    ('Here you go: {"speak": false} thanks', {"speak": False}),
])
def test_readable_provider_replies_parse_as_before(reasoning_loop, raw, expected):
    assert reasoning_loop._parse_reasoning_json(raw) == expected
