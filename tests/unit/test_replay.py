"""Offline replay / policy evaluation over Decision Records — v7.48.0."""
from __future__ import annotations

import pytest


@pytest.fixture
def replay(load):
    return load("replay")


@pytest.fixture
def pattern_analyzer(load):
    return load("pattern_analyzer")


def _rec(conf, outcome):
    return {"confidence": conf, "outcome": outcome}


# ── bucketing ─────────────────────────────────────────────────────────────────
def test_threshold_buckets(replay):
    recs = [
        _rec(0.9, "good"),         # acted & good  -> kept_right
        _rec(0.8, "wrong"),        # acted & bad   -> kept_wrong
        _rec(0.2, "good"),         # held  & good  -> suppressed_right (good call lost)
        _rec(0.1, "unnecessary"),  # held  & bad   -> suppressed_wrong (mistake avoided)
    ]
    r = replay.evaluate_threshold(recs, 0.5)
    assert (r.kept_right, r.kept_wrong, r.suppressed_right, r.suppressed_wrong) == (1, 1, 1, 1)
    assert r.total == 4
    assert r.correct == 2                      # kept_right + suppressed_wrong
    assert r.accuracy == 0.5
    assert r.mistakes_avoided == 1
    assert r.good_calls_lost == 1


def test_good_outcome_is_treated_as_positive_regression(replay):
    """Regression for the vocabulary bug: RIGHT = "right" never matched any
    real Decision Record outcome (only good/unnecessary/wrong are ever
    written), so a "good" record used to be silently dropped by _judged()
    entirely — every judged record replay.py ever actually saw was "wrong",
    scored as a negative example. A "good" record must now count, and count
    as the POSITIVE class, matching decision_record.calibration()."""
    recs = [_rec(0.9, "good")]
    r = replay.evaluate_threshold(recs, 0.5)
    assert r.total == 1                # counted at all (used to be dropped)
    assert r.kept_right == 1           # counted as a correct/positive call
    assert r.kept_wrong == 0
    assert r.accuracy == 1.0


def test_unnecessary_outcome_is_treated_as_negative(replay):
    """"unnecessary" is a real, distinct negative outcome (decision_record.
    OUTCOME_UNNECESSARY) — it must count same as "wrong", not be skipped."""
    r = replay.evaluate_threshold([_rec(0.9, "unnecessary")], 0.5)
    assert r.total == 1
    assert r.kept_wrong == 1
    assert r.kept_right == 0


def test_old_right_vocabulary_no_longer_recognised(replay):
    """The removed "right" string is not a real outcome anywhere in the
    integration — a record carrying it (e.g. stale test fixture data) must
    be skipped as unjudged, not silently treated as good or bad."""
    r = replay.evaluate_threshold([_rec(0.9, "right")], 0.5)
    assert r.total == 0


def test_skips_unjudged_and_missing_confidence(replay):
    recs = [
        _rec(0.9, "good"),
        _rec(0.9, None),          # unjudged
        _rec(None, "wrong"),      # no confidence
        {"outcome": "good"},      # no confidence key
        _rec("nan-ish", "good"),  # non-numeric confidence
    ]
    r = replay.evaluate_threshold(recs, 0.5)
    assert r.total == 1                          # only the first record counts


def test_threshold_at_boundary_is_inclusive(replay):
    # confidence exactly == threshold counts as "acted"
    r = replay.evaluate_threshold([_rec(0.5, "good")], 0.5)
    assert r.kept_right == 1 and r.suppressed_right == 0


# ── sweep + recommendation ────────────────────────────────────────────────────
def _separable_corpus():
    """Good decisions cluster high-confidence, wrong ones low — so a mid
    threshold separates them well."""
    recs = []
    for _ in range(20):
        recs.append(_rec(0.85, "good"))
    for _ in range(20):
        recs.append(_rec(0.25, "wrong"))
    return recs


def test_recommend_picks_separating_threshold(replay):
    rec = replay.recommend_threshold(_separable_corpus(), min_samples=10)
    assert rec is not None
    best = rec["recommended"]
    # a threshold between 0.25 and 0.85 should classify all 40 correctly
    assert 0.25 < best["threshold"] <= 0.85
    assert best["accuracy"] == 1.0
    assert rec["samples"] == 40


def test_recommend_withheld_below_min_samples(replay):
    recs = [_rec(0.9, "good"), _rec(0.2, "wrong")]      # only 2 judged
    assert replay.recommend_threshold(recs, min_samples=25) is None


def test_recommend_tie_breaks_toward_lower_threshold(replay):
    # all good, all high confidence -> every threshold <= 0.9 is 100% accurate;
    # recommendation should prefer the lowest (act more readily)
    recs = [_rec(0.9, "good") for _ in range(30)]
    rec = replay.recommend_threshold(recs, min_samples=10)
    assert rec["recommended"]["threshold"] == 0.0
    assert rec["recommended"]["accuracy"] == 1.0


def test_sweep_covers_grid(replay):
    results = replay.sweep(_separable_corpus())
    assert len(results) == 21                    # 0.00..1.00 step 0.05
    assert all(r.total == 40 for r in results)


# ── DB-facing convenience degrades safely ─────────────────────────────────────
def test_replay_kind_not_ready_with_no_data(replay, monkeypatch):
    import sys
    import types
    fake = types.ModuleType("jc.decision_record")
    fake.recent = lambda **k: []                 # no records
    monkeypatch.setitem(sys.modules, "jc.decision_record", fake)
    out = replay.replay_kind("intrusion", min_samples=25)
    assert out["ready"] is False
    assert out["samples"] == 0 and out["needed"] == 25


def test_replay_kind_ready_with_enough_judged(replay, monkeypatch):
    import sys
    import types
    fake = types.ModuleType("jc.decision_record")
    fake.recent = lambda **k: _separable_corpus()
    monkeypatch.setitem(sys.modules, "jc.decision_record", fake)
    out = replay.replay_kind("intrusion", min_samples=10)
    assert out["ready"] is True
    assert out["kind"] == "intrusion"
    assert out["recommended"]["accuracy"] == 1.0


# ── replay_one — current-policy Decision Lab replay (Phase 4) ────────────────

def _decision(kind="suggestion", confidence=0.8, decision="propose automation",
             outcome="good", id_=7):
    return {"id": id_, "kind": kind, "confidence": confidence,
            "decision": decision, "outcome": outcome}


def test_replay_one_label_is_exact_required_text(replay, pattern_analyzer, monkeypatch):
    monkeypatch.setattr(pattern_analyzer, "_effective_threshold", lambda: 0.65)
    out = replay.replay_one(_decision())
    assert out["label"] == ("Replay using current settings. This is not an "
                            "exact reconstruction of the original decision.")


def test_replay_one_recorded_fields_pass_through_unchanged(replay, pattern_analyzer, monkeypatch):
    monkeypatch.setattr(pattern_analyzer, "_effective_threshold", lambda: 0.65)
    rec = _decision(decision="propose automation", outcome="wrong", confidence=0.5, id_=42)
    out = replay.replay_one(rec)
    assert out["id"] == 42
    assert out["kind"] == "suggestion"
    assert out["recorded_decision"] == "propose automation"
    assert out["recorded_outcome"] == "wrong"
    assert out["recorded_confidence"] == 0.5


def test_replay_one_would_pass_current_threshold(replay, pattern_analyzer, monkeypatch):
    monkeypatch.setattr(pattern_analyzer, "_effective_threshold", lambda: 0.65)
    above = replay.replay_one(_decision(confidence=0.9))
    below = replay.replay_one(_decision(confidence=0.3))
    assert above["supported"] is True
    assert above["would_pass_current_threshold"] is True
    assert below["would_pass_current_threshold"] is False
    assert above["current_threshold"] == 0.65


def test_replay_one_within_0_05_of_threshold(replay, pattern_analyzer, monkeypatch):
    monkeypatch.setattr(pattern_analyzer, "_effective_threshold", lambda: 0.65)
    near = replay.replay_one(_decision(confidence=0.68))     # |0.68-0.65| = 0.03
    far = replay.replay_one(_decision(confidence=0.95))      # |0.95-0.65| = 0.30
    boundary = replay.replay_one(_decision(confidence=0.70)) # |0.70-0.65| = 0.05 exactly
    assert near["within_0_05_of_threshold"] is True
    assert far["within_0_05_of_threshold"] is False
    assert boundary["within_0_05_of_threshold"] is True      # boundary is inclusive


def test_replay_one_unsupported_kind_invents_no_threshold(replay, pattern_analyzer, monkeypatch):
    """A kind with no real adjustable policy must come back unsupported —
    never a fabricated threshold for it to compare against."""
    def _boom():
        raise AssertionError("must not consult a threshold for an unsupported kind")
    monkeypatch.setattr(pattern_analyzer, "_effective_threshold", _boom)

    out = replay.replay_one(_decision(kind="intrusion"))
    assert out["supported"] is False
    assert "current_threshold" not in out
    assert "would_pass_current_threshold" not in out
    assert "reason" in out


def test_replay_one_missing_confidence_is_unsupported(replay, pattern_analyzer, monkeypatch):
    monkeypatch.setattr(pattern_analyzer, "_effective_threshold", lambda: 0.65)
    out = replay.replay_one(_decision(confidence=None))
    assert out["supported"] is False
    assert "current_threshold" not in out


def test_replay_one_never_returns_original_would_act(replay, pattern_analyzer, monkeypatch):
    """Nova never stored the historical threshold, so there is nothing to
    reconstruct — this key must never appear, supported or not."""
    monkeypatch.setattr(pattern_analyzer, "_effective_threshold", lambda: 0.65)
    supported = replay.replay_one(_decision(kind="suggestion"))
    unsupported = replay.replay_one(_decision(kind="intrusion"))
    assert "original_would_act" not in supported
    assert "original_would_act" not in unsupported
