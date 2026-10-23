"""Automation trials: the failure paths, the type handling and the exact data
written, which the main trial tests do not pin."""
import sqlite3
import types

import pytest

from fakes import FakeHass


@pytest.fixture
def tr(load):
    return load("automation.trials")


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "patterns.db")


def _raw(db, sql="SELECT * FROM automation_trials ORDER BY id"):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


@pytest.fixture
def broken(tr, monkeypatch):
    def boom(path):
        raise sqlite3.OperationalError("cannot open")
    monkeypatch.setattr(tr, "_connect", boom)


# ── what create writes ───────────────────────────────────────────────────────

def test_create_stores_the_given_install_time_and_coerces_types(tr, db):
    tid = tr.create("7", 42, installed_at="150", db_path=db)
    (row,) = _raw(db)
    assert tid == row["id"]
    assert (row["suggestion_id"], row["automation_id"]) == (7, "42")
    assert row["installed_at"] == 150.0 and row["run_count"] == 0
    assert row["automation_entity_id"] is None


def test_create_without_a_time_uses_now(tr, db, monkeypatch):
    monkeypatch.setattr(tr.time, "time", lambda: 999.5)
    tr.create(1, "a", db_path=db)
    assert _raw(db)[0]["installed_at"] == 999.5


def test_ids_count_up(tr, db):
    assert [tr.create(n, f"a{n}", db_path=db) for n in (1, 2, 3)] == [1, 2, 3]


def test_a_bad_suggestion_id_gives_none_and_writes_nothing(tr, db):
    assert tr.create("not a number", "a", db_path=db) is None
    assert _raw(db) == []


def test_create_gives_none_when_the_database_cannot_open(tr, broken, db):
    assert tr.create(1, "a", db_path=db) is None


# ── record_run ───────────────────────────────────────────────────────────────

def test_a_run_resolves_the_entity_and_counts_in_one_step(tr, db):
    tr.create(1, "nova_auto_a", db_path=db)
    assert tr.record_run("automation.porch", "nova_auto_a", ts=5, db_path=db) is True
    (row,) = _raw(db)
    assert (row["automation_entity_id"], row["run_count"], row["last_run"]) == (
        "automation.porch", 1, 5.0)


def test_later_runs_match_on_the_entity_alone(tr, db):
    tr.create(1, "nova_auto_a", db_path=db)
    tr.record_run("automation.porch", "nova_auto_a", ts=1, db_path=db)
    assert tr.record_run("automation.porch", None, ts=2, db_path=db) is True
    (row,) = _raw(db)
    assert row["run_count"] == 2 and row["last_run"] == 2.0


def test_a_run_for_another_automation_changes_nothing(tr, db):
    tr.create(1, "nova_auto_a", db_path=db)
    assert tr.record_run("automation.other", "other_id", ts=1, db_path=db) is False
    assert tr.record_run("automation.other", None, ts=1, db_path=db) is False
    (row,) = _raw(db)
    assert row["run_count"] == 0 and row["automation_entity_id"] is None


def test_a_resolved_trial_is_not_claimed_by_a_matching_id_alone(tr, db):
    tr.create(1, "nova_auto_a", db_path=db)
    tr.record_run("automation.first", "nova_auto_a", ts=1, db_path=db)
    assert tr.record_run("automation.second", "nova_auto_a", ts=2, db_path=db) is False
    assert _raw(db)[0]["automation_entity_id"] == "automation.first"


def test_a_run_without_a_time_uses_now(tr, db, monkeypatch):
    monkeypatch.setattr(tr.time, "time", lambda: 77.0)
    tr.create(1, "a", db_path=db)
    tr.record_run("automation.x", "a", db_path=db)
    assert _raw(db)[0]["last_run"] == 77.0


def test_record_run_is_false_when_the_database_cannot_open(tr, broken, db):
    assert tr.record_run("automation.x", "a", db_path=db) is False


def test_record_run_is_false_when_the_time_is_not_a_number(tr, db):
    tr.create(1, "a", db_path=db)
    assert tr.record_run("automation.x", "a", ts="soon", db_path=db) is False
    assert _raw(db)[0]["run_count"] == 0


# ── list_trials ──────────────────────────────────────────────────────────────

def test_a_missing_file_is_listed_as_nothing_and_not_created(tr, tmp_path):
    path = tmp_path / "absent.db"
    assert tr.list_trials(db_path=str(path)) == []
    assert not path.exists()


def test_listing_gives_plain_dicts_with_every_column(tr, db):
    tr.create(1, "a", installed_at=1.0, db_path=db)
    (row,) = tr.list_trials(db_path=db)
    assert type(row) is dict
    assert set(row) == {"id", "suggestion_id", "automation_id", "automation_entity_id",
                        "installed_at", "run_count", "last_run", "manual_outcome",
                        "manual_outcome_ts"}


def test_listing_gives_nothing_when_the_database_cannot_open(tr, broken, db):
    open(db, "w").close()
    assert tr.list_trials(db_path=db) == []


def test_listing_gives_nothing_when_the_file_is_not_a_database(tr, tmp_path):
    path = tmp_path / "junk.db"
    path.write_text("this is not sqlite")
    assert tr.list_trials(db_path=str(path)) == []


# ── set_manual_outcome ───────────────────────────────────────────────────────

def test_an_outcome_is_stored_with_its_time(tr, db):
    tid = tr.create(1, "a", db_path=db)
    assert tr.set_manual_outcome(tid, "working", ts=12, db_path=db) is True
    (row,) = _raw(db)
    assert (row["manual_outcome"], row["manual_outcome_ts"]) == ("working", 12.0)


def test_an_outcome_without_a_time_uses_now(tr, db, monkeypatch):
    monkeypatch.setattr(tr.time, "time", lambda: 31.0)
    tid = tr.create(1, "a", db_path=db)
    tr.set_manual_outcome(str(tid), "needs_adjustment", db_path=db)
    assert _raw(db)[0]["manual_outcome_ts"] == 31.0


def test_the_verdict_is_stored_as_text_whatever_it_is(tr, db):
    tid = tr.create(1, "a", db_path=db)
    tr.set_manual_outcome(tid, 5, ts=1, db_path=db)
    assert _raw(db)[0]["manual_outcome"] == "5"


def test_a_missing_trial_is_false_and_a_bad_id_is_false(tr, db):
    tr.create(1, "a", db_path=db)
    assert tr.set_manual_outcome(99, "working", db_path=db) is False
    assert tr.set_manual_outcome("x", "working", db_path=db) is False
    assert _raw(db)[0]["manual_outcome"] is None


def test_an_outcome_is_false_when_the_database_cannot_open(tr, broken, db):
    assert tr.set_manual_outcome(1, "working", db_path=db) is False


# ── paths ────────────────────────────────────────────────────────────────────

def test_the_default_database_is_the_shared_patterns_file(tr, load, monkeypatch):
    monkeypatch.setattr(tr, "_DEFAULT_DB", None)
    assert tr._resolve(None) == load("paths").patterns_db()
    monkeypatch.setattr(tr, "_DEFAULT_DB", "/tmp/elsewhere.db")
    assert tr._resolve(None) == "/tmp/elsewhere.db"
    assert tr._resolve("/tmp/explicit.db") == "/tmp/explicit.db"


def test_the_default_database_is_used_when_none_is_given(tr, db, monkeypatch):
    monkeypatch.setattr(tr, "_DEFAULT_DB", db)
    assert tr.create(1, "a") == 1
    assert tr.list_trials()[0]["automation_id"] == "a"


def test_the_schema_is_added_without_touching_other_tables(tr, db):
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE keepme (x INTEGER)")
    conn.execute("INSERT INTO keepme VALUES (5)")
    conn.commit()
    conn.close()
    tr.create(1, "a", db_path=db)
    assert _raw(db, "SELECT x FROM keepme") == [{"x": 5}]


# ── the event listener ───────────────────────────────────────────────────────

class _Registry:
    def __init__(self, unique_id="uid-1"):
        self.asked = []
        self.unique_id = unique_id

    def async_get(self, entity_id):
        self.asked.append(entity_id)
        return types.SimpleNamespace(unique_id=self.unique_id) if self.unique_id else None


@pytest.fixture
def listener(tr, monkeypatch):
    import sys
    reg = _Registry()
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    er.async_get = lambda hass: reg
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", er)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", er,
                        raising=False)
    calls = []
    hass = FakeHass()

    async def run(func, *args):
        calls.append((func, args))
    hass.async_add_executor_job = run
    return types.SimpleNamespace(reg=reg, calls=calls, hass=hass)


class _Event:
    def __init__(self, data, fired=None):
        self.data = data
        if fired is not None:
            self.time_fired = fired


async def test_a_trigger_is_recorded_with_the_registry_id_and_the_event_time(tr, listener):
    import datetime
    when = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    await tr.async_handle_triggered(
        listener.hass, _Event({"entity_id": "automation.porch"}, when))
    assert listener.reg.asked == ["automation.porch"]
    assert listener.calls == [(tr.record_run, ("automation.porch", "uid-1", when.timestamp()))]


async def test_an_event_with_no_time_uses_now(tr, listener, monkeypatch):
    monkeypatch.setattr(tr.time, "time", lambda: 4321.0)
    await tr.async_handle_triggered(listener.hass, _Event({"entity_id": "automation.x"}))
    assert listener.calls[0][1] == ("automation.x", "uid-1", 4321.0)


async def test_an_entity_the_registry_does_not_know_is_recorded_with_no_unique_id(
        tr, listener):
    listener.reg.unique_id = None
    await tr.async_handle_triggered(
        listener.hass, _Event({"entity_id": "automation.x"}, None))
    assert listener.calls[0][1][1] is None


@pytest.mark.parametrize("data", [{}, {"entity_id": ""}, {"entity_id": None}])
async def test_an_event_without_an_entity_is_ignored(tr, listener, data):
    await tr.async_handle_triggered(listener.hass, _Event(data))
    assert listener.calls == [] and listener.reg.asked == []


async def test_a_failing_write_is_swallowed(tr, listener):
    async def boom(func, *args):
        raise RuntimeError("executor gone")
    listener.hass.async_add_executor_job = boom
    await tr.async_handle_triggered(listener.hass, _Event({"entity_id": "automation.x"}))
