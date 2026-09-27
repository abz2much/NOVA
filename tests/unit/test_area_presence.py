"""Presence-gated learned automation suggestions."""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta


def _context(ap, *, sensor="binary_sensor.office_presence", history=()):
    return ap.AreaPresenceContext(
        sensor_history={sensor: tuple(history)},
        entity_areas={sensor: "office", "light.office": "office"},
        area_sensors={"office": (sensor,)},
        area_names={"office": "Office"},
    )


def test_state_at_uses_last_known_value(load):
    ap = load("automation.area_presence")
    history = ((10.0, False), (20.0, True), (30.0, False))
    assert ap.state_at(history, 9.0) is None
    assert ap.state_at(history, 20.0) is True
    assert ap.state_at(history, 29.0) is True


def test_gate_uses_trigger_times_not_action_times(load):
    ap = load("automation.area_presence")
    history = []
    trigger_times = []
    action_times = []
    for index in range(6):
        base = 1000.0 + index * 1000
        history.extend(((base - 10, False), (base + 30, True), (base + 90, False)))
        trigger_times.append(base)
        action_times.append(base + 60)
    context = _context(ap, history=history)

    assert ap.presence_gate_condition(
        "light.office", trigger_times, context) is None
    assert ap.presence_gate_condition(
        "light.office", action_times, context) is not None


def test_gate_requires_eighty_percent_and_real_off_state(load):
    ap = load("automation.area_presence")
    times = [100.0 + index * 100 for index in range(10)]
    history = [(0.0, False)]
    for index, when in enumerate(times):
        history.append((when - 1, index < 8))
    context = _context(ap, history=history)
    assert ap.presence_gate_condition("light.office", times, context)

    always_on = _context(ap, history=((0.0, True), (2000.0, True)))
    assert ap.presence_gate_condition("light.office", times, always_on) is None


def test_gate_excludes_trigger_entity(load):
    ap = load("automation.area_presence")
    sensor = "binary_sensor.office_presence"
    context = _context(
        ap, sensor=sensor,
        history=((0.0, False), (10.0, True), (1000.0, False)))
    assert ap.presence_gate_condition(
        "light.office", [20, 30, 40, 50, 60], context,
        exclude=(sensor,)) is None


def test_release_requires_gate_and_uses_same_sensor(load):
    ap = load("automation.area_presence")
    history = []
    off_times = []
    for index in range(5):
        on = 1000.0 + index * 2000
        history.extend(((on, True), (on + 300, False)))
        off_times.append(on + 345)
    context = _context(ap, history=history)
    gate = {"entity_id": "binary_sensor.office_presence", "area_id": "office"}

    assert ap.presence_release("light.office", off_times, context, None) is None
    release = ap.presence_release("light.office", off_times, context, gate)
    assert release == {
        "entity_id": "binary_sensor.office_presence",
        "area_id": "office",
        "area_name": "Office",
        "settle_seconds": 60,
    }


def test_release_rejects_weak_correlation_and_caps_duration(load):
    ap = load("automation.area_presence")
    history = []
    for index in range(5):
        on = 1000.0 + index * 3000
        history.extend(((on, True), (on + 100, False)))
    context = _context(ap, history=history)
    gate = {"entity_id": "binary_sensor.office_presence", "area_id": "office"}
    assert ap.presence_release(
        "light.office", [100, 200, 300, 400, 1100], context, gate) is None

    off_times = [1900 + index * 3000 for index in range(5)]
    release = ap.presence_release("light.office", off_times, context, gate)
    assert release and release["settle_seconds"] == 600


def test_numeric_gate_uses_threshold_crossings(load):
    ap = load("automation.area_presence")
    history = []
    actions = []
    for index in range(5):
        base = index * 1000.0
        history.extend(((base, 80.0), (base + 10, 20.0)))
        actions.append(base + 40)
    assert ap.numeric_trigger_times(history, "below", 40.0, actions) == [
        10.0, 1010.0, 2010.0, 3010.0, 4010.0]


def _sequence_conn():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE state_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, entity_id TEXT, domain TEXT, old_state TEXT,
            new_state TEXT, area_id TEXT, hour INTEGER, day_of_week INTEGER,
            triggered_by TEXT DEFAULT 'system', person TEXT DEFAULT 'unknown'
        );
    """)
    connection.row_factory = sqlite3.Row
    return connection


def _add(connection, entity_id, state, when, source="user"):
    connection.execute(
        "INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
        "new_state, area_id, hour, day_of_week, triggered_by) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (when.isoformat(), entity_id, entity_id.split(".", 1)[0], "off", state,
         "", when.hour, when.weekday(), source),
    )


def test_sequence_finder_samples_trigger_time_and_filters_release_sources(load):
    patterns = load("automation.patterns")
    ap = load("automation.area_presence")
    connection = _sequence_conn()
    history = []
    base = datetime.now() - timedelta(days=10)
    for index in range(6):
        trigger = base + timedelta(days=index)
        _add(connection, "binary_sensor.front_door", "on", trigger)
        _add(connection, "light.office", "on", trigger + timedelta(seconds=60))
        _add(connection, "light.office", "off", trigger + timedelta(seconds=150))
        _add(connection, "light.office", "off", trigger + timedelta(seconds=300),
             source="automation")
        history.extend(((trigger.timestamp() - 30, True),
                        (trigger.timestamp() + 120, False)))
    connection.commit()
    context = _context(ap, history=history)

    found = patterns.PatternAnalyzer()._find_sequence_patterns(
        connection, area_presence=context)
    match = next(pattern for pattern in found
                 if pattern.details.get("trigger", {}).get("entity")
                 == "binary_sensor.front_door"
                 and pattern.details.get("action", {}).get("entity") == "light.office"
                 and pattern.details.get("action", {}).get("state") == "on")

    assert match.details["presence_gate"]["area_name"] == "Office"
    assert match.details["presence_release"]["settle_seconds"] == 30


def test_sequence_finder_does_not_gate_from_action_time(load):
    patterns = load("automation.patterns")
    ap = load("automation.area_presence")
    connection = _sequence_conn()
    history = []
    base = datetime.now() - timedelta(days=10)
    for index in range(6):
        trigger = base + timedelta(days=index)
        _add(connection, "binary_sensor.front_door", "on", trigger)
        _add(connection, "light.office", "on", trigger + timedelta(seconds=60))
        history.extend(((trigger.timestamp() - 10, False),
                        (trigger.timestamp() + 30, True),
                        (trigger.timestamp() + 90, False)))
    connection.commit()
    context = _context(ap, history=history)

    found = patterns.PatternAnalyzer()._find_sequence_patterns(
        connection, area_presence=context)
    match = next(pattern for pattern in found
                 if pattern.details.get("trigger", {}).get("entity")
                 == "binary_sensor.front_door"
                 and pattern.details.get("action", {}).get("entity") == "light.office")
    assert "presence_gate" not in match.details


def _sequence(models, *, gate=True, release=True):
    details = {
        "trigger": {"entity": "binary_sensor.front_door", "state": "on"},
        "action": {"entity": "light.office", "state": "on"},
        "delay_seconds": 60,
    }
    if gate:
        details["condition"] = [{
            "condition": "state", "entity_id": "binary_sensor.office_presence",
            "state": "on",
        }]
        details["presence_gate"] = {
            "entity_id": "binary_sensor.office_presence",
            "area_id": "office", "area_name": "Office",
        }
    if release:
        details["presence_release"] = {
            "entity_id": "binary_sensor.office_presence",
            "area_id": "office", "area_name": "Office", "settle_seconds": 60,
        }
    return models.DetectedPattern(
        "sequence", "door then light", ["binary_sensor.front_door", "light.office"],
        0.9, 8, details=details)


def test_sequence_generation_adds_gate_and_safe_release(load):
    suggestions = load("automation.suggestions")
    models = load("automation.models")
    auto = json.loads(suggestions.generate_automation(_sequence(models)))

    assert auto["mode"] == "restart"
    assert auto["condition"] == [{
        "condition": "state", "entity_id": "binary_sensor.office_presence",
        "state": "on",
    }]
    assert auto["alias"].endswith("off when presence clears")
    assert auto["action"][1]["condition"] == "state"  # post-delay recheck
    choice = auto["action"][3]
    wait = choice["default"][0]["wait_for_trigger"][0]
    assert wait == {
        "trigger": "state", "entity_id": "binary_sensor.office_presence",
        "from": "on", "to": "off", "for": "00:01:00",
    }
    assert choice["choose"][0]["sequence"][-1]["for"] == "00:01:00"
    assert auto["action"][-1]["service"] == "light.turn_off"


def test_release_is_not_generated_without_gate(load):
    suggestions = load("automation.suggestions")
    models = load("automation.models")
    auto = json.loads(suggestions.generate_automation(
        _sequence(models, gate=False, release=True)))
    assert "mode" not in auto
    assert len(auto["action"]) == 2


def test_numeric_generation_emits_gate(load):
    suggestions = load("automation.suggestions")
    models = load("automation.models")
    pattern = models.DetectedPattern(
        "numeric_trigger", "dark then light", ["sensor.lux", "light.office"],
        0.9, 8, details={
            "trigger_sensor": "sensor.lux", "op": "below", "threshold": 40.0,
            "action": {"entity": "light.office", "state": "on"},
            "condition": [{"condition": "state",
                           "entity_id": "binary_sensor.office_presence",
                           "state": "on"}],
            "presence_gate": {"entity_id": "binary_sensor.office_presence",
                              "area_id": "office", "area_name": "Office"},
        })
    auto = json.loads(suggestions.generate_automation(pattern))
    assert auto["condition"][0]["entity_id"] == "binary_sensor.office_presence"
    assert auto["alias"].endswith("when presence is detected")


def test_no_presence_data_keeps_sequence_json_byte_identical(load):
    suggestions = load("automation.suggestions")
    models = load("automation.models")
    pattern = _sequence(models, gate=False, release=False)
    expected = json.dumps({
        "alias": "Nova Learned: light.office after binary_sensor.front_door",
        "trigger": {"platform": "state", "entity_id": "binary_sensor.front_door",
                    "to": "on"},
        "action": [
            {"delay": "00:01:00"},
            {"service": "light.turn_on", "entity_id": "light.office"},
        ],
    }, indent=2)
    assert suggestions.generate_automation(pattern) == expected


def test_release_has_distinct_stable_identity(load):
    suggestions = load("automation.suggestions")
    models = load("automation.models")
    base = _sequence(models, gate=True, release=False)
    release = _sequence(models, gate=True, release=True)
    base_key = suggestions.suggestion_identity(
        base.pattern_type, base.entity_ids, base.details)
    release_key = suggestions.suggestion_identity(
        release.pattern_type, release.entity_ids, release.details)
    assert base_key != release_key
    changed = dict(release.details)
    changed["presence_release"] = dict(
        changed["presence_release"], settle_seconds=300, area_name="Study")
    assert suggestions.suggestion_identity(
        release.pattern_type, release.entity_ids, changed) == release_key


async def test_collector_failure_returns_empty_context(load, fake_hass):
    patterns = load("automation.patterns")
    analyzer = patterns.PatternAnalyzer()
    assert await analyzer._fetch_area_presence_context(fake_hass) == patterns.EMPTY_CONTEXT


async def test_collector_is_bounded_and_ignores_motion(
        load, fake_hass, monkeypatch):
    patterns = load("automation.patterns")
    audio = load("audio_routing")
    for index in range(45):
        fake_hass.states.set(
            f"binary_sensor.presence_{index}", "on", device_class="presence")
    fake_hass.states.set("binary_sensor.motion", "on", device_class="motion")
    fake_hass.states.set("light.office", "on")
    monkeypatch.setattr(audio, "entity_area", lambda hass, entity_id: "office")

    captured = {}

    def _history(hass, start, end, entity_ids, **kwargs):
        captured["ids"] = list(entity_ids)
        now = datetime.now().astimezone()
        return {
            entity_id: [
                types.SimpleNamespace(state="off", last_changed=now - timedelta(hours=1)),
                types.SimpleNamespace(state="on", last_changed=now),
            ]
            for entity_id in entity_ids
        }

    class _RecorderInstance:
        async def async_add_executor_job(self, func):
            return func()

    recorder = types.ModuleType("homeassistant.components.recorder")
    recorder.get_instance = lambda hass: _RecorderInstance()
    recorder.history = types.SimpleNamespace(get_significant_states=_history)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder)

    area_registry = sys.modules["homeassistant.helpers.area_registry"]
    monkeypatch.setattr(
        area_registry, "async_get",
        lambda hass: types.SimpleNamespace(
            async_get_area=lambda area_id: types.SimpleNamespace(name="Office")))

    context = await patterns.PatternAnalyzer()._fetch_area_presence_context(fake_hass)

    assert len(captured["ids"]) == 40
    assert "binary_sensor.motion" not in captured["ids"]
    assert context.entity_areas["light.office"] == "office"
    assert context.area_names["office"] == "Office"
