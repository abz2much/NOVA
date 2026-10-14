"""The decision record, checked against its spec (7d).

One row per proactive decision, written once and never changed except for a
single outcome. Reads summarise confirmed outcomes for learning. Every entry
point is fail soft: a broken database gives the documented empty answer.
"""
import sqlite3

import pytest


@pytest.fixture
def dr(load, tmp_path, monkeypatch):
    mod = load("decision_record")
    monkeypatch.setattr(mod, "_DEFAULT_DB", str(tmp_path / "decisions.db"))
    return mod


def _rows(dr, sql):
    conn = sqlite3.connect(dr._default_db())
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


# ── Writing ─────────────────────────────────────────────────────────────────

def test_a_record_keeps_its_parts_apart_and_reads_back(dr):
    rid = dr.record("anticipation_overdue", observation={"door": "open"},
                    interpretation={"meaning": "left open"}, evidence={"minutes": 20},
                    decision="announce", reason="long open", model="m1", tokens="42",
                    latency_ms=12.9, confidence="0.75", ref="suggestion:3", ts=100.0)
    row = dr.get(rid)
    assert row["kind"] == "anticipation_overdue"
    assert row["observation"] == {"door": "open"}
    assert row["interpretation"] == {"meaning": "left open"}
    assert row["evidence"] == {"minutes": 20}
    assert (row["decision"], row["reason"], row["model"]) == ("announce", "long open", "m1")
    assert (row["tokens"], row["latency_ms"], row["confidence"]) == (42, 12, 0.75)
    assert (row["ref"], row["ts"], row["outcome"]) == ("suggestion:3", 100.0, None)


def test_missing_parts_are_stored_as_empty(dr, monkeypatch):
    monkeypatch.setattr(dr.time, "time", lambda: 555.0)
    row = dr.get(dr.record("k"))
    assert row["observation"] == row["interpretation"] == row["evidence"] == {}
    assert (row["decision"], row["reason"]) == ("", "")
    assert row["model"] is None and row["tokens"] is None and row["ref"] is None
    assert row["ts"] == 555.0


def test_numbers_that_cannot_be_read_become_null(dr):
    row = dr.get(dr.record("k", tokens="many", latency_ms=object(), confidence="sure"))
    assert row["tokens"] is None and row["latency_ms"] is None and row["confidence"] is None


def test_json_parts_fall_back_to_text_or_empty(dr):
    loop: dict = {}
    loop["self"] = loop
    rid = dr.record("k", observation={"when": object()}, evidence=loop)
    row = dr.get(rid)
    assert isinstance(row["observation"]["when"], str)
    assert row["evidence"] == {}


def test_json_parts_are_stored_with_sorted_keys(dr):
    rid = dr.record("k", observation={"b": 1, "a": 2})
    assert _rows(dr, f"SELECT observation FROM decision_records WHERE id={rid}") == [
        ('{"a": 2, "b": 1}',)]


def test_a_part_that_will_not_parse_reads_as_empty(dr):
    rid = dr.record("k", observation={"a": 1})
    conn = sqlite3.connect(dr._default_db())
    conn.execute("UPDATE decision_records SET observation='not json' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    assert dr.get(rid)["observation"] == {}


def test_a_database_that_cannot_open_never_raises(dr, tmp_path, monkeypatch):
    bad = str(tmp_path / "missing_dir" / "x" / "decisions.db")

    def _boom(*a, **k):
        raise sqlite3.OperationalError("cannot open")
    monkeypatch.setattr(dr, "_connect", _boom)
    assert dr.record("k", db_path=bad) is None
    assert dr.set_outcome(1, "good", db_path=bad) is False
    assert dr.set_outcome_checked(1, "good", db_path=bad) == "not_found"
    assert dr.set_outcome_by_ref("r", "good", db_path=bad) is False
    assert dr.set_outcome_recent("k", "good", db_path=bad) is False
    assert dr.get(1, db_path=bad) is None
    assert dr.recent(db_path=bad) == []
    assert dr.page(db_path=bad) == {"items": [], "next_cursor": None}
    dr.ensure_schema(db_path=bad)


def test_ensure_schema_creates_the_table(dr):
    dr.ensure_schema()
    assert _rows(dr, "SELECT COUNT(*) FROM decision_records") == [(0,)]


# ── The one outcome ─────────────────────────────────────────────────────────

def test_an_outcome_is_set_once_and_then_kept(dr):
    rid = dr.record("k")
    assert dr.set_outcome(rid, "good", source="phone", ts=7.0) is True
    assert dr.set_outcome(rid, "wrong", source="phone") is False
    row = dr.get(rid)
    assert (row["outcome"], row["outcome_source"], row["outcome_ts"]) == ("good", "phone", 7.0)


def test_no_id_and_an_unknown_id_set_nothing(dr):
    assert dr.set_outcome(None, "good") is False
    assert dr.set_outcome(999, "good") is False


def test_the_checked_setter_says_why(dr):
    rid = dr.record("k")
    assert dr.set_outcome_checked(None, "good") == "not_found"
    assert dr.set_outcome_checked(999, "good") == "not_found"
    assert dr.set_outcome_checked(rid, "good") == "ok"
    assert dr.set_outcome_checked(rid, "wrong") == "already_judged"


def test_by_ref_judges_the_newest_unjudged_record(dr):
    older = dr.record("k", ref="suggestion:1", ts=1.0)
    newer = dr.record("k", ref="suggestion:1", ts=2.0)
    assert dr.set_outcome_by_ref("suggestion:1", "good") is True
    assert dr.get(newer)["outcome"] == "good" and dr.get(older)["outcome"] is None
    assert dr.set_outcome_by_ref("suggestion:1", "wrong") is True
    assert dr.get(older)["outcome"] == "wrong"
    assert dr.set_outcome_by_ref("suggestion:1", "good") is False
    assert dr.set_outcome_by_ref("", "good") is False


def test_recent_kind_judges_only_within_the_age(dr, monkeypatch):
    monkeypatch.setattr(dr.time, "time", lambda: 10_000.0)
    stale = dr.record("intrusion", ts=10_000.0 - 4000)
    assert dr.set_outcome_recent("intrusion", "wrong") is False
    fresh = dr.record("intrusion", ts=10_000.0 - 10)
    dr.record("other", ts=10_000.0)
    assert dr.set_outcome_recent("intrusion", "wrong", source="dismiss_intrusion") is True
    assert dr.get(fresh)["outcome"] == "wrong" and dr.get(stale)["outcome"] is None
    assert dr.set_outcome_recent("intrusion", "wrong", max_age=5000) is True
    assert dr.get(stale)["outcome"] == "wrong"


# ── Reading ─────────────────────────────────────────────────────────────────

def test_recent_is_newest_first_with_filters(dr):
    a = dr.record("x", ts=1.0)
    b = dr.record("y", ts=2.0)
    c = dr.record("x", ts=3.0)
    dr.set_outcome(c, "good")
    assert [r["id"] for r in dr.recent()] == [c, b, a]
    assert [r["id"] for r in dr.recent(kind="x")] == [c, a]
    assert [r["id"] for r in dr.recent(only_unjudged=True)] == [b, a]
    assert [r["id"] for r in dr.recent(limit=1)] == [c]
    assert dr.recent()[0]["observation"] == {}


def test_a_page_has_summary_columns_and_a_cursor(dr):
    ids = [dr.record("x", ts=5.0, observation={"big": 1}) for _ in range(3)]
    first = dr.page(limit=2)
    assert [r["id"] for r in first["items"]] == [ids[2], ids[1]]
    assert set(first["items"][0]) == {"id", "ts", "kind", "decision", "reason", "confidence",
                                      "model", "tokens", "latency_ms", "outcome"}
    assert first["next_cursor"] == {"ts": 5.0, "id": ids[1]}
    rest = dr.page(limit=2, cursor_ts=5.0, cursor_id=ids[1])
    assert [r["id"] for r in rest["items"]] == [ids[0]] and rest["next_cursor"] is None


def test_a_half_cursor_is_ignored_and_the_limit_is_at_least_one(dr):
    a = dr.record("x", ts=1.0)
    b = dr.record("y", ts=2.0)
    assert [r["id"] for r in dr.page(cursor_ts=1.5)["items"]] == [b, a]
    assert [r["id"] for r in dr.page(limit=0)["items"]] == [b]
    dr.set_outcome(b, "good")
    assert [r["id"] for r in dr.page(kind="y")["items"]] == [b]
    assert [r["id"] for r in dr.page(only_unjudged=True)["items"]] == [a]


def test_stats_count_outcomes_and_never_create_the_store(dr, tmp_path):
    missing = str(tmp_path / "none.db")
    assert dr.stats(db_path=missing) == {"total": 0, "judged": 0, "good": 0,
                                         "unnecessary": 0, "wrong": 0}
    assert not (tmp_path / "none.db").exists()
    for verdict in ("good", "good", "wrong", None):
        rid = dr.record("k")
        if verdict:
            dr.set_outcome(rid, verdict)
    assert dr.stats() == {"total": 4, "judged": 3, "good": 2, "unnecessary": 0, "wrong": 1}


# ── Calibration ─────────────────────────────────────────────────────────────

def test_calibration_with_no_store_or_no_judged_rows(dr, tmp_path):
    empty = {"n": 0, "good_rate": None, "brier": None, "ece": None, "bins": [], "kind": None}
    assert dr.calibration(db_path=str(tmp_path / "none.db")) == empty
    dr.record("k", confidence=0.5)
    assert dr.calibration() == empty


def test_calibration_scores_confidence_against_outcomes(dr):
    for conf, verdict in ((0.9, "good"), (0.9, "wrong"), (0.1, "unnecessary"),
                          (1.7, "good"), (-3, "wrong")):
        dr.set_outcome(dr.record("k", confidence=conf), verdict)
    dr.record("k")                                   # unjudged: left out
    dr.set_outcome(dr.record("k"), "good")           # no confidence: left out
    out = dr.calibration(bins=2)
    assert out["n"] == 5
    assert out["good_rate"] == 0.4
    # confidences clamp to 1.0 and 0.0
    assert out["brier"] == round(((0.1 ** 2) + 0.81 + 0.01 + 0 + 0) / 5, 4)
    assert out["bins"] == [
        {"lo": 0.0, "hi": 0.5, "count": 2, "mean_confidence": 0.05, "good_rate": 0.0},
        {"lo": 0.5, "hi": 1.0, "count": 3, "mean_confidence": 0.933, "good_rate": 0.667},
    ]
    assert out["ece"] == round(2 / 5 * 0.05 + 3 / 5 * abs(2.8 / 3 - 2 / 3), 4)


def test_calibration_filters_by_kind_and_since_and_keeps_empty_bins(dr):
    dr.set_outcome(dr.record("a", confidence=0.95, ts=10.0), "good")
    dr.set_outcome(dr.record("b", confidence=0.95, ts=10.0), "good")
    dr.set_outcome(dr.record("a", confidence=0.95, ts=1.0), "good")
    out = dr.calibration(kind="a", since=5.0, bins=0)
    assert out["kind"] == "a" and out["n"] == 1
    assert len(out["bins"]) == 1 and out["bins"][0]["count"] == 1
    many = dr.calibration(bins=4)
    assert [b["count"] for b in many["bins"]] == [0, 0, 0, 3]
    assert many["bins"][0]["mean_confidence"] is None


# ── Learning from confirmed outcomes ────────────────────────────────────────

def _judge(dr, verdict, source="phone", ts=None):
    dr.set_outcome(dr.record("k", ts=ts), verdict, source=source)


def test_the_interruption_budget_reads_confirmed_outcomes_only(dr):
    assert dr.interruption_budget()["assessment"] == "no data"
    _judge(dr, "wrong", source="auto")
    assert dr.interruption_budget()["judged"] == 0
    for verdict in ("good", "unnecessary", "wrong", "wrong"):
        _judge(dr, verdict)
    out = dr.interruption_budget(floor=0.25)
    assert (out["judged"], out["good"], out["unnecessary"], out["wrong"]) == (4, 1, 1, 2)
    assert out["unwelcome_rate"] == 0.75
    assert out["multiplier"] == round(max(0.25, 1 - 0.75 * 0.75), 3)
    assert out["assessment"] == "over-interrupting"


@pytest.mark.parametrize("good, bad, assessment", [(3, 1, "borderline"), (9, 1, "healthy")])
def test_the_budget_assessment_bands(dr, good, bad, assessment):
    for _ in range(good):
        _judge(dr, "good")
    for _ in range(bad):
        _judge(dr, "unnecessary", source="panel_label")
    assert dr.interruption_budget()["assessment"] == assessment


def test_the_budget_window_and_floor_clamp(dr, monkeypatch):
    monkeypatch.setattr(dr.time, "time", lambda: 100_000.0)
    _judge(dr, "wrong", ts=1.0)
    assert dr.interruption_budget()["judged"] == 0
    _judge(dr, "wrong", ts=99_999.0)
    assert dr.interruption_budget(floor=5)["multiplier"] == 1.0
    assert dr.interruption_budget(floor=-1)["multiplier"] == 0.0


def test_the_budget_without_a_store(dr, tmp_path):
    out = dr.interruption_budget(db_path=str(tmp_path / "none.db"), window_s=60)
    assert out == {"window_s": 60, "judged": 0, "good": 0, "unnecessary": 0, "wrong": 0,
                   "unwelcome_rate": None, "multiplier": 1.0, "assessment": "no data"}


@pytest.mark.parametrize("rate, delta", [(None, -0.07), (0.0, -0.07), (0.1, -0.07),
                                         (0.2, 0.0), (0.3, 0.07), (0.49, 0.07),
                                         (0.5, 0.15), ("0.6", 0.15), ("bad", 0.0)])
def test_threshold_delta_bands(dr, rate, delta):
    assert dr.threshold_delta_from_rate(rate) == delta


def test_outcome_rate_for_one_kind(dr):
    for verdict, source in (("good", "installed"), ("wrong", "dismiss_suggestion"),
                            ("good", "auto")):
        dr.set_outcome(dr.record("suggestion"), verdict, source=source)
    out = dr.outcome_rate("suggestion")
    assert out == {"kind": "suggestion", "judged": 2, "good": 1, "unnecessary": 0,
                   "wrong": 1, "good_rate": 0.5, "unwelcome_rate": 0.5}


def test_outcome_rate_by_prefix_escapes_wildcards(dr):
    dr.set_outcome(dr.record("anticipation_overdue"), "good", source="phone")
    dr.set_outcome(dr.record("anticipationXoverdue"), "wrong", source="phone")
    dr.set_outcome(dr.record("anticipation"), "good", source="phone")
    assert dr.outcome_rate("anticipation", prefix=True)["judged"] == 3
    assert dr.outcome_rate("anticipation_", prefix=True)["judged"] == 1
    assert dr.outcome_rate("anticipation%", prefix=True)["judged"] == 0


def test_outcome_rate_window_and_no_store(dr, tmp_path, monkeypatch):
    monkeypatch.setattr(dr.time, "time", lambda: 1000.0)
    dr.set_outcome(dr.record("k", ts=10.0), "good", source="phone")
    assert dr.outcome_rate("k", window_s=100)["judged"] == 0
    assert dr.outcome_rate("k")["judged"] == 1
    none = dr.outcome_rate("k", db_path=str(tmp_path / "none.db"))
    assert none["judged"] == 0 and none["good_rate"] is None


def test_confirmed_sources_are_the_five_person_confirmed_ones(dr):
    assert dr.CONFIRMED_SOURCES == frozenset({
        "phone", "dismiss_intrusion", "panel_label", "installed", "dismiss_suggestion"})
    assert (dr.OUTCOME_GOOD, dr.OUTCOME_UNNECESSARY, dr.OUTCOME_WRONG) == (
        "good", "unnecessary", "wrong")
