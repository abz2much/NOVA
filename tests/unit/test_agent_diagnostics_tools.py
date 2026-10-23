"""The four diagnostic tools the agent can call: cognitive_status,
connectivity_status, root_cause and system_diagnostics.

Each one asks another part of Nova (stubbed here) and turns the answer into
the text the model reads. The error replies are pinned too.
"""
import json
import sys
import types

import pytest

from fakes import FakeHass

SAFE = "RuntimeError (details are in the Home Assistant log)"


@pytest.fixture
def diag(load):
    return load("agent_runtime.capabilities.diagnostics")


def _stub(monkeypatch, name, **attrs):
    mod = types.ModuleType(f"jc.{name}")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"jc.{name}", mod)
    return mod


# ── cognitive_status ─────────────────────────────────────────────────────────

class _Analyzer:
    def get_stats(self):
        return {"patterns": 4, "suggestions": 1}


async def test_cognitive_status_adds_the_pattern_stats(diag, monkeypatch):
    _stub(monkeypatch, "cognitive_core", status=lambda: {"running": True, "tick": 9})
    _stub(monkeypatch, "automation.patterns", get_analyzer=lambda: _Analyzer())
    out = await diag._exec_cognitive_status(FakeHass(), {})
    assert json.loads(out) == {"running": True, "tick": 9,
                               "pattern_analysis": {"patterns": 4, "suggestions": 1}}
    assert list(json.loads(out)) == ["running", "tick", "pattern_analysis"]


async def test_the_pattern_stats_are_read_in_the_executor(diag, monkeypatch):
    _stub(monkeypatch, "cognitive_core", status=lambda: {})
    analyzer = _Analyzer()
    _stub(monkeypatch, "automation.patterns", get_analyzer=lambda: analyzer)
    hass, jobs = FakeHass(), []

    async def run(func, *args):
        jobs.append((func, args))
        return {"via": "executor"}
    hass.async_add_executor_job = run
    out = await diag._exec_cognitive_status(hass, {})
    assert jobs == [(analyzer.get_stats, ())]
    assert json.loads(out) == {"pattern_analysis": {"via": "executor"}}


async def test_cognitive_status_failure_is_a_safe_error(diag, monkeypatch):
    def boom():
        raise RuntimeError("secret detail")
    _stub(monkeypatch, "cognitive_core", status=boom)
    out = await diag._exec_cognitive_status(FakeHass(), {})
    assert json.loads(out) == {"error": SAFE}
    assert "secret detail" not in out


# ── connectivity_status ──────────────────────────────────────────────────────

async def test_connectivity_status_is_the_connectivity_report(diag, monkeypatch):
    _stub(monkeypatch, "connectivity", status=lambda: {"state": "closed", "failures": 0})
    out = await diag._exec_connectivity_status(FakeHass(), {"ignored": 1})
    assert json.loads(out) == {"state": "closed", "failures": 0}


async def test_connectivity_status_failure_is_a_safe_error(diag, monkeypatch):
    def boom():
        raise RuntimeError("secret detail")
    _stub(monkeypatch, "connectivity", status=boom)
    assert json.loads(await diag._exec_connectivity_status(FakeHass(), {})) == {
        "error": SAFE}


# ── system_diagnostics ───────────────────────────────────────────────────────

async def test_system_diagnostics_is_the_service_health_run(diag, monkeypatch):
    seen = []

    async def run_service_health(hass):
        seen.append(hass)
        return {"llm": "ok", "tts": "slow"}

    _stub(monkeypatch, "diagnostics", run_service_health=run_service_health)
    hass = FakeHass()
    out = await diag._exec_system_diagnostics(hass, {})
    assert json.loads(out) == {"llm": "ok", "tts": "slow"}
    assert seen == [hass]


async def test_system_diagnostics_failure_is_a_safe_error(diag, monkeypatch):
    async def boom(hass):
        raise RuntimeError("secret detail")
    _stub(monkeypatch, "diagnostics", run_service_health=boom)
    assert json.loads(await diag._exec_system_diagnostics(FakeHass(), {})) == {
        "error": SAFE}


# ── root_cause ───────────────────────────────────────────────────────────────

class _Rca:
    DEFAULT_WINDOW_SECS = 1234

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else {}

    def entity_names(self, hass):
        return {"light.lamp": "Lamp"}

    def analyze(self, entity_id, event_time, window, names=None):
        self.calls.append((entity_id, event_time, window, names))
        return self.result


@pytest.fixture
def rca(monkeypatch, load):
    stub = _Rca()
    monkeypatch.setitem(sys.modules, "jc.rca", stub)
    presentation = load("agent_runtime.presentation")
    monkeypatch.setattr(presentation, "display_name",
                        lambda hass, entity_id: {"light.lamp": "Lamp"}.get(entity_id, entity_id))
    return stub


@pytest.mark.parametrize("args", [{}, {"entity_id": ""}, {"entity_id": "   "},
                                  {"entity_id": None}])
async def test_root_cause_needs_an_entity(diag, rca, args):
    out = await diag._exec_root_cause(FakeHass(), args)
    assert out == ("root_cause needs an entity_id "
                   "(resolve names with search_entities first).")
    assert rca.calls == []


async def test_root_cause_asks_for_the_window_in_seconds(diag, rca):
    await diag._exec_root_cause(FakeHass(), {"entity_id": " light.lamp "})
    await diag._exec_root_cause(FakeHass(), {"entity_id": "light.lamp",
                                             "event_time": " 2026-03-01T10:00:00 ",
                                             "window_minutes": "10"})
    await diag._exec_root_cause(FakeHass(), {"entity_id": "light.lamp",
                                             "window_minutes": 2.5})
    assert rca.calls == [
        ("light.lamp", None, 1800, {"light.lamp": "Lamp"}),
        ("light.lamp", "2026-03-01T10:00:00", 600, {"light.lamp": "Lamp"}),
        ("light.lamp", None, 150, {"light.lamp": "Lamp"}),
    ]


@pytest.mark.parametrize("minutes, window", [
    (0, 1800), (None, 1800), ("", 1800), ("abc", 1234), ([1], 1234), (-5, -300),
    ("1e1", 600)])
async def test_the_window_falls_back_when_it_is_not_a_number(
        diag, rca, minutes, window):
    await diag._exec_root_cause(
        FakeHass(), {"entity_id": "light.lamp", "window_minutes": minutes})
    assert rca.calls[0][2] == window


async def test_an_empty_result_gives_just_the_header_and_an_empty_verdict(diag, rca):
    out = await diag._exec_root_cause(FakeHass(), {"entity_id": "light.lamp"})
    assert out == "Root cause analysis for Lamp (light.lamp):\nVerdict: "


async def test_an_entity_with_no_friendly_name_is_not_repeated(diag, rca):
    out = await diag._exec_root_cause(FakeHass(), {"entity_id": "sensor.door"})
    assert out == "Root cause analysis for sensor.door:\nVerdict: "


async def test_the_full_report(diag, rca):
    rca.result = {
        "event": {"timestamp": "2026-03-01T10:00:00", "old_state": "on",
                  "new_state": "off", "area_id": "hall"},
        "summary": "The breaker tripped.",
        "candidates": [
            {"confidence": 0.876, "cause": "Power cut", "evidence": "all lights off"},
            {"confidence": 0.1, "cause": "Bulb", "evidence": "one bulb"},
        ],
        "timeline": [{"t": f"10:{n:02d}", "src": "state", "text": f"event {n}"}
                     for n in range(15)],
    }
    out = await diag._exec_root_cause(FakeHass(), {"entity_id": "light.lamp"})
    lines = out.split("\n")
    assert lines[:8] == [
        "Root cause analysis for Lamp (light.lamp):",
        "Event: on → off at 2026-03-01T10:00:00 (area hall)",
        "Verdict: The breaker tripped.",
        "Ranked causes:",
        "  1. [87%] Power cut — all lights off",
        "  2. [10%] Bulb — one bulb",
        "Timeline (most recent last):",
        "  10:03 [state] event 3",
    ]
    assert lines[8:] == [f"  10:{n:02d} [state] event {n}" for n in range(4, 15)]
    assert len(lines) == 19


async def test_an_event_without_an_area_has_no_area_note(diag, rca):
    rca.result = {"event": {"timestamp": "T", "old_state": None, "new_state": "on"}}
    out = await diag._exec_root_cause(FakeHass(), {"entity_id": "light.lamp"})
    assert out.split("\n")[1] == "Event: None → on at T"


async def test_an_event_without_a_timestamp_is_left_out(diag, rca):
    rca.result = {"event": {"old_state": "on", "new_state": "off"}, "summary": "s"}
    out = await diag._exec_root_cause(FakeHass(), {"entity_id": "light.lamp"})
    assert out == "Root cause analysis for Lamp (light.lamp):\nVerdict: s"


async def test_a_missing_event_candidates_and_timeline_are_all_optional(diag, rca):
    rca.result = {"event": None, "candidates": None, "timeline": None, "summary": None}
    out = await diag._exec_root_cause(FakeHass(), {"entity_id": "light.lamp"})
    assert out == "Root cause analysis for Lamp (light.lamp):\nVerdict: None"


async def test_the_analysis_runs_in_the_executor(diag, rca):
    hass, jobs = FakeHass(), []

    async def run(func, *args):
        jobs.append(args)
        return func(*args)
    hass.async_add_executor_job = run
    await diag._exec_root_cause(hass, {"entity_id": "light.lamp"})
    assert jobs == [()] and len(rca.calls) == 1


async def test_a_failing_analysis_is_not_caught_here(diag, rca):
    def boom(*a, **k):
        raise ValueError("no history")
    rca.analyze = boom
    with pytest.raises(ValueError, match="no history"):
        await diag._exec_root_cause(FakeHass(), {"entity_id": "light.lamp"})


def test_the_agent_logger_keeps_the_name_it_always_had(diag):
    assert diag._LOGGER.name == "jc.agent"
