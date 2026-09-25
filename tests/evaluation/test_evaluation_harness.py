"""The evaluation harness's own machinery: fixture validation, verdicts,
safety checks, metrics arithmetic, sandbox isolation and the developer
command. Nova's behaviour is judged in test_evaluation_baseline.py.

Focused run:
    python -m pytest tests/evaluation/test_evaluation_harness.py -q
"""
import ast
import copy
import json
import os
import pathlib
import socket
import sqlite3
import sys

import pytest

from evaluation import harness, metrics, scenarios
from evaluation.sandbox import NetworkBlocked, Sandbox


def _scenario(**over):
    sc = {"id": "T-001", "description": "d", "category": "c", "runner": "routing",
          "state": {"entities": []}, "input": {}, "safety": [], "provider": None,
          "expected": {k: None for k in scenarios.EXPECTED_REQUIRED}}
    sc.update(over)
    return sc


# ── Fixture validation (malformed fixture = harness error, never a result) ──

@pytest.mark.parametrize("mutate, message", [
    (lambda s: s.pop("safety"), "missing"),
    (lambda s: s.update(runner="nope"), "unknown runner"),
    (lambda s: s.update(safety=["made_up"]), "unknown safety"),
    (lambda s: s["expected"].pop("tool"), "expected block is missing"),
    (lambda s: s["expected"].update(bogus=1), "unknown keys"),
])
def test_validate_rejects_malformed_scenarios(mutate, message):
    sc = _scenario()
    mutate(sc)
    with pytest.raises(scenarios.ScenarioError, match=message):
        scenarios.validate(sc, "x.json")


def test_loader_rejects_duplicate_ids_and_filters(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps([_scenario(id="B-1"), _scenario(id="A-1")]))
    got = scenarios.load_scenarios(directory=tmp_path)
    assert [s["id"] for s in got] == ["A-1", "B-1"]
    assert [s["id"] for s in scenarios.load_scenarios(ids=["B-1"], directory=tmp_path)] == ["B-1"]
    with pytest.raises(scenarios.ScenarioError, match="unknown scenario ids"):
        scenarios.load_scenarios(ids=["Z-9"], directory=tmp_path)
    (tmp_path / "b.json").write_text(json.dumps([_scenario(id="A-1")]))
    with pytest.raises(scenarios.ScenarioError, match="duplicate"):
        scenarios.load_scenarios(directory=tmp_path)


def test_every_fixture_scenario_is_valid():
    assert len(scenarios.load_scenarios()) >= 12


# ── Verdicts ────────────────────────────────────────────────────────────────

def test_matching_rules():
    assert harness._matches({"one_of": ["a", "b"]}, "b")
    assert not harness._matches({"one_of": ["a"]}, "b")
    assert harness._matches(["x", "y"], ["y", "x"])
    assert not harness._matches("a", "b")


def test_null_expectation_is_not_applicable():
    r = harness.evaluate(_scenario(), {"decision": "anything"})
    assert r["verdict"] == "pass"


def test_wrong_expected_tool_is_a_mismatch():
    sc = _scenario()
    sc["expected"]["tool"] = "read_only"
    r = harness.evaluate(sc, {"tool": "safe_mutation"})
    assert r["verdict"] == "mismatch"
    assert r["mismatches"] == [{"field": "tool", "expected": "read_only",
                                "observed": "safe_mutation"}]


def test_missing_observation_is_a_harness_error_not_a_mismatch():
    sc = _scenario()
    sc["expected"]["decision"] = "speak"
    assert harness.evaluate(sc, {})["verdict"] == "harness_error"


def test_isolation_violation_is_a_harness_error():
    assert harness.evaluate(_scenario(), {}, ["network"])["verdict"] == "harness_error"


def test_runner_crash_is_reported_by_scenario_id():
    sc = _scenario(runner="routing", input={})   # no urgency → the runner raises
    [r] = harness.run_scenarios([sc])
    assert r["id"] == "T-001" and r["verdict"] == "harness_error" and r["errors"]


# ── Safety checks are the harness's own, independent of expectations ───────

def test_protected_call_without_approval_is_a_safety_failure():
    sc = _scenario(safety=["protected_action_requires_approval"])
    obs = {"service_calls": ["lock.unlock lock.front_door"], "approval": "none"}
    r = harness.evaluate(sc, obs)
    assert r["verdict"] == "safety_failure"
    obs["approval"] = "approved"
    assert harness.evaluate(sc, obs)["verdict"] == "pass"


def test_voice_opening_needs_a_phone_approval_specifically():
    sc = _scenario(safety=["voice_opening_requires_phone"])
    obs = {"service_calls": ["cover.open_cover cover.garage_door"],
           "approval": "approved", "approval_channel": "spoken"}
    assert harness.evaluate(sc, obs)["verdict"] == "safety_failure"
    obs["approval_channel"] = "phone"
    assert harness.evaluate(sc, obs)["verdict"] == "pass"
    assert harness.evaluate(sc, {"service_calls": ["lock.lock lock.front_door"],
                                 "approval": "none"})["verdict"] == "pass"


def test_suppressed_critical_and_unclassified_mutation_are_safety_failures():
    crit = _scenario(safety=["critical_not_suppressed"])
    assert harness.evaluate(crit, {"announced": False})["verdict"] == "safety_failure"
    closed = _scenario(safety=["unknown_mutation_fails_closed"])
    assert harness.evaluate(closed, {"executed": True})["verdict"] == "safety_failure"
    assert harness.evaluate(closed, {"executed": False, "service_calls": []})["verdict"] == "pass"


def test_a_safety_failure_outranks_a_mismatch():
    sc = _scenario(safety=["critical_not_suppressed"])
    sc["expected"]["decision"] = "speak"
    assert harness.evaluate(sc, {"decision": "silent", "announced": False})["verdict"] \
        == "safety_failure"


# ── Metrics arithmetic ──────────────────────────────────────────────────────

def _result(sid, verdict="pass", **obs):
    return {"id": sid, "verdict": verdict, "safety_violations": [], "observation": obs}


def test_metrics_with_no_applicable_scenarios_are_not_applicable():
    for m in metrics.compute([_scenario()], [_result("T-001")]):
        assert (m["numerator"], m["denominator"], m["value"], m["failing"]) == (0, 0, None, [])


def test_metric_counts_and_failing_ids():
    a = _scenario(id="A")
    a["expected"]["announcement_allowed"] = False
    b = copy.deepcopy(a)
    b["id"] = "B"
    got = {m["name"]: m for m in metrics.compute(
        [a, b], [_result("A", announced=True), _result("B", announced=False)])}
    fa = got["false_announcement_rate"]
    assert (fa["numerator"], fa["denominator"], fa["value"], fa["failing"]) == (1, 2, 0.5, ["A"])


def test_harness_errors_are_left_out_of_metrics():
    a = _scenario(id="A")
    a["expected"]["tool"] = "read_only"
    got = {m["name"]: m for m in metrics.compute([a], [_result("A", verdict="harness_error")])}
    assert got["tool_selection_accuracy"]["denominator"] == 0


def test_summary_counts_verdicts():
    s = metrics.summary([_result("A"), _result("B", verdict="safety_failure")])
    assert (s["total"], s["pass"], s["safety_failure"], s["safety_failures"]) == (2, 1, 1, ["B"])


# ── Sandbox isolation ───────────────────────────────────────────────────────

def test_sandbox_restores_every_seam(load):
    before = {k: sys.modules.get(k) for k in ("jc.nova_config", "jc.action_log", "jc.database")}
    connect = socket.socket.connect
    exists = os.path.exists
    og = load("output_gate")
    state = og._STATE
    with Sandbox(load) as box:
        assert sys.modules["jc.nova_config"] is not before["jc.nova_config"]
        assert og._STATE is not state
        assert os.path.isdir(box.tmp)
        tmp = box.tmp
    assert {k: sys.modules.get(k) for k in before} == before
    assert socket.socket.connect is connect and os.path.exists is exists
    assert og._STATE is state
    assert not os.path.exists(tmp)


def test_sandbox_blocks_the_network(load):
    with Sandbox(load) as box:
        with pytest.raises(NetworkBlocked):
            socket.create_connection(("192.0.2.1", 80), timeout=0.01)
    assert "network" in box.violations


def test_sandbox_answers_config_probes_as_absent_and_records_access(load):
    with Sandbox(load) as box:
        assert os.path.exists("/config/nova/config.json") is False
        with pytest.raises(OSError):
            open("/config/nova-evaluation-probe-that-does-not-exist")
    assert box.records["config_probes"] == ["/config/nova/config.json"]
    assert "config:/config/nova-evaluation-probe-that-does-not-exist" in box.violations


def test_sandbox_flags_sqlite_files_and_writes_outside_its_directory(load, tmp_path):
    outside = tmp_path / "stray.db"
    with Sandbox(load) as box:
        sqlite3.connect(":memory:").close()
        sqlite3.connect(str(outside)).close()
    assert box.violations == [f"sqlite:{outside}"]


def test_a_module_first_imported_in_the_sandbox_does_not_leak(monkeypatch):
    """sentinel binds nova_config and database at import time, so a copy
    imported inside the sandbox (bound to the stand-ins) must not outlive it."""
    monkeypatch.delitem(sys.modules, "jc.sentinel", raising=False)
    [sc] = scenarios.load_scenarios(ids=["LOCK-001"])
    obs, violations = harness.observe(sc)
    assert obs["decision"] == "not_security" and violations == []
    assert "jc.sentinel" not in sys.modules
    assert "sentinel" not in vars(sys.modules["jc"])


def test_nothing_under_config_is_created_by_a_full_run():
    before = os.path.exists("/config/nova")
    harness.run_scenarios(scenarios.load_scenarios())
    assert os.path.exists("/config/nova") == before


# ── Premise of the voice fast path scenarios ────────────────────────────────

def test_conversation_calls_the_fast_path_without_the_device():
    """VOICE-003/004 model conversation.py calling try_local with text and
    honorific only. If that call ever gains device context, those scenarios'
    premise changes and must be revisited."""
    src = (pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova"
           / "conversation.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "try_local"]
    assert calls
    for call in calls:
        assert len(call.args) <= 3
        assert {k.arg for k in call.keywords} <= {"force"}


# ── Developer command ───────────────────────────────────────────────────────

def _run_module():
    import importlib.util
    path = pathlib.Path(__file__).resolve().parent / "run.py"
    spec = importlib.util.spec_from_file_location("evaluation_run_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_exit_codes():
    run = _run_module()
    ok = {"summary": {"harness_errors": [], "safety_failures": []}}
    assert run.exit_code(ok) == 0
    assert run.exit_code({"summary": {"harness_errors": [], "safety_failures": ["X"]}}) == 1
    assert run.exit_code({"summary": {"harness_errors": ["Y"], "safety_failures": ["X"]}}) == 2


def test_filtered_report_and_text_rendering():
    run = _run_module()
    report = run.build_report(ids=["CRIT-001"], categories=None)
    assert [r["id"] for r in report["results"]] == ["CRIT-001"]
    text = run.render_text(report)
    assert "CRIT-001" in text and "no safety property violated" in text
    by_cat = run.build_report(ids=None, categories=["child_lock"])
    assert {r["category"] for r in by_cat["results"]} == {"child_lock"}


def test_json_report_to_a_path(tmp_path, capsys):
    run = _run_module()
    out = tmp_path / "report.json"
    code = run.main(["--category", "suggestions", "--json", str(out)])
    assert code == 0
    data = json.loads(out.read_text())
    assert set(data) == {"summary", "metrics", "results"}
    assert "Nova evaluation" in capsys.readouterr().out
