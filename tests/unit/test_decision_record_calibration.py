"""What Nova learns from judged decisions.

calibration() sets each decision's stated confidence against whether it was
judged good (good counts as 1, anything else 0). interruption_budget() turns
the recent share of unwelcome proactive output into a multiplier for the
announcement cap, and outcome_rate() gives one kind's verdict counts. All of
them read a throwaway database here. The numbers below are worked by hand
from the formulas in decision_record.py.
"""
import time

import pytest

PHONE = "phone"                  # a confirmed source (8.24.0)
CALL_OFF = "dismiss_intrusion"   # also confirmed


@pytest.fixture
def dr(load):
    return load("decision_record")


@pytest.fixture
def db(dr, tmp_path):
    path = str(tmp_path / "judged.sqlite")
    dr.ensure_schema(path)
    return path


def judge(dr, db, *verdicts, kind="intrusion", confidence=0.8, source=PHONE, ts=None):
    """Record one decision per verdict and judge it. A verdict of None leaves
    that decision unjudged."""
    for verdict in verdicts:
        rid = dr.record(kind, confidence=confidence, ts=ts, db_path=db)
        assert rid is not None
        if verdict is not None:
            assert dr.set_outcome(rid, verdict, source=source, db_path=db) is True


def bin_for(cal, lo, hi):
    (found,) = [b for b in cal["bins"] if (b["lo"], b["hi"]) == (lo, hi)]
    return found


# ── calibration ─────────────────────────────────────────────────────────────

def test_confidence_that_matched_reality_scores_near_zero(dr, db):
    judge(dr, db, "good", "good", confidence=0.9)
    judge(dr, db, "wrong", "wrong", confidence=0.1)
    cal = dr.calibration(db_path=db, bins=5)
    assert cal["n"] == 4 and cal["good_rate"] == 0.5
    assert cal["brier"] == pytest.approx(0.01) and cal["brier"] < 0.05
    assert cal["ece"] == pytest.approx(0.1)
    assert len(cal["bins"]) == 5
    assert bin_for(cal, 0.8, 1.0) == {"lo": 0.8, "hi": 1.0, "count": 2,
                                      "mean_confidence": 0.9, "good_rate": 1.0}
    assert bin_for(cal, 0.0, 0.2)["good_rate"] == 0.0
    # Bins nobody fell into say so rather than inventing a rate.
    assert bin_for(cal, 0.4, 0.6) == {"lo": 0.4, "hi": 0.6, "count": 0,
                                      "mean_confidence": None, "good_rate": None}


def test_overconfidence_shows_in_the_brier_score(dr, db):
    judge(dr, db, "wrong", "wrong", "wrong", confidence=0.9)
    cal = dr.calibration(db_path=db)
    assert cal["good_rate"] == 0.0
    assert cal["brier"] == pytest.approx(0.81) and cal["brier"] > 0.7
    assert cal["ece"] == pytest.approx(0.9)


def test_unnecessary_is_not_counted_as_good(dr, db):
    judge(dr, db, "unnecessary", "good", confidence=0.5)
    cal = dr.calibration(db_path=db)
    assert cal["good_rate"] == 0.5
    assert cal["brier"] == pytest.approx(0.25)


def test_unjudged_decisions_are_left_out(dr, db):
    judge(dr, db, "good", None, None, confidence=0.7)
    assert dr.calibration(db_path=db)["n"] == 1


def test_calibration_can_be_narrowed_to_one_kind(dr, db):
    judge(dr, db, "good", kind="intrusion", confidence=0.9)
    judge(dr, db, "wrong", "wrong", kind="suggestion", confidence=0.6)
    cal = dr.calibration(db_path=db, kind="suggestion")
    assert (cal["n"], cal["good_rate"], cal["kind"]) == (2, 0.0, "suggestion")


def test_confidence_outside_zero_to_one_is_clamped(dr, db):
    judge(dr, db, "good", confidence=1.7)
    cal = dr.calibration(db_path=db, bins=2)
    assert cal["brier"] == 0.0
    assert bin_for(cal, 0.5, 1.0)["mean_confidence"] == 1.0


def test_empty_store_has_no_scores(dr, db):
    cal = dr.calibration(db_path=db)
    assert cal["n"] == 0 and cal["brier"] is None and cal["bins"] == []
    assert cal["good_rate"] is None and cal["ece"] is None


def test_reading_a_missing_store_does_not_create_it(dr, tmp_path):
    missing = tmp_path / "never.sqlite"
    assert dr.calibration(db_path=str(missing))["n"] == 0
    assert dr.interruption_budget(db_path=str(missing))["assessment"] == "no data"
    assert not missing.exists()


# ── interruption budget ─────────────────────────────────────────────────────

@pytest.mark.parametrize("verdicts, rate, assessment, multiplier", [
    (["good"] * 4 + ["unnecessary"], 0.2, "healthy", 0.85),
    (["good"] * 3 + ["wrong"], 0.25, "borderline", 0.812),
    (["wrong", "unnecessary", "unnecessary", "good"], 0.75, "over-interrupting", 0.438),
], ids=["healthy", "borderline", "over"])
def test_budget_follows_the_unwelcome_share(dr, db, verdicts, rate, assessment, multiplier):
    judge(dr, db, *verdicts)
    budget = dr.interruption_budget(db_path=db, floor=0.25)
    assert budget["judged"] == len(verdicts)
    assert budget["unwelcome_rate"] == rate
    assert budget["assessment"] == assessment
    assert budget["multiplier"] == multiplier
    assert 0.25 <= budget["multiplier"] < 1.0


def test_budget_counts_each_verdict(dr, db):
    judge(dr, db, "good", "good", "unnecessary", "wrong")
    budget = dr.interruption_budget(db_path=db)
    assert (budget["good"], budget["unnecessary"], budget["wrong"]) == (2, 1, 1)


def test_budget_never_drops_below_the_floor(dr, db):
    judge(dr, db, "wrong", "wrong", "wrong")
    assert dr.interruption_budget(db_path=db, floor=0.4)["multiplier"] == 0.4


def test_no_judged_decisions_leave_the_cap_alone(dr, db):
    budget = dr.interruption_budget(db_path=db)
    assert budget["multiplier"] == 1.0 and budget["assessment"] == "no data"
    assert budget["unwelcome_rate"] is None


def test_budget_ignores_verdicts_nobody_confirmed(dr, db):
    judge(dr, db, "wrong", "wrong", source="auto_guess")
    judge(dr, db, "good")
    budget = dr.interruption_budget(db_path=db)
    assert budget["judged"] == 1 and budget["assessment"] == "healthy"
    assert budget["multiplier"] == 1.0


def test_budget_only_looks_inside_its_window(dr, db):
    judge(dr, db, "wrong", "wrong", ts=time.time() - 3 * 86400)
    judge(dr, db, "good")
    assert dr.interruption_budget(db_path=db, window_s=86400)["judged"] == 1


# ── per-kind outcome rate ───────────────────────────────────────────────────

def test_outcome_rate_keeps_kinds_apart(dr, db):
    judge(dr, db, "good", "good", "unnecessary", "wrong", kind="suggestion",
          confidence=0.7, source=CALL_OFF)
    judge(dr, db, "good", kind="intrusion", confidence=0.9, source=CALL_OFF)
    rate = dr.outcome_rate("suggestion", db_path=db)
    assert rate["judged"] == 4 and rate["good"] == 2
    assert rate["unwelcome_rate"] == 0.5 and rate["good_rate"] == 0.5
    # The intrusion verdict must not leak into the suggestion figures.
    assert dr.outcome_rate("intrusion", db_path=db)["judged"] == 1


def test_outcome_rate_prefix_covers_a_family_of_kinds(dr, db):
    judge(dr, db, "good", kind="anticipation_overdue")
    judge(dr, db, "wrong", kind="anticipation_routine")
    judge(dr, db, "wrong", kind="anticipationless")
    judge(dr, db, "good", kind="other")
    family = dr.outcome_rate("anticipation_", db_path=db, prefix=True)
    assert family["judged"] == 2 and family["good_rate"] == 0.5
    # Without the flag the name must match exactly.
    assert dr.outcome_rate("anticipation_", db_path=db)["judged"] == 0


def test_outcome_rate_for_an_unseen_kind_is_empty(dr, db):
    assert dr.outcome_rate("nothing_here", db_path=db) == {
        "kind": "nothing_here", "judged": 0, "good": 0, "unnecessary": 0, "wrong": 0,
        "good_rate": None, "unwelcome_rate": None}


# ── turning a rate into a threshold change ─────────────────────────────────

@pytest.mark.parametrize("rate, delta", [
    (0.9, 0.15), (0.5, 0.15), (0.49, 0.07), (0.3, 0.07), (0.2, 0.0),
    (0.1, -0.07), (0.0, -0.07), (None, -0.07), ("not a number", 0.0),
])
def test_threshold_delta_from_rate(dr, rate, delta):
    assert dr.threshold_delta_from_rate(rate) == delta
