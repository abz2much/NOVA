"""Routine and sequence scoring (Phase 8).

cognitive/patterns.py turns plain evidence into a confidence and an
explainable reason. These tests pin each rule, and that a strong routine
keeps exactly the confidence the original formula gave it.

Focused run:
    python -m pytest tests/unit/test_cognitive_patterns.py -q
"""
import sqlite3
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def sc(load):
    return load("cognitive.patterns")


def _routine(sc, **kw):
    base = dict(observations=20, positive_days=20, eligible_days=20, window_days=20,
                days_since_last=1.0, automated_observations=0)
    base.update(kw)
    return sc.score_time_routine(sc.RoutineEvidence(**base), min_occurrences=5)


def test_a_strong_routine_keeps_the_original_confidence(sc):
    for pos, elig in [(20, 20), (18, 20), (8, 8), (12, 15)]:
        out = _routine(sc, observations=pos, positive_days=pos, eligible_days=elig,
                       window_days=pos)
        legacy = round(pos / elig * min(1.0, pos / 5), 3)
        assert out.accepted and out.confidence == legacy


def test_repeats_on_few_days_are_rejected(sc):
    out = _routine(sc, observations=12, positive_days=1, eligible_days=1, window_days=1)
    assert not out.accepted and out.reason == sc.R_TOO_FEW_DAYS and out.confidence == 0.0
    assert _routine(sc, observations=4).reason == sc.R_TOO_FEW_OBSERVATIONS


def test_low_coverage_is_rejected(sc):
    assert _routine(sc, positive_days=5, eligible_days=29).reason == sc.R_LOW_COVERAGE


def test_recency(sc):
    assert sc.recency_factor(0) == sc.recency_factor(sc.RECENT_DAYS) == 1.0
    assert sc.recency_factor(sc.STALE_DAYS) == 0.0
    mid = (sc.RECENT_DAYS + sc.STALE_DAYS) / 2
    assert sc.recency_factor(mid) == pytest.approx(0.5)
    assert _routine(sc, days_since_last=sc.STALE_DAYS).reason == sc.R_STALE
    assert _routine(sc, days_since_last=10).confidence < 1.0


def test_spread_timing_lowers_confidence(sc):
    clean = _routine(sc, window_days=20).confidence
    spread = _routine(sc, positive_days=10, observations=10, eligible_days=20,
                      window_days=30).confidence
    assert clean == 1.0 and spread < 0.5 * 0.5


def test_mostly_automation_caused_is_rejected(sc):
    assert _routine(sc, automated_observations=20).reason == sc.R_AUTOMATION_CAUSED
    assert _routine(sc, automated_observations=5).accepted


def test_sequence_needs_conditional_support(sc):
    ev = sc.SequenceEvidence
    strong = sc.score_sequence(ev(support=15, trigger_count=16, distinct_days=12),
                               min_occurrences=5)
    assert strong.accepted and strong.confidence == 1.0
    busy = sc.score_sequence(ev(support=30, trigger_count=500, distinct_days=12),
                             min_occurrences=5)
    assert not busy.accepted and busy.reason == sc.R_WEAK_ASSOCIATION
    one_day = sc.score_sequence(ev(support=10, trigger_count=10, distinct_days=1),
                                min_occurrences=5)
    assert one_day.reason == sc.R_TOO_FEW_DAYS


def test_scores_are_deterministic_and_explain_themselves(sc):
    a = _routine(sc, positive_days=14, observations=14, days_since_last=9)
    assert a == _routine(sc, positive_days=14, observations=14, days_since_last=9)
    ev = a.evidence()
    assert ev["result"] == "accepted" and ev["distinct_days"] == 14
    assert list(ev)[:2] == ["score", "result"]


_SCHEMA = """
CREATE TABLE state_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
  entity_id TEXT NOT NULL, domain TEXT NOT NULL, old_state TEXT, new_state TEXT NOT NULL,
  area_id TEXT, hour INTEGER, day_of_week INTEGER, triggered_by TEXT DEFAULT 'system',
  person TEXT DEFAULT 'unknown');
"""


def _conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.row_factory = sqlite3.Row
    now = datetime.now().replace(second=0, microsecond=0)
    for eid, state, days, hour, minute, source in rows:
        for d in days:
            dt = (now - timedelta(days=d)).replace(hour=hour, minute=minute)
            conn.execute("INSERT INTO state_changes (timestamp, entity_id, domain, old_state, "
                         "new_state, area_id, hour, day_of_week, triggered_by) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (dt.isoformat(), eid, eid.split(".")[0], "off", state, "", hour,
                          dt.weekday(), source))
    return conn


def test_detected_routines_carry_their_evidence(load):
    pa = load("automation.patterns")
    conn = _conn([("light.porch", "on", range(1, 11), 18, 0, "user"),
                  ("light.porch", "on", range(1, 4), 18, 30, "automation")])
    found = pa.PatternAnalyzer()._find_time_routines(conn)
    assert len(found) == 1
    ev = found[0].details["evidence"]
    assert ev["distinct_days"] == 10 and ev["automated_share"] == round(3 / 13, 3)
    assert found[0].occurrences == 10          # automation rows stay excluded


def test_a_chattering_trigger_does_not_multiply_support(load):
    pa = load("automation.patterns")
    rows = [("binary_sensor.hall_motion", "on", [d], 20, m, "user")
            for d in range(1, 9) for m in (0, 1, 2, 3, 4)]
    rows += [("light.hall", "on", range(1, 9), 20, 6, "user")]
    found = pa.PatternAnalyzer()._find_sequence_patterns(_conn(rows))
    motion = [p for p in found if p.entity_ids == ["binary_sensor.hall_motion", "light.hall"]]
    assert motion == []          # 8 follow-ups out of 40 triggers: association only
    rows = [("switch.hall_button", "on", range(1, 9), 20, 0, "user"),
            ("light.hall", "on", range(1, 9), 20, 1, "user")]
    found = pa.PatternAnalyzer()._find_sequence_patterns(_conn(rows))
    assert [p.occurrences for p in found if p.entity_ids[0] == "switch.hall_button"] == [8]
