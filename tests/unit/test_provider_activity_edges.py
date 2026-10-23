"""Provider activity: the type handling, failure paths, averages and the
database path helpers that the main activity tests leave open."""
import sqlite3
import types
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def pa(load):
    return load("provider_activity")


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "activity.db")


def _rec(pa, db, **over):
    args = dict(provider="groq", model="m", role="conversation", location="cloud",
                data_category="household", success=True, db_path=db)
    args.update(over)
    return pa.record(**args)


def _raw(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM provider_activity_daily")]
    finally:
        conn.close()


# ── what record stores ───────────────────────────────────────────────────────

def test_a_first_call_makes_one_row_with_these_values(pa, db):
    assert _rec(pa, db, input_tokens=10, output_tokens=4, latency_ms=250, day="2026-03-01")
    (row,) = _raw(db)
    assert (row["day"], row["provider"], row["model"], row["role"], row["location"],
            row["data_category"]) == ("2026-03-01", "groq", "m", "conversation", "cloud",
                                      "household")
    assert (row["success_count"], row["failure_count"], row["call_count"]) == (1, 0, 1)
    assert (row["input_tokens"], row["output_tokens"], row["latency_ms_sum"]) == (10, 4, 250)


def test_a_failed_call_counts_as_a_failure(pa, db):
    _rec(pa, db, success=False)
    (row,) = _raw(db)
    assert (row["success_count"], row["failure_count"]) == (0, 1)


@pytest.mark.parametrize("success", [1, "yes", [1]])
def test_anything_truthy_counts_as_a_success(pa, db, success):
    _rec(pa, db, success=success)
    assert _raw(db)[0]["success_count"] == 1


@pytest.mark.parametrize("success", [0, None, "", []])
def test_anything_falsy_counts_as_a_failure(pa, db, success):
    _rec(pa, db, success=success)
    assert _raw(db)[0]["failure_count"] == 1


def test_names_are_stored_as_text_and_latency_as_a_whole_number(pa, db):
    _rec(pa, db, provider=5, model=None, role=1.5, latency_ms="42")
    (row,) = _raw(db)
    assert (row["provider"], row["model"], row["role"]) == ("5", "None", "1.5")
    assert row["latency_ms_sum"] == 42


def test_the_day_defaults_to_today(pa, db):
    _rec(pa, db)
    assert _raw(db)[0]["day"] == datetime.now().strftime("%Y-%m-%d")
    assert pa._today() == datetime.now().strftime("%Y-%m-%d")


def test_a_bad_latency_is_a_quiet_false_and_writes_nothing(pa, db):
    assert _rec(pa, db, latency_ms="slow") is False
    assert _raw(db) == []


def test_token_sums_start_empty_then_add_up_and_never_go_back_to_empty(pa, db):
    _rec(pa, db, day="d")
    assert _raw(db)[0]["input_tokens"] is None
    _rec(pa, db, day="d", input_tokens=5, output_tokens=2)
    _rec(pa, db, day="d")
    (row,) = _raw(db)
    assert (row["input_tokens"], row["output_tokens"], row["call_count"]) == (5, 2, 3)


def test_the_default_database_is_used_when_none_is_given(pa, db, monkeypatch):
    monkeypatch.setattr(pa, "_DEFAULT_DB", db)
    assert pa.record("g", "m", "r", "cloud", "c", True) is True
    assert len(_raw(db)) == 1
    assert pa._resolve(None) == db and pa._resolve("/x.db") == "/x.db"


def test_the_default_path_is_the_nova_provider_activity_file(pa, load, monkeypatch):
    monkeypatch.setattr(pa, "_DEFAULT_DB", None)
    assert pa._default_db() == load("paths").nova_path("provider_activity.db")


def test_record_is_false_when_the_database_cannot_open(pa, db, monkeypatch):
    def boom(path):
        raise sqlite3.OperationalError("cannot open")
    monkeypatch.setattr(pa, "_connect", boom)
    assert _rec(pa, db) is False


def test_the_database_is_set_up_in_wal_mode(pa, db):
    _rec(pa, db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


# ── list_days ────────────────────────────────────────────────────────────────

def test_an_entry_shows_counts_and_averages(pa, db):
    day = datetime.now().strftime("%Y-%m-%d")
    _rec(pa, db, input_tokens=10, output_tokens=4, latency_ms=100)
    _rec(pa, db, success=False, input_tokens=5, output_tokens=1, latency_ms=201)
    (entry_day,) = pa.list_days(db_path=db)
    assert entry_day["day"] == day
    (entry,) = entry_day["entries"]
    assert entry == {
        "provider": "groq", "model": "m", "role": "conversation", "location": "cloud",
        "data_category": "household", "success_count": 1, "failure_count": 1,
        "call_count": 2, "avg_input_tokens": 7.5, "avg_output_tokens": 2.5,
        "avg_latency_ms": 150.5}
    assert list(entry) == ["provider", "model", "role", "location", "data_category",
                           "success_count", "failure_count", "call_count",
                           "avg_input_tokens", "avg_output_tokens", "avg_latency_ms"]


def test_averages_are_none_when_no_call_reported_tokens(pa, db):
    _rec(pa, db, latency_ms=30)
    (entry,) = pa.list_days(db_path=db)[0]["entries"]
    assert entry["avg_input_tokens"] is None and entry["avg_output_tokens"] is None
    assert entry["avg_latency_ms"] == 30.0


def test_the_window_counts_today_as_day_one(pa, db):
    today = datetime.now()
    for back in (0, 1, 2, 3):
        _rec(pa, db, day=(today - timedelta(days=back)).strftime("%Y-%m-%d"))
    assert len(pa.list_days(1, db_path=db)) == 1
    assert len(pa.list_days(2, db_path=db)) == 2
    assert len(pa.list_days(4, db_path=db)) == 4
    assert len(pa.list_days(0, db_path=db)) == 1
    assert len(pa.list_days(-5, db_path=db)) == 1
    assert len(pa.list_days(db_path=db)) == 4


def test_days_come_newest_first_and_buckets_group_under_their_day(pa, db):
    today = datetime.now()
    d0 = today.strftime("%Y-%m-%d")
    d1 = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    _rec(pa, db, day=d1)
    _rec(pa, db, day=d0)
    _rec(pa, db, day=d0, model="other")
    out = pa.list_days(db_path=db)
    assert [d["day"] for d in out] == [d0, d1]
    assert len(out[0]["entries"]) == 2 and len(out[1]["entries"]) == 1


def test_a_missing_file_lists_nothing_and_is_not_created(pa, tmp_path):
    path = tmp_path / "absent.db"
    assert pa.list_days(db_path=str(path)) == []
    assert not path.exists()


def test_an_unopenable_database_lists_nothing(pa, db, monkeypatch):
    open(db, "w").close()

    def boom(path):
        raise sqlite3.OperationalError("cannot open")
    monkeypatch.setattr(pa, "_connect", boom)
    assert pa.list_days(db_path=db) == []


def test_a_bad_day_count_lists_nothing(pa, db):
    _rec(pa, db)
    assert pa.list_days("lots", db_path=db) == []


def test_a_file_that_is_not_a_database_lists_nothing(pa, tmp_path):
    path = tmp_path / "junk.db"
    path.write_text("not sqlite")
    assert pa.list_days(db_path=str(path)) == []


# ── the per instance path ────────────────────────────────────────────────────

def test_the_path_is_under_the_config_directory(pa):
    hass = types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *parts: "/config/" + "/".join(parts)))
    assert pa.db_path_for(hass) == "/config/nova/provider_activity.db"


@pytest.mark.parametrize("hass", [
    object(), types.SimpleNamespace(), types.SimpleNamespace(config=None),
    types.SimpleNamespace(config=types.SimpleNamespace(path="/config")),
    types.SimpleNamespace(config=types.SimpleNamespace(
        path=lambda *p: (_ for _ in ()).throw(RuntimeError("no config dir"))))])
def test_no_config_directory_means_no_path(pa, hass):
    assert pa.db_path_for(hass) is None
