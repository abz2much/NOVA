"""The optional adaptive confidence bar for automation suggestions.

With adaptive_suggestion_threshold on, the bar a pattern must clear moves with
how people received recent suggestions: stricter when they were mostly
unwelcome, a little looser when nearly all landed well. It needs a minimum
number of judged outcomes, reads only the "suggestion" outcomes (never safety
ones), is cached for five minutes, and the result stays within 0.3 to 0.95.
"""
from __future__ import annotations

import time

import pytest


class Outcomes:
    """Stands in for decision_record.outcome_rate and remembers each query."""

    def __init__(self, judged=10, unwelcome=0.0):
        self.judged, self.unwelcome = judged, unwelcome
        self.asked: list = []
        self.fail = False

    def __call__(self, kind, window_s=None, db_path=None, prefix=False):
        self.asked.append((kind, window_s, prefix))
        if self.fail:
            raise OSError("decisions.db unreadable")
        return {"kind": kind, "judged": self.judged, "unwelcome_rate": self.unwelcome}


@pytest.fixture
def pa(load):
    return load("pattern_analyzer")


@pytest.fixture
def patterns(load, monkeypatch):
    mod = load("automation.patterns")
    # An expired cache going in, and the real one put back afterwards.
    monkeypatch.setitem(mod._ADAPT_CACHE, "ts", 0.0)
    monkeypatch.setitem(mod._ADAPT_CACHE, "delta", 0.0)
    return mod


@pytest.fixture
def adaptive(load, monkeypatch):
    """Switch the option on or off, and feed it outcomes."""
    settings = {"adaptive_suggestion_threshold": True}
    monkeypatch.setattr(load("nova_config"), "get",
                        lambda key, default=None: settings.get(key, default))
    outcomes = Outcomes()
    monkeypatch.setattr(load("decision_record"), "outcome_rate", outcomes)
    outcomes.settings = settings
    return outcomes


# ── the learned move ─────────────────────────────────────────────────────────

def test_switched_off_by_default_and_reads_nothing(pa, patterns, adaptive):
    del adaptive.settings["adaptive_suggestion_threshold"]
    adaptive.unwelcome = 0.9
    assert pa._learned_threshold_delta() == 0.0
    assert adaptive.asked == []


def test_explicitly_off(pa, patterns, adaptive):
    adaptive.settings["adaptive_suggestion_threshold"] = False
    adaptive.unwelcome = 0.9
    assert pa._learned_threshold_delta() == 0.0


@pytest.mark.parametrize("judged, unwelcome, delta", [
    (3, 0.9, 0.0),      # too few outcomes to judge by
    (4, 0.9, 0.0),      # one short of the minimum sample
    (5, 0.9, 0.15),     # exactly the minimum sample
    (10, 0.6, 0.15),    # mostly unwelcome
    (10, 0.5, 0.15),
    (10, 0.35, 0.07),   # somewhat unwelcome
    (10, 0.3, 0.07),
    (10, 0.2, 0.0),     # the neutral band
    (10, 0.1, -0.07),
    (10, 0.05, -0.07),  # almost all welcome
])
def test_delta_from_recent_outcomes(pa, patterns, adaptive, judged, unwelcome, delta):
    adaptive.judged, adaptive.unwelcome = judged, unwelcome
    assert pa._learned_threshold_delta() == delta


def test_reads_only_suggestion_outcomes_over_a_month(pa, patterns, adaptive):
    pa._learned_threshold_delta()
    assert adaptive.asked == [("suggestion", 30 * 86400.0, False)]


def test_unreadable_outcomes_leave_the_bar_alone(pa, patterns, adaptive):
    adaptive.fail = True
    adaptive.unwelcome = 0.9
    assert pa._learned_threshold_delta() == 0.0


def test_result_is_cached_for_five_minutes(pa, patterns, adaptive):
    adaptive.unwelcome = 0.6
    assert pa._learned_threshold_delta() == 0.15
    adaptive.unwelcome = 0.05
    assert pa._learned_threshold_delta() == 0.15      # still the cached move
    assert len(adaptive.asked) == 1
    # Once the cache is older than five minutes it is read again.
    patterns._ADAPT_CACHE["ts"] = time.time() - 301
    assert pa._learned_threshold_delta() == -0.07
    assert len(adaptive.asked) == 2


# ── the bar itself ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("base, unwelcome, bar", [
    (0.9, 0.6, 0.95),     # 0.9 + 0.15 is held at the ceiling
    (0.32, 0.05, 0.3),    # 0.32 - 0.07 is held at the floor
])
def test_effective_threshold_is_clamped(pa, patterns, adaptive, monkeypatch,
                                        base, unwelcome, bar):
    monkeypatch.setattr(patterns, "CONFIDENCE_THRESHOLD", base)
    adaptive.unwelcome = unwelcome
    assert pa._effective_threshold() == bar


@pytest.mark.parametrize("unwelcome, bar", [(0.35, 0.72), (0.2, 0.65), (0.05, 0.58)])
def test_effective_threshold_in_range(pa, patterns, adaptive, monkeypatch, unwelcome, bar):
    monkeypatch.setattr(patterns, "CONFIDENCE_THRESHOLD", 0.65)
    adaptive.unwelcome = unwelcome
    assert pa._effective_threshold() == pytest.approx(bar)


def test_effective_threshold_is_the_base_when_off(pa, patterns, adaptive, monkeypatch):
    adaptive.settings["adaptive_suggestion_threshold"] = False
    monkeypatch.setattr(patterns, "CONFIDENCE_THRESHOLD", 0.65)
    assert pa._effective_threshold() == 0.65
