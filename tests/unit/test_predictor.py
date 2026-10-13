"""Fresh behaviour tests for automation/predictor.py, written from the spec.

All timestamps are built in local time, because the matrix buckets by the
machine's local clock."""
import datetime as dt
import importlib
import importlib.util
import json
import logging
import os
import pathlib
import sys

import pytest

SRC = (pathlib.Path(__file__).resolve().parents[2]
       / "custom_components" / "nova" / "automation" / "predictor.py")


def _standalone():
    spec = importlib.util.spec_from_file_location("fresh_predictor", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fresh_predictor"] = mod
    spec.loader.exec_module(mod)
    return mod


PR = _standalone()


def at(day, hour, minute=0, second=0, month=3, year=2025):
    return dt.datetime(year, month, day, hour, minute, second).timestamp()


@pytest.fixture
def store(tmp_path):
    return tmp_path / "habits" / "matrix.json"


@pytest.fixture
def hm(store):
    return PR.PredictiveHabitMatrix(path=str(store))


def _read(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


# ── interface ────────────────────────────────────────────────────────────────

def test_constants():
    assert PR.DEFAULT_BUCKET_MINUTES == 30
    assert PR.DEFAULT_THRESHOLD == 0.90
    assert PR.DEFAULT_LEAD_MINUTES == 7
    assert PR.MAX_EVENTS == 5000
    assert PR.DEFAULT_MATRIX_PATH is None


def test_constructor_defaults_and_no_disk_access(store):
    m = PR.PredictiveHabitMatrix(path=str(store))
    assert (m.path, m.bucket_minutes, m.max_events) == (str(store), 30, 5000)
    assert not store.parent.exists()


@pytest.mark.parametrize("given, kept", [(0, 1), (-15, 1), ("45", 45), (12.9, 12)])
def test_bucket_minutes_is_clamped_to_an_int_of_at_least_one(store, given, kept):
    assert PR.PredictiveHabitMatrix(path=str(store), bucket_minutes=given).bucket_minutes == kept


def test_default_path_via_paths(load, monkeypatch, tmp_path):
    mod = load("automation.predictor")
    paths = load("paths")
    monkeypatch.setattr(mod, "DEFAULT_MATRIX_PATH", None)
    monkeypatch.setattr(paths, "_root", str(tmp_path))
    assert mod.PredictiveHabitMatrix().path == os.path.join(
        str(tmp_path), "nova", "habit_matrix.json")
    monkeypatch.setattr(mod, "DEFAULT_MATRIX_PATH", str(tmp_path / "elsewhere.json"))
    assert mod._default_matrix_path() == str(tmp_path / "elsewhere.json")
    assert mod.PredictiveHabitMatrix().path == str(tmp_path / "elsewhere.json")


def test_standalone_default_path_raises_import_error(monkeypatch, tmp_path):
    monkeypatch.setattr(PR, "DEFAULT_MATRIX_PATH", str(tmp_path / "x.json"))
    with pytest.raises(ImportError):
        PR.PredictiveHabitMatrix()


def test_package_exports_the_class(load):
    mod = load("automation.predictor")
    pkg = importlib.import_module("jc.automation")
    assert pkg.PredictiveHabitMatrix is mod.PredictiveHabitMatrix
    assert "PredictiveHabitMatrix" in pkg.__all__


# ── time helpers ─────────────────────────────────────────────────────────────

def test_day_key_is_local_iso_date():
    assert PR._day_key(at(9, 23, 59)) == "2025-03-09"
    assert PR._day_key(at(10, 0, 0)) == "2025-03-10"


@pytest.mark.parametrize("hour, minute, second, slot", [
    (0, 0, 0, 0), (8, 0, 0, 16), (8, 29, 59, 16), (8, 30, 0, 17), (23, 59, 59, 47)])
def test_slot_with_thirty_minute_buckets(hm, hour, minute, second, slot):
    assert hm._slot(at(5, hour, minute, second)) == slot


def test_slot_with_custom_bucket(store):
    m = PR.PredictiveHabitMatrix(path=str(store), bucket_minutes=45)
    assert m._slot(at(5, 1, 29)) == 1      # 89 // 45
    assert m._slot(at(5, 1, 30)) == 2      # 90 // 45


# ── record_event ─────────────────────────────────────────────────────────────

def test_record_returns_and_stores_the_event(hm, store):
    ts = at(4, 7, 40)
    ev = hm.record_event("hallway_entry", ts)
    assert ev == {"key": "hallway_entry", "ts": float(ts), "slot": 15, "day": "2025-03-04"}
    assert _read(store) == [ev]


def test_record_coerces_key_and_ts(hm):
    ev = hm.record_event(42, str(at(4, 7, 40)))
    assert ev["key"] == "42"
    assert isinstance(ev["ts"], float)


def test_record_defaults_to_now(hm, monkeypatch):
    monkeypatch.setattr(PR.time, "time", lambda: at(6, 18, 5))
    ev = hm.record_event("lamp_on")
    assert ev["ts"] == at(6, 18, 5) and ev["slot"] == 36 and ev["day"] == "2025-03-06"


def test_record_appends_in_order(hm, store):
    for i, key in enumerate(("a", "b", "c")):
        hm.record_event(key, at(1 + i, 9))
    assert [e["key"] for e in _read(store)] == ["a", "b", "c"]
    assert not pathlib.Path(f"{store}.tmp").exists()


def test_cap_keeps_newest(store):
    m = PR.PredictiveHabitMatrix(path=str(store), max_events=4)
    for i in range(10):
        m.record_event(f"k{i}", at(1, 9, i))
    assert [e["key"] for e in _read(store)] == ["k6", "k7", "k8", "k9"]


def test_cap_of_zero_never_trims(store):
    m = PR.PredictiveHabitMatrix(path=str(store), max_events=0)
    for i in range(3):
        m.record_event("k", at(1, 9, i))
    assert len(_read(store)) == 3


def test_utf8_without_escapes(hm, store):
    hm.record_event("salle_de_séjour_entry", at(2, 9))
    raw = store.read_text(encoding="utf-8")
    assert "séjour" in raw and "\\u" not in raw


def test_save_failure_logs_error(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    m = PR.PredictiveHabitMatrix(path=str(blocker / "sub" / "m.json"))
    with caplog.at_level(logging.ERROR):
        ev = m.record_event("k", at(1, 9))
    assert ev["key"] == "k"
    assert any("Failed to persist habit matrix" in r.getMessage() for r in caplog.records)


# ── loading tolerance ────────────────────────────────────────────────────────

def test_missing_file_is_empty_without_logs(hm, caplog):
    with caplog.at_level(logging.DEBUG):
        assert hm.observed_days() == 0
        assert hm.probability("x", at(1, 9)) == 0.0
        assert hm.due_preemptions(at(1, 9)) == []
    assert not caplog.records


def test_bad_json_warns_then_is_replaced(store, caplog):
    store.parent.mkdir(parents=True)
    store.write_text("[{oops", encoding="utf-8")
    m = PR.PredictiveHabitMatrix(path=str(store))
    with caplog.at_level(logging.WARNING):
        assert m.observed_days() == 0
    assert any("habit matrix unreadable" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)
    m.record_event("k", at(1, 9))
    assert len(_read(store)) == 1


@pytest.mark.parametrize("payload", ['{"key": "k"}', "7", "null"])
def test_non_list_json_is_empty(store, payload):
    store.parent.mkdir(parents=True)
    store.write_text(payload, encoding="utf-8")
    assert PR.PredictiveHabitMatrix(path=str(store)).observed_days() == 0


def test_non_utf8_file_raises(store):
    store.parent.mkdir(parents=True)
    store.write_bytes(b"\xff\xfe\x01")
    with pytest.raises(UnicodeDecodeError):
        PR.PredictiveHabitMatrix(path=str(store)).observed_days()


def test_instances_share_the_file(store):
    PR.PredictiveHabitMatrix(path=str(store)).record_event("k", at(1, 9))
    PR.PredictiveHabitMatrix(path=str(store)).record_event("k", at(2, 9))
    assert PR.PredictiveHabitMatrix(path=str(store)).observed_days() == 2


# ── probability ──────────────────────────────────────────────────────────────

def test_probability_every_day_is_one(hm):
    for d in range(1, 8):
        hm.record_event("study_entry", at(d, 19, 10))
    assert hm.probability("study_entry", at(8, 19, 25)) == 1.0


def test_probability_share_of_all_observed_days(hm):
    for d in range(1, 5):
        hm.record_event("study_entry", at(d, 19, 10))
    for d in range(5, 8):
        hm.record_event("garden_entry", at(d, 6, 0))   # other key, other slot
    assert hm.probability("study_entry", at(8, 19, 0)) == round(4 / 7, 4)


def test_probability_counts_a_day_once(hm):
    for minute in (1, 5, 20):
        hm.record_event("k", at(1, 19, minute))
    hm.record_event("other", at(2, 9))
    assert hm.probability("k", at(3, 19, 0)) == 0.5


def test_probability_other_slot_or_key_is_zero(hm):
    hm.record_event("k", at(1, 19, 10))
    assert hm.probability("k", at(2, 19, 30)) == 0.0
    assert hm.probability("K", at(2, 19, 10)) == 0.0


def test_probability_uses_stored_slot(store):
    coarse = PR.PredictiveHabitMatrix(path=str(store), bucket_minutes=60)
    coarse.record_event("k", at(1, 8, 45))           # stored slot 8
    fine = PR.PredictiveHabitMatrix(path=str(store))  # 30-min buckets
    assert fine.probability("k", at(2, 8, 45)) == 0.0  # slot 17 != 8
    assert fine.probability("k", at(2, 4, 0)) == 1.0   # slot 8 == stored 8


def test_probability_ignores_rows_without_day(store):
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps([{"key": "k", "slot": 18}]), encoding="utf-8")
    assert PR.PredictiveHabitMatrix(path=str(store)).probability("k", at(1, 9)) == 0.0


def test_probability_rounded_to_four_places(hm):
    hm.record_event("k", at(1, 9))
    for d in (2, 3):
        hm.record_event("x", at(d, 9))
    assert hm.probability("k", at(4, 9)) == 0.3333


# ── should_preempt ───────────────────────────────────────────────────────────

def test_should_preempt_threshold_is_inclusive(hm):
    for d in range(1, 10):
        hm.record_event("k", at(d, 9))
    hm.record_event("x", at(10, 15))
    assert hm.probability("k", at(11, 9)) == 0.9
    assert hm.should_preempt("k", at(11, 9)) is True
    assert hm.should_preempt("k", at(11, 9), threshold=0.91) is False
    assert hm.should_preempt("k", at(11, 15), threshold=0.0) is True


# ── due_preemptions ──────────────────────────────────────────────────────────

def test_due_looks_ahead_by_lead_minutes(hm):
    for d in range(1, 6):
        hm.record_event("porch_light_on", at(d, 18, 35))
    now = at(6, 18, 25)                                   # 18:32 lands in 18:30 bucket
    assert hm.due_preemptions(now) == [
        {"key": "porch_light_on", "probability": 1.0, "lead_minutes": 7}]
    assert hm.due_preemptions(now, lead_minutes=1) == []  # 18:26 is the 18:00 bucket


def test_due_sorted_highest_first_and_filtered(hm):
    for d in range(1, 11):
        hm.record_event("a", at(d, 7, 10))
        if d <= 9:
            hm.record_event("b", at(d, 7, 15))
        if d <= 5:
            hm.record_event("c", at(d, 7, 20))
    due = hm.due_preemptions(at(11, 7, 0), lead_minutes=5, threshold=0.9)
    assert [(i["key"], i["probability"]) for i in due] == [("a", 1.0), ("b", 0.9)]
    assert all(i["lead_minutes"] == 5 for i in due)


def test_due_defaults_to_now(hm, monkeypatch):
    for d in range(1, 4):
        hm.record_event("k", at(d, 12, 10))
    monkeypatch.setattr(PR.time, "time", lambda: at(4, 12, 0))
    assert [i["key"] for i in hm.due_preemptions()] == ["k"]


def test_due_skips_rows_without_key(store):
    store.parent.mkdir(parents=True)
    store.write_text(json.dumps([{"key": "", "slot": 24, "day": "2025-03-01"}]),
                     encoding="utf-8")
    m = PR.PredictiveHabitMatrix(path=str(store))
    assert m.due_preemptions(at(2, 12), threshold=0.0) == []


# ── observed_days ────────────────────────────────────────────────────────────

def test_observed_days_counts_distinct_days(hm):
    hm.record_event("a", at(1, 8))
    hm.record_event("b", at(1, 20))
    hm.record_event("a", at(3, 8))
    assert hm.observed_days() == 2
