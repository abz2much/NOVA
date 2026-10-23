"""The per-person routine store (person_patterns, v6.85.0): saving a routine
for a household member, reading them back, and failing quietly."""
import json
import logging
import sqlite3
from datetime import datetime

import pytest


@pytest.fixture
def pp(load):
    load("identity")            # so person names are normalised
    return load("person_patterns")


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "p.db")


def _rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM person_patterns ORDER BY id")]
    finally:
        conn.close()


# ── saving ───────────────────────────────────────────────────────────────────

def test_a_saved_routine_reads_back(pp, db):
    assert pp.store("username", "time_routine", "start the coffee",
                    data={"hour": 6}, confidence=0.8, occurrences=7, db_path=db) is True
    (row,) = pp.read(db_path=db)
    assert row["person"] == "username" and row["description"] == "start the coffee"
    assert row["pattern_type"] == "time_routine"
    assert row["confidence"] == 0.8 and row["occurrences"] == 7
    assert json.loads(row["data"]) == {"hour": 6}
    assert row["routine_key"] is None
    datetime.fromisoformat(row["last_seen"])


def test_the_defaults_are_no_data_zero_confidence_and_one_occurrence(pp, db):
    pp.store("username", "t", "d", db_path=db)
    (row,) = _rows(db)
    assert (row["data"], row["confidence"], row["occurrences"]) == ("{}", 0.0, 1)


def test_the_same_description_is_refreshed_in_place(pp, db):
    pp.store("username", "time_routine", "start the coffee", confidence=0.5, occurrences=5,
             data={"a": 1}, db_path=db)
    pp.store("username", "time_routine", "start the coffee", confidence=0.9, occurrences=9,
             data={"b": 2}, db_path=db)
    rows = pp.read("username", db_path=db)
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.9 and rows[0]["occurrences"] == 9
    assert json.loads(rows[0]["data"]) == {"b": 2}


def test_a_different_description_or_type_is_a_new_routine(pp, db):
    pp.store("username", "time_routine", "a", db_path=db)
    pp.store("username", "time_routine", "b", db_path=db)
    pp.store("username", "sequence", "a", db_path=db)
    assert len(pp.read("username", db_path=db)) == 3


def test_a_key_finds_the_routine_even_when_the_wording_changes(pp, db):
    pp.store("username", "time_routine", "coffee at 06:40", key="coffee", db_path=db)
    pp.store("username", "time_routine", "coffee at 06:45", key="coffee",
             confidence=0.7, db_path=db)
    (row,) = _rows(db)
    assert row["description"] == "coffee at 06:45" and row["routine_key"] == "coffee"
    assert row["confidence"] == 0.7


def test_a_key_is_scoped_to_the_person_and_type(pp, db):
    pp.store("username", "time_routine", "x", key="k", db_path=db)
    pp.store("other", "time_routine", "x", key="k", db_path=db)
    pp.store("username", "sequence", "x", key="k", db_path=db)
    pp.store("username", "time_routine", "x", key="k2", db_path=db)
    assert len(_rows(db)) == 4


def test_a_routine_stored_without_a_key_is_not_matched_by_a_key(pp, db):
    pp.store("username", "time_routine", "x", db_path=db)
    pp.store("username", "time_routine", "x", key="k", db_path=db)
    assert len(_rows(db)) == 2


def test_duplicates_for_one_key_collapse_to_one_row_keeping_the_newest_id(pp, db):
    pp.store("username", "time_routine", "one", key="k", db_path=db)
    conn = sqlite3.connect(db)
    for text in ("two", "three"):
        conn.execute(
            "INSERT INTO person_patterns (person, pattern_type, description, data, "
            "confidence, last_seen, occurrences, routine_key) VALUES "
            "('username','time_routine',?,'{}',0.1,'2020-01-01',1,'k')", (text,))
    conn.commit()
    conn.close()
    pp.store("username", "time_routine", "fresh", key="k", confidence=0.6, db_path=db)
    (row,) = _rows(db)
    assert row["id"] == 3 and row["description"] == "fresh" and row["confidence"] == 0.6


def test_duplicates_matched_by_description_collapse_too(pp, db):
    pp.store("username", "t", "same", db_path=db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO person_patterns (person, pattern_type, description, data, confidence, "
        "last_seen, occurrences) VALUES ('username','t','same','{}',0.1,'2020-01-01',1)")
    conn.commit()
    conn.close()
    pp.store("username", "t", "same", confidence=0.9, db_path=db)
    (row,) = _rows(db)
    assert row["id"] == 1 and row["confidence"] == 0.9


def test_the_person_name_is_normalised(pp, db):
    pp.store("Username Smith", "time_routine", "x", db_path=db)
    assert pp.read("username_smith", db_path=db)
    assert pp.read("Username Smith", db_path=db)
    assert pp.read(db_path=db)[0]["person"] == "username_smith"


@pytest.mark.parametrize("person", ["", None])
def test_no_person_is_refused_and_nothing_is_created(pp, db, person):
    import os
    assert pp.store(person, "t", "d", db_path=db) is False
    assert not os.path.exists(db)


def test_a_failed_save_is_false_and_noted_at_debug(pp, db, monkeypatch, caplog):
    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")
    monkeypatch.setattr(pp._store, "connect", boom)
    with caplog.at_level(logging.DEBUG, logger=pp._LOGGER.name):
        assert pp.store("username", "t", "d", db_path=db) is False
    messages = [r.getMessage() for r in caplog.records]
    assert "person_patterns store failed: locked" in messages


# ── reading ──────────────────────────────────────────────────────────────────

def test_a_read_can_be_for_one_person(pp, db):
    pp.store("username", "time_routine", "a", db_path=db)
    pp.store("username2", "time_routine", "b", db_path=db)
    assert {r["person"] for r in pp.read("username", db_path=db)} == {"username"}
    assert len(pp.read(db_path=db)) == 2


def test_one_persons_routines_come_best_first(pp, db):
    pp.store("username", "t", "low", confidence=0.2, db_path=db)
    pp.store("username", "t", "high", confidence=0.9, db_path=db)
    pp.store("username", "t", "mid", confidence=0.5, db_path=db)
    assert [r["description"] for r in pp.read("username", db_path=db)] == [
        "high", "mid", "low"]


def test_everyones_routines_come_by_person_then_best_first(pp, db):
    pp.store("zed", "t", "z1", confidence=0.9, db_path=db)
    pp.store("amy", "t", "a-low", confidence=0.1, db_path=db)
    pp.store("amy", "t", "a-high", confidence=0.8, db_path=db)
    assert [r["description"] for r in pp.read(db_path=db)] == ["a-high", "a-low", "z1"]


def test_rows_are_dicts_keyed_by_column(pp, db):
    pp.store("username", "t", "d", db_path=db)
    (row,) = pp.read(db_path=db)
    assert type(row) is dict
    assert set(row) == {"id", "person", "pattern_type", "description", "data",
                        "confidence", "last_seen", "occurrences", "routine_key"}


def test_reading_a_missing_database_gives_nothing(pp, tmp_path):
    assert pp.read(db_path=str(tmp_path / "nope.db")) == []


def test_a_failed_read_is_an_empty_list_noted_at_debug(pp, db, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("broken")
    monkeypatch.setattr(pp._store, "connect", boom)
    with caplog.at_level(logging.DEBUG, logger=pp._LOGGER.name):
        assert pp.read("username", db_path=db) == []
    assert [r.getMessage() for r in caplog.records] == ["person_patterns read failed: broken"]


# ── schema and paths ─────────────────────────────────────────────────────────

def test_setting_up_the_table_twice_is_fine(pp, db):
    pp.ensure_schema(db)
    pp.ensure_schema(db)
    assert pp.read(db_path=db) == []


def test_a_failed_setup_is_only_a_debug_note(pp, db, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("cannot open")
    monkeypatch.setattr(pp._store, "connect", boom)
    with caplog.at_level(logging.DEBUG, logger=pp._LOGGER.name):
        assert pp.ensure_schema(db) is None
    assert [r.getMessage() for r in caplog.records] == [
        "person_patterns ensure_schema failed: cannot open"]


def test_the_database_defaults_to_the_shared_patterns_file(pp, load, monkeypatch):
    monkeypatch.setattr(pp, "DB_PATH", None)
    assert pp._db_path() == load("paths").patterns_db()
    monkeypatch.setattr(pp, "DB_PATH", "/tmp/elsewhere.db")
    assert pp._db_path() == "/tmp/elsewhere.db"


def test_the_default_database_is_used_when_none_is_given(pp, db, monkeypatch):
    monkeypatch.setattr(pp, "DB_PATH", db)
    assert pp.store("username", "t", "d") is True
    assert pp.read("username")[0]["description"] == "d"


def test_a_name_that_cannot_be_normalised_is_used_as_text(pp, monkeypatch, load):
    monkeypatch.setattr(load("identity"), "normalize",
                        lambda p: (_ for _ in ()).throw(RuntimeError("no")))
    assert pp._normalize("Some One") == "Some One"
    assert pp._normalize(None) == ""
