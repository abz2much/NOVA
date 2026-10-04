"""Characterisation tests for the panel's stat helpers (ws_panel_stats.py).

`_get_person_routines`, `_downsample` and `_get_area_sparklines` had almost
no coverage (2% to 20%). These tests pin what they return, including the
"never raises" fallbacks, so that moving them out of websocket.py could not
change anything.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeHass


@pytest.fixture
def mod(load):
    return load("ws_panel_stats")


# ── _downsample ──────────────────────────────────────────────────────────────

def test_downsample_returns_the_same_list_when_short_enough(mod):
    vals = [1.0, 2.0, 3.0]
    assert mod._downsample(vals, 3) is vals
    assert mod._downsample(vals, 10) is vals


def test_downsample_with_a_non_positive_limit_returns_everything(mod):
    vals = [1.0, 2.0, 3.0]
    assert mod._downsample(vals, 0) is vals
    assert mod._downsample(vals, -4) is vals


def test_downsample_is_evenly_spaced(mod):
    vals = [float(i) for i in range(100)]
    assert mod._downsample(vals, 20) == [float(i) for i in range(0, 100, 5)]
    assert mod._downsample(vals, 7) == [0.0, 14.0, 28.0, 42.0, 57.0, 71.0, 85.0]
    assert mod._downsample([1.0, 2.0, 3.0, 4.0, 5.0], 2) == [1.0, 3.0]
    assert mod._downsample([], 5) == []


# ── _get_person_routines ─────────────────────────────────────────────────────

@pytest.fixture
def analyzer(load, monkeypatch):
    patterns = load("automation.patterns")
    holder = types.SimpleNamespace(rows=[], boom=False)

    def get_person_patterns():
        if holder.boom:
            raise RuntimeError("db unavailable")
        return holder.rows

    monkeypatch.setattr(patterns, "get_analyzer", lambda: types.SimpleNamespace(
        get_person_patterns=get_person_patterns))
    return holder


def test_person_routines_group_rows_by_person(mod, analyzer):
    analyzer.rows = [
        {"person": "Abi", "id": 1, "pattern_type": "time", "description": "up at 7",
         "confidence": 0.8765, "occurrences": 12, "last_seen": "2026-01-01"},
        {"person": "Sam", "id": 2, "pattern_type": "time", "description": "up at 8"},
        {"person": "Abi", "id": 3, "pattern_type": "device", "description": "lamp",
         "confidence": None},
        {"person": "", "id": 4, "description": "nobody"},
        {"id": 5, "description": "no person key"},
    ]
    out = mod._get_person_routines()
    assert list(out) == ["Abi", "Sam"]
    assert out["Abi"][0] == {
        "id": 1, "pattern_type": "time", "description": "up at 7",
        "confidence": 0.88, "occurrences": 12, "last_seen": "2026-01-01"}
    assert out["Abi"][1]["confidence"] == 0.0
    assert out["Sam"][0] == {
        "id": 2, "pattern_type": "time", "description": "up at 8",
        "confidence": 0.0, "occurrences": 0, "last_seen": ""}


def test_person_routines_name_entity_ids_in_descriptions(mod, analyzer, load):
    load("cognitive.naming")
    analyzer.rows = [{"person": "Abi", "id": 1,
                      "description": "turns on light.kitchen at 7"}]
    out = mod._get_person_routines({"light.kitchen": "Kitchen Light"})
    assert out["Abi"][0]["description"] == "turns on Kitchen Light at 7"
    assert mod._get_person_routines()["Abi"][0]["description"] == "turns on light.kitchen at 7"


def test_person_routines_never_raise(mod, analyzer):
    analyzer.boom = True
    assert mod._get_person_routines() == {}
    analyzer.boom = False
    analyzer.rows = [{"person": "Abi", "confidence": "not a number"}]
    assert mod._get_person_routines() == {}


def test_person_routines_empty(mod, analyzer):
    assert mod._get_person_routines() == {}


# ── _get_goals ───────────────────────────────────────────────────────────────

def test_goals_are_shaped_for_the_panel(mod, load, monkeypatch):
    goals = load("goals")
    seen = {}

    def recent(limit):
        seen["limit"] = limit
        return [
            {"id": "g1", "title": "Tidy", "outcome": "done", "status": "closed",
             "steps": [{"status": "done"}, {"status": "open"}, {"status": "done"}],
             "next_check_ts": "t1", "deadline_ts": 5, "last_result": "ok",
             "updated_ts": "t2"},
            {"id": "g2"},
        ]

    monkeypatch.setattr(goals, "recent", recent)
    out = mod._get_goals()
    assert seen["limit"] == 20
    assert out[0] == {
        "id": "g1", "title": "Tidy", "outcome": "done", "status": "closed",
        "steps_done": 2, "steps_total": 3,
        "steps": [{"status": "done"}, {"status": "open"}, {"status": "done"}],
        "next_check_ts": "t1", "deadline_ts": 5, "last_result": "ok", "updated_ts": "t2"}
    assert out[1] == {
        "id": "g2", "title": "", "outcome": "", "status": "active",
        "steps_done": 0, "steps_total": 0, "steps": [],
        "next_check_ts": "", "deadline_ts": None, "last_result": "", "updated_ts": ""}


def test_goals_never_raise(mod, load, monkeypatch):
    def boom(limit):
        raise RuntimeError("store unavailable")
    monkeypatch.setattr(load("goals"), "recent", boom)
    assert mod._get_goals() == []


# ── _get_area_sparklines ─────────────────────────────────────────────────────

class _Fake:
    """A recorder, its history module and the clock, installed in sys.modules."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.raw = {}
        self.fetch_error = None
        self.end = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)
        fake = self

        class _Instance:
            async def async_add_executor_job(self, func):
                return func()

        history = types.SimpleNamespace()

        def get_significant_states(hass, start, end, entity_ids, **kw):
            fake.calls.append({"start": start, "end": end,
                               "entity_ids": entity_ids, **kw})
            if fake.fetch_error:
                raise fake.fetch_error
            return fake.raw

        history.get_significant_states = get_significant_states
        recorder = types.ModuleType("homeassistant.components.recorder")
        recorder.get_instance = lambda hass: _Instance()
        recorder.history = history
        dt = types.ModuleType("homeassistant.util.dt")
        dt.utcnow = lambda: fake.end
        for name in ("homeassistant", "homeassistant.components", "homeassistant.util"):
            if name not in sys.modules:
                monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder)
        monkeypatch.setitem(sys.modules, "homeassistant.util.dt", dt)
        monkeypatch.setattr(sys.modules["homeassistant.util"], "dt", dt, raising=False)


@pytest.fixture
def rec(monkeypatch):
    return _Fake(monkeypatch)


async def test_sparklines_without_any_entity_do_nothing(mod, rec):
    assert await mod._get_area_sparklines(FakeHass(), {}) == {}
    assert await mod._get_area_sparklines(
        FakeHass(), {"a": {"temp": None, "humidity": None}}) == {}
    assert rec.calls == []


async def test_sparklines_without_a_recorder_return_nothing(mod, monkeypatch):
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", None)
    assert await mod._get_area_sparklines(
        FakeHass(), {"a": {"temp": "sensor.t", "humidity": None}}) == {}


async def test_sparklines_ask_the_recorder_for_the_right_window(mod, rec):
    rec.raw = {}
    out = await mod._get_area_sparklines(
        FakeHass(),
        {"a": {"temp": "sensor.t", "humidity": "sensor.h"},
         "b": {"temp": "sensor.t", "humidity": None}},
        hours=6.0)
    assert out == {}                                  # no history: nothing to draw
    (call,) = rec.calls
    assert call["entity_ids"] == ["sensor.h", "sensor.t"]       # sorted, unique
    assert call["end"] == rec.end
    assert call["start"] == rec.end - timedelta(hours=6)
    assert call["minimal_response"] is True and call["no_attributes"] is True


async def test_sparklines_default_window_is_twelve_hours(mod, rec):
    await mod._get_area_sparklines(FakeHass(), {"a": {"temp": "sensor.t"}})
    assert rec.calls[0]["start"] == rec.end - timedelta(hours=12)


async def test_sparklines_read_state_objects_and_dicts_and_skip_bad_values(mod, rec):
    rec.raw = {
        "sensor.t": [types.SimpleNamespace(state="20.5"),
                     {"state": "21", "last_changed": "x"},
                     {"state": "unavailable"},
                     types.SimpleNamespace(state=None),
                     {"state": "22"}],
        "sensor.h": [{"state": "bad"}],
    }
    out = await mod._get_area_sparklines(
        FakeHass(),
        {"a": {"temp": "sensor.t", "humidity": "sensor.h"},
         "b": {"temp": "sensor.missing", "humidity": None}})
    assert out == {"a": {"temp": [20.5, 21.0, 22.0]}}    # no humidity key, no area b


async def test_sparklines_downsample_to_the_requested_points(mod, rec):
    rec.raw = {"sensor.t": [{"state": str(i)} for i in range(100)],
               "sensor.h": [{"state": str(i)} for i in range(10)]}
    out = await mod._get_area_sparklines(
        FakeHass(), {"a": {"temp": "sensor.t", "humidity": "sensor.h"}}, points=5)
    assert out["a"]["temp"] == [0.0, 20.0, 40.0, 60.0, 80.0]
    assert out["a"]["humidity"] == [0.0, 2.0, 4.0, 6.0, 8.0]


async def test_sparklines_never_raise_when_the_recorder_fails(mod, rec, caplog):
    rec.fetch_error = RuntimeError("recorder busy")
    with caplog.at_level("DEBUG"):
        out = await mod._get_area_sparklines(FakeHass(), {"a": {"temp": "sensor.t"}})
    assert out == {}
    assert any("sparkline history fetch failed: recorder busy" in r.getMessage()
               for r in caplog.records)
