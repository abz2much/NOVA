"""Nova's deterministic evaluation baseline.

Runs every scenario in fixtures/ against Nova's real decision code and
compares the outcome with the pinned baseline:

  * a scenario not listed in baseline.json must pass;
  * a listed behaviour mismatch must still show that verdict, so a change
    in behaviour (better or worse) is noticed and the baseline updated in
    the same change;
  * any safety failure fails the suite, except the reported defects listed
    in baseline.json, which are strict xfails: they show as XFAIL on every
    run until fixed, and XPASS (a failure) once fixed so the entry is
    removed. They are open defects, not accepted behaviour.

Focused run:
    python -m pytest tests/evaluation -q
Human readable report:
    python tests/evaluation/run.py
"""
import json

import pytest

from evaluation import harness, metrics, scenarios

REQUIRED_CATEGORIES = {
    "entity_resolution", "automation_attribution", "child_lock", "nobody_home",
    "voice_protected", "camera_repeat", "provider_unavailable", "critical_gate",
    "tool_selection", "action_reporting", "suggestions",
}

_BASELINE = scenarios.load_baseline()
REPORTED_SAFETY_DEFECTS = sorted(
    sid for sid, pinned in _BASELINE.items() if pinned["verdict"] == "safety_failure")


@pytest.fixture(scope="module")
def evaluation():
    chosen = scenarios.load_scenarios()
    results = harness.run_scenarios(chosen)
    return chosen, {r["id"]: r for r in results}, results


def test_every_required_category_has_scenarios(evaluation):
    chosen, _, _ = evaluation
    counts = {}
    for sc in chosen:
        counts[sc["category"]] = counts.get(sc["category"], 0) + 1
    assert REQUIRED_CATEGORIES <= set(counts), sorted(REQUIRED_CATEGORIES - set(counts))
    assert [s["id"] for s in chosen] == sorted(s["id"] for s in chosen)


def test_no_harness_errors(evaluation):
    _, by_id, _ = evaluation
    errors = {sid: r["errors"] for sid, r in sorted(by_id.items())
              if r["verdict"] == "harness_error"}
    assert errors == {}


def test_baseline_names_only_known_scenarios(evaluation):
    _, by_id, _ = evaluation
    assert sorted(set(_BASELINE) - set(by_id)) == []
    assert all(p["verdict"] in ("mismatch", "safety_failure") and p.get("note")
               for p in _BASELINE.values())


def test_behaviour_matches_pinned_baseline(evaluation):
    _, by_id, _ = evaluation
    drift = {}
    for sid, r in sorted(by_id.items()):
        pinned = _BASELINE.get(sid, {}).get("verdict", "pass")
        if "safety_failure" in (pinned, r["verdict"]):
            continue   # safety is judged separately below
        if r["verdict"] != pinned:
            drift[sid] = {"pinned": pinned, "now": r["verdict"],
                          "mismatches": r["mismatches"]}
    assert drift == {}, json.dumps(drift, indent=2, sort_keys=True)


def test_no_safety_failure_outside_the_reported_defects(evaluation):
    _, by_id, _ = evaluation
    unreported = sorted(sid for sid, r in by_id.items()
                        if r["verdict"] == "safety_failure"
                        and sid not in REPORTED_SAFETY_DEFECTS)
    assert unreported == [], {sid: by_id[sid]["safety_violations"] for sid in unreported}


@pytest.mark.parametrize("sid", REPORTED_SAFETY_DEFECTS)
@pytest.mark.xfail(strict=True,
                   reason="reported safety defect awaiting an approved fix (see baseline.json)")
def test_reported_safety_defect(evaluation, sid):
    _, by_id, _ = evaluation
    assert by_id[sid]["verdict"] != "safety_failure", by_id[sid]["safety_violations"]


def test_metrics_match_pinned_values(evaluation):
    chosen, _, results = evaluation
    pinned = scenarios.load_metrics_baseline()
    now = {m["name"]: {"numerator": m["numerator"], "denominator": m["denominator"],
                       "failing": m["failing"]}
           for m in metrics.compute(chosen, results)}
    assert now == pinned


def test_repeated_runs_are_identical(evaluation):
    chosen, _, results = evaluation
    again = harness.run_scenarios(chosen)
    assert json.dumps(again, sort_keys=True) == json.dumps(results, sort_keys=True)
