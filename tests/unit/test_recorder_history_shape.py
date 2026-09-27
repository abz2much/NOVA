"""Nova's recorder readers against Home Assistant's real history shape.

With ``minimal_response=True`` and the default (non-compressed) format,
``recorder.history.get_significant_states`` returns a native ``State`` only
for the first entry of each entity. Every later entry is a plain dict whose
``last_changed`` is an ISO 8601 UTC string (``homeassistant/components/
recorder/history/__init__.py``, "Non-compressed state format returns an ISO
formatted string"; the same code is in ``history/modern.py`` in 2024.10 and
2025.4). These tests feed that exact shape to every Nova reader.
"""
from __future__ import annotations

import sqlite3
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest


BASE = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def _ha_rows(values, *, base=BASE, step=timedelta(minutes=37)):
    """First entry a State-like object with a datetime, the rest HA dicts."""
    rows = [types.SimpleNamespace(state=values[0], last_changed=base,
                                  last_updated=base)]
    for index, value in enumerate(values[1:], 1):
        when = base + step * index
        rows.append({"state": value, "last_changed": when.isoformat()})
    return rows


def _expected_epochs(count, *, base=BASE, step=timedelta(minutes=37)):
    return [(base + step * index).timestamp() for index in range(count)]


def _install_recorder(monkeypatch, response):
    class _RecorderInstance:
        async def async_add_executor_job(self, func):
            return func()

    recorder = types.ModuleType("homeassistant.components.recorder")
    recorder.get_instance = lambda hass: _RecorderInstance()
    recorder.history = types.SimpleNamespace(
        get_significant_states=lambda hass, start, end, ids, **kw: response(ids))
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder)


def test_recorder_epoch_accepts_every_recorder_time_shape(load):
    rt = load("automation.recorder_time")
    aware = datetime(2026, 9, 20, 18, 4, 11, 123456, tzinfo=timezone.utc)
    assert rt.recorder_epoch(aware) == aware.timestamp()
    assert rt.recorder_epoch(aware.isoformat()) == aware.timestamp()
    assert rt.recorder_epoch("2026-09-20T18:04:11.123456+00:00") == aware.timestamp()
    assert rt.recorder_epoch(aware.timestamp()) == aware.timestamp()
    assert rt.recorder_epoch(1790000000) == 1790000000.0
    for bad in (None, True, "", "not a time", object(), {"x": 1}):
        assert rt.recorder_epoch(bad) is None


async def test_presence_fetch_keeps_every_recorder_entry(
        load, fake_hass, monkeypatch):
    patterns = load("automation.patterns")
    audio = load("audio_routing")
    fake_hass.states.set(
        "binary_sensor.hall_presence", "on", device_class="occupancy")
    fake_hass.states.set("light.hall", "on")
    monkeypatch.setattr(audio, "entity_area", lambda hass, entity_id: "hall")
    values = ["off" if index % 2 == 0 else "on" for index in range(17)]
    _install_recorder(monkeypatch, lambda ids: {
        entity_id: _ha_rows(values) for entity_id in ids})

    context = await patterns.PatternAnalyzer()._fetch_area_presence_context(fake_hass)

    series = context.sensor_history["binary_sensor.hall_presence"]
    assert len(series) == 17
    assert [epoch for epoch, _on in series] == _expected_epochs(17)
    assert [on for _epoch, on in series] == [value == "on" for value in values]


async def test_numeric_fetch_keeps_every_recorder_entry(
        load, fake_hass, monkeypatch):
    patterns = load("automation.patterns")
    fake_hass.states.set("sensor.hall_temperature", "20",
                         device_class="temperature")
    values = [str(15 + index % 7) for index in range(31)]
    _install_recorder(monkeypatch, lambda ids: {
        entity_id: _ha_rows(values) for entity_id in ids})

    history = await patterns.PatternAnalyzer()._fetch_numeric_sensor_history(fake_hass)

    series = history["sensor.hall_temperature"]
    assert len(series) == 31
    assert [epoch for epoch, _value in series] == _expected_epochs(31)
    assert [value for _epoch, value in series] == [float(v) for v in values]


async def test_history_backfill_imports_every_recorder_entry(
        load, fake_hass, monkeypatch):
    core = load("cognitive_core")
    fake_hass.states.set("light.hall", "on")
    inserted = {}

    class _StateLogger:
        def distinct_logged_entities(self):
            return set()

        def bulk_insert_history(self, rows):
            inserted["rows"] = rows
            return len(rows)

    monkeypatch.setattr(core._CORE, "state_logger", _StateLogger())
    values = ["off" if index % 2 == 0 else "on" for index in range(17)]
    _install_recorder(monkeypatch, lambda ids: {
        entity_id: _ha_rows(values) for entity_id in ids})

    result = await core.backfill_from_history(fake_hass)

    assert result["imported"] == 17
    rows = inserted["rows"]
    # Stored as naive local time, exactly as live logging writes it.
    assert [datetime.fromisoformat(row[0]).timestamp() for row in rows] == (
        _expected_epochs(17))
    assert [row[4] for row in rows] == values


@pytest.fixture
def far_timezone(monkeypatch):
    """Run in a zone far from UTC so any UTC/local mix-up shifts by hours."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is unavailable on this platform")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    assert time.timezone != 0
    yield
    monkeypatch.undo()
    time.tzset()


async def test_presence_gate_lines_up_with_naive_local_pattern_rows(
        load, fake_hass, monkeypatch, far_timezone):
    """patterns.db rows are naive local time; recorder strings are UTC. Both
    must come out as the same epoch or the gate is checked at the wrong time."""
    patterns = load("automation.patterns")
    audio = load("audio_routing")
    fake_hass.states.set(
        "binary_sensor.hall_presence", "on", device_class="occupancy")
    fake_hass.states.set("light.hall", "on")
    monkeypatch.setattr(audio, "entity_area", lambda hass, entity_id: "hall")

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE state_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, entity_id TEXT, domain TEXT, old_state TEXT,
            new_state TEXT, area_id TEXT, hour INTEGER, day_of_week INTEGER,
            triggered_by TEXT DEFAULT 'system', person TEXT DEFAULT 'unknown'
        );
    """)

    def _add(entity_id, state, epoch):
        local = datetime.fromtimestamp(epoch)          # as cognitive_core writes
        connection.execute(
            "INSERT INTO state_changes (timestamp, entity_id, domain, "
            "old_state, new_state, area_id, hour, day_of_week, triggered_by) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (local.isoformat(), entity_id, entity_id.split(".", 1)[0], "off",
             state, "", local.hour, local.weekday(), "user"))

    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=10)
    presence = [types.SimpleNamespace(state="off", last_changed=start)]
    for day in range(6):
        trigger = (start + timedelta(days=day, hours=3)).timestamp()
        _add("binary_sensor.front_door", "on", trigger)
        _add("light.hall", "on", trigger + 60)
        _add("light.hall", "off", trigger + 150)
        # Presence is on only from 30 s before the trigger to 2 min after it,
        # so a UTC/local offset of even one hour would miss every sample.
        for offset, value in ((-30, "on"), (120, "off")):
            when = datetime.fromtimestamp(trigger + offset, timezone.utc)
            presence.append({"state": value, "last_changed": when.isoformat()})
    connection.commit()
    _install_recorder(monkeypatch, lambda ids: {
        entity_id: presence for entity_id in ids})

    context = await patterns.PatternAnalyzer()._fetch_area_presence_context(fake_hass)
    found = patterns.PatternAnalyzer()._find_sequence_patterns(
        connection, area_presence=context)
    match = next(
        pattern for pattern in found
        if pattern.details["trigger"]["entity"] == "binary_sensor.front_door"
        and pattern.details["action"] == {"entity": "light.hall", "state": "on"})

    assert match.details["presence_gate"]["entity_id"] == "binary_sensor.hall_presence"
    assert match.details["presence_release"]["settle_seconds"] == 30
