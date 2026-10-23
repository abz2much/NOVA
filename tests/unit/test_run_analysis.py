"""The panel's "Analyze Now" button: run_analysis_now().

A manual run skips only the six hour wait between analyses. The enough-data
gate still applies, so a new install gets {ran: False, reason} instead of
noise, and a run that goes ahead reports what it found alongside the store's
own totals.
"""
from __future__ import annotations

import sys
import types
from datetime import date, datetime, time as clock, timedelta

import pytest


@pytest.fixture
def core(load):
    return load("cognitive_core")


@pytest.fixture
def patterns(load, monkeypatch):
    load("pattern_analyzer")
    mod = load("automation.patterns")
    monkeypatch.setattr(mod, "MIN_OCCURRENCES", 5)
    monkeypatch.setattr(mod, "CONFIDENCE_THRESHOLD", 0.65)
    return mod


@pytest.fixture
def quiet_run(core, patterns, monkeypatch):
    """No AI review, no history import, and set_thresholds calls recorded
    instead of changing the shipped thresholds."""
    async def no_review(hass):
        return None
    monkeypatch.setitem(sys.modules, "jc.suggestion_review",
                        types.SimpleNamespace(reviewer_for=no_review))

    async def no_import(hass, days=30):
        return {"imported": 0}
    monkeypatch.setattr(core, "backfill_from_history", no_import)
    monkeypatch.setattr(core._CORE, "config", {})
    applied: list = []
    monkeypatch.setattr(patterns, "set_thresholds", lambda *args: applied.append(args))
    return applied


class _StubAnalyzer:
    """Stands in for PatternAnalyzer: a fixed eligibility and a fixed result."""

    def __init__(self, eligible: bool):
        self.eligible = eligible
        self._last_analysis = 999.0
        self._last_result: dict = {}
        self.runs = 0

    def should_analyze(self):
        return self.eligible

    async def analyze(self, hass):
        self.runs += 1
        self._last_result = {"patterns_found": 3, "new_suggestions": 2,
                             "person_routines": 1, "facts": 0}
        return ["a", "b", "c"]


def _stats_only(days: int, changes: int):
    return types.SimpleNamespace(
        get_pattern_stats=lambda: {"days_of_data": days, "state_changes": changes})


@pytest.mark.parametrize("eligible", [False, True])
async def test_a_manual_run_always_clears_the_six_hour_wait(
        core, patterns, quiet_run, monkeypatch, fake_hass, eligible):
    stub = _StubAnalyzer(eligible)
    monkeypatch.setattr(patterns, "get_analyzer", lambda: stub)
    monkeypatch.setattr(core._CORE, "state_logger", _stats_only(3, 12))
    await core.run_analysis_now(fake_hass)
    assert stub._last_analysis == 0.0


async def test_too_little_history_gives_a_reason_and_the_totals(
        core, patterns, quiet_run, monkeypatch, fake_hass):
    stub = _StubAnalyzer(eligible=False)
    monkeypatch.setattr(patterns, "get_analyzer", lambda: stub)
    monkeypatch.setattr(core._CORE, "state_logger", _stats_only(3, 12))
    res = await core.run_analysis_now(fake_hass)

    assert res["ran"] is False
    assert "week" in res["reason"].lower() and "7 days" in res["reason"]
    assert (res["days_of_data"], res["state_changes"]) == (3, 12)
    # Nothing ran and the thresholds were left alone.
    assert stub.runs == 0 and quiet_run == []


async def test_an_eligible_run_reports_what_it_found(
        core, patterns, quiet_run, monkeypatch, fake_hass):
    stub = _StubAnalyzer(eligible=True)
    monkeypatch.setattr(patterns, "get_analyzer", lambda: stub)
    monkeypatch.setattr(core._CORE, "state_logger", _stats_only(20, 5000))
    res = await core.run_analysis_now(fake_hass)

    assert res["ran"] is True and stub.runs == 1
    assert (res["patterns_found"], res["new_suggestions"]) == (3, 2)
    assert (res["person_routines"], res["facts"]) == (1, 0)
    assert (res["state_changes"], res["days_of_data"]) == (5000, 20)
    assert res["backfill"] == {"imported": 0}
    # The panel defaults go in before the run: 4 repeats, 0.55 confidence.
    assert quiet_run == [(4, 0.55)]


async def test_without_a_state_logger_the_totals_read_as_zero(
        core, patterns, quiet_run, monkeypatch, fake_hass):
    stub = _StubAnalyzer(eligible=False)
    monkeypatch.setattr(patterns, "get_analyzer", lambda: stub)
    monkeypatch.setattr(core._CORE, "state_logger", None)
    res = await core.run_analysis_now(fake_hass)
    assert res["ran"] is False
    assert (res["days_of_data"], res["state_changes"]) == (0, 0)


# ── with the real analyzer over a real store ────────────────────────────────

# One day's changes: (hour, entity, state). Five a day, the first at midnight
# so the store's age in whole days does not depend on when the test runs.
_DAY = ((0, "light.lamp", "off"), (7, "light.hall", "on"), (8, "light.hall", "off"),
        (18, "light.porch", "on"), (23, "light.porch", "off"))


def _real_setup(core, patterns, monkeypatch, tmp_path, days: int):
    logger = core.StateLogger(db_path=str(tmp_path / "patterns.db"))
    rows = []
    for back in range(days, 0, -1):
        for hour, entity_id, state in _DAY:
            at = datetime.combine(date.today() - timedelta(days=back), clock(hour, 0))
            rows.append((at.isoformat(), entity_id, "light", "unknown", state))
    assert logger.bulk_insert_history(rows) == len(rows)
    monkeypatch.setattr(core._CORE, "state_logger", logger)

    analyzer = patterns.PatternAnalyzer()
    analyzer._db = str(tmp_path / "patterns.db")
    monkeypatch.setattr(analyzer, "_promote_to_knowledge", lambda found: 0)
    monkeypatch.setattr(patterns, "get_analyzer", lambda: analyzer)
    return analyzer


async def test_three_days_in_a_real_store_is_turned_away(
        core, patterns, quiet_run, monkeypatch, fake_hass, tmp_path):
    analyzer = _real_setup(core, patterns, monkeypatch, tmp_path, days=3)
    res = await core.run_analysis_now(fake_hass)
    assert res["ran"] is False
    assert (res["days_of_data"], res["state_changes"]) == (3, 15)
    assert analyzer._last_result == {}


async def test_twelve_days_in_a_real_store_runs_and_explains_itself(
        core, patterns, quiet_run, monkeypatch, fake_hass, tmp_path):
    _real_setup(core, patterns, monkeypatch, tmp_path, days=12)
    res = await core.run_analysis_now(fake_hass)

    assert res["ran"] is True
    assert (res["days_of_data"], res["state_changes"]) == (12, 60)
    # Each of the five daily changes is a routine on 12 of 12 days.
    assert res["patterns_found"] == 5
    assert res["new_suggestions"] == 5
    assert res["near_misses"] == []
    diagnostic = res["diagnostic"]
    assert diagnostic["total_days"] == 12
    assert {s["entity_id"] for s in diagnostic["top_sources"]} == {
        "light.lamp", "light.hall", "light.porch"}
