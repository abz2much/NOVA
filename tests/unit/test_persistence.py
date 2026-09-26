"""Phase 9: Nova's persistence boundary.

Covers the single schema owner (persistence/schema.py), the setup-time
store upgrade (persistence/sqlite.py) against legacy databases, and the
JSON file helper (persistence/files.py). Every legacy database here is
built by hand from the columns older releases shipped with.
"""
import ast
import json
import os
import sqlite3
import stat
import threading

import pytest

import contract_extract as ce


@pytest.fixture
def ps(load):
    return load("persistence.sqlite")


@pytest.fixture
def schema(load):
    return load("persistence.schema")


@pytest.fixture
def files(load):
    return load("persistence.files")


def _store(schema, key):
    return next(s for s in schema.STORES if s.key == key)


def _cols(path, table):
    with sqlite3.connect(path) as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(path):
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _legacy(path, *sql):
    conn = sqlite3.connect(path)
    for s in sql:
        conn.execute(s)
    conn.commit()
    conn.close()


def _ledger(path):
    with sqlite3.connect(path) as conn:
        return dict(conn.execute("SELECT component, version FROM nova_schema_components"))


# ── Fresh schema is exactly what each owner used to create ──────────────────

FRESH_COLUMNS = {
    "conversations": ["id", "timestamp", "device_id", "role", "content", "subject"],
    "sentinel_events": ["id", "timestamp", "entity_id", "event_type", "detail"],
    "activity_log": ["id", "timestamp", "entity_id", "category", "urgency", "message",
                     "was_spoken", "source"],
    "spoken_history": ["id", "timestamp", "text", "source", "speakers", "delivery_state",
                       "repeat_of_id", "action_request_id"],
    "facts": ["id", "kind", "subject", "key", "value", "source", "confidence", "salience",
              "status", "created_at", "updated_at", "last_referenced", "expires_at"],
    "state_changes": ["id", "timestamp", "entity_id", "domain", "old_state", "new_state",
                      "area_id", "hour", "day_of_week", "triggered_by", "source_entity_id",
                      "source_confidence", "person", "person_confidence",
                      "detection_confidence"],
    "suggestions": ["id", "created", "description", "automation_yaml", "status",
                    "confidence", "pattern_count", "approved_at", "dismissed_at",
                    "pattern_type", "entity_ids", "details"],
    "cognition_model": ["entity_id", "data", "updated"],
    "cognition_alerted": ["key", "day"],
    "decision_records": ["id", "ts", "kind", "observation", "interpretation", "evidence",
                         "decision", "reason", "model", "tokens", "latency_ms",
                         "confidence", "outcome", "outcome_ts", "outcome_source", "ref"],
}


def test_fresh_schema_matches_the_shipped_columns(ps, schema, tmp_path):
    db = str(tmp_path / "fresh.db")
    conn = sqlite3.connect(db)
    names = [c for c in schema.COMPONENTS if not schema.COMPONENTS[c].virtual]
    ps.ensure(conn, *names)
    conn.commit()
    conn.close()
    for table, cols in FRESH_COLUMNS.items():
        assert _cols(db, table) == cols, table


def test_every_component_belongs_to_exactly_one_store(schema):
    owned = [c for s in schema.STORES for c in s.components]
    assert sorted(owned) == sorted(schema.COMPONENTS)


def test_storage_inventory_declares_every_nova_table_once(schema):
    """The pinned storage contract and the schema registry agree, including
    the four tables the old contract missed."""
    declared = [t for c in schema.COMPONENTS.values() for t in c.tables]
    assert len(declared) == len(set(declared))
    contract = ce.storage_contract()
    owned = set(contract["persistence.schema"]["tables"])
    assert owned == set(declared) | {schema.LEDGER_TABLE}
    assert {"cognition_model", "cognition_alerted", "memory_fts", "document_fts"} <= owned
    others = {t for mod, v in contract.items() if mod != "persistence.schema"
              for t in v["tables"]}
    assert others == set(), "a table is declared outside persistence/schema.py"


# ── Setup upgrade against legacy databases ──────────────────────────────────

def test_legacy_conversations_gain_subject_and_keep_rows(ps, schema, tmp_path):
    db = str(tmp_path / "conversations.db")
    _legacy(db,
            "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, device_id TEXT NOT NULL DEFAULT 'unknown', "
            "role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL)",
            "INSERT INTO conversations (timestamp, device_id, role, content) "
            "VALUES ('2026-01-01T00:00:00', 'dev', 'user', 'hello')")
    status = ps.upgrade_store(db, _store(schema, "conversations"))
    assert status.state == "upgraded"
    assert "subject" in _cols(db, "conversations")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT content, subject FROM conversations").fetchall() == [
            ("hello", None)]
    assert {"action_log", "spoken_history"} <= _tables(db)
    assert _ledger(db) == {"conversations": 2, "action_log": 1, "spoken_history": 2}


def test_legacy_spoken_history_gains_nullable_action_request_id(ps, schema, tmp_path):
    db = str(tmp_path / "conversations.db")
    _legacy(db,
            "CREATE TABLE spoken_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL, "
            "speakers TEXT NOT NULL, delivery_state TEXT NOT NULL DEFAULT 'sent', "
            "repeat_of_id INTEGER)",
            "INSERT INTO spoken_history (timestamp, text, source, speakers) "
            "VALUES (1.0, 'hi', 'reminder', '[]')")
    assert ps.upgrade_store(db, _store(schema, "conversations")).ok
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT text, action_request_id FROM spoken_history").fetchall() == [("hi", None)]


def test_knowledge_without_status_reads_back_confirmed(ps, schema, tmp_path):
    db = str(tmp_path / "knowledge.db")
    _legacy(db,
            "CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "kind TEXT NOT NULL DEFAULT 'fact', subject TEXT NOT NULL DEFAULT 'household', "
            "key TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'stated', "
            "confidence REAL NOT NULL DEFAULT 1.0, salience REAL NOT NULL DEFAULT 1.0, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL, last_referenced REAL, "
            "expires_at REAL, UNIQUE(subject, key))",
            "INSERT INTO facts (key, value, created_at, updated_at) VALUES ('bins', 'tue', 1, 1)")
    assert ps.upgrade_store(db, _store(schema, "knowledge")).state == "upgraded"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT key, status FROM facts").fetchall() == [("bins", "confirmed")]


def test_knowledge_with_status_keeps_pending_and_confirmed_exactly(ps, schema, tmp_path, load):
    db = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(db)
    ps.ensure(conn, "facts")
    conn.executemany(
        "INSERT INTO facts (key, value, status, created_at, updated_at) VALUES (?, ?, ?, 1, 1)",
        [("a", "1", "pending"), ("b", "2", "confirmed")])
    conn.commit()
    conn.close()
    for _ in range(2):
        assert ps.upgrade_store(db, _store(schema, "knowledge")).ok
    with sqlite3.connect(db) as conn:
        assert dict(conn.execute("SELECT key, status FROM facts")) == {
            "a": "pending", "b": "confirmed"}
    kn = load("knowledge")
    kn.DB_PATH = db
    try:
        assert [f["key"] for f in kn.pending_facts()] == ["a"]
    finally:
        kn.DB_PATH = "/config/nova/knowledge.db"


def test_oldest_pattern_log_gains_every_shipped_column(ps, schema, tmp_path):
    db = str(tmp_path / "patterns.db")
    _legacy(db,
            "CREATE TABLE state_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, entity_id TEXT NOT NULL, domain TEXT NOT NULL, "
            "old_state TEXT, new_state TEXT NOT NULL, area_id TEXT, hour INTEGER, "
            "day_of_week INTEGER, triggered_by TEXT DEFAULT 'system')",
            "CREATE TABLE suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "created TEXT NOT NULL, description TEXT NOT NULL, automation_yaml TEXT, "
            "status TEXT DEFAULT 'pending', confidence REAL DEFAULT 0.0, "
            "pattern_count INTEGER DEFAULT 0, approved_at TEXT, dismissed_at TEXT)",
            "INSERT INTO state_changes (timestamp, entity_id, domain, new_state) "
            "VALUES ('2026-01-01T08:00:00', 'light.hall', 'light', 'on')")
    assert ps.upgrade_store(db, _store(schema, "patterns")).state == "upgraded"
    cols = _cols(db, "state_changes")
    for c in ("person", "person_confidence", "detection_confidence",
              "source_entity_id", "source_confidence"):
        assert c in cols
    assert {"pattern_type", "entity_ids", "details"} <= set(_cols(db, "suggestions"))
    assert {"idx_sc_person", "idx_sc_source"} <= _indexes(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT person, person_confidence, detection_confidence, "
                           "source_entity_id FROM state_changes").fetchone()
    assert row == ("unknown", 0.0, None, "")
    assert {"cognition_model", "cognition_alerted", "automation_trials", "followups",
            "goals", "person_patterns", "commands"} <= _tables(db)


def test_cognition_alerted_is_created_beside_an_existing_model(ps, schema, tmp_path):
    db = str(tmp_path / "patterns.db")
    _legacy(db,
            "CREATE TABLE cognition_model (entity_id TEXT PRIMARY KEY, data TEXT, updated REAL)",
            "INSERT INTO cognition_model VALUES ('light.hall', '{}', 1.0)")
    assert ps.upgrade_store(db, _store(schema, "patterns")).ok
    assert "cognition_alerted" in _tables(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT entity_id FROM cognition_model").fetchall() == [
            ("light.hall",)]


def test_cognition_load_creates_both_tables(load, tmp_path):
    cog = load("cognition")
    db = str(tmp_path / "patterns.db")
    cog.load_from_db(db)
    assert {"cognition_model", "cognition_alerted"} <= _tables(db)


def _legacy_decisions(db):
    _legacy(db,
            "CREATE TABLE decision_records (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts REAL NOT NULL, kind TEXT NOT NULL, observation TEXT NOT NULL DEFAULT '{}', "
            "interpretation TEXT NOT NULL DEFAULT '{}', evidence TEXT NOT NULL DEFAULT '{}', "
            "decision TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', model TEXT, "
            "tokens INTEGER, latency_ms INTEGER, confidence REAL, outcome TEXT, "
            "outcome_ts REAL, outcome_source TEXT)",
            "INSERT INTO decision_records (ts, kind) VALUES (1.0, 'announce')")


def test_decisions_without_ref_upgrade_at_setup(ps, schema, tmp_path):
    db = str(tmp_path / "decisions.db")
    _legacy_decisions(db)
    assert ps.upgrade_store(db, _store(schema, "decisions")).state == "upgraded"
    assert "ref" in _cols(db, "decision_records")
    assert "idx_dr_ref" in _indexes(db)


def test_decisions_without_ref_open_on_first_use(load, tmp_path):
    """The old owner built idx_dr_ref before adding `ref`, so a database from
    before `ref` failed on every connect. The column now comes first."""
    dr = load("decision_record")
    db = str(tmp_path / "decisions.db")
    _legacy_decisions(db)
    conn = dr._connect(db)
    conn.close()
    assert "ref" in _cols(db, "decision_records")
    assert "idx_dr_ref" in _indexes(db)


# ── Repeat, partial, failure and concurrency ────────────────────────────────

def test_repeat_upgrade_is_a_no_op(ps, schema, tmp_path):
    db = str(tmp_path / "patterns.db")
    _legacy(db, "CREATE TABLE cognition_model (entity_id TEXT PRIMARY KEY, data TEXT, updated REAL)")
    store = _store(schema, "patterns")
    assert ps.upgrade_store(db, store).state == "upgraded"
    before = _ledger(db)
    assert ps.upgrade_store(db, store).state == "current"
    assert _ledger(db) == before
    assert before == {name: schema.COMPONENTS[name].version for name in store.components}


def test_partially_migrated_database_completes(ps, schema, tmp_path):
    """A prior attempt that added some columns (and even stamped a stale
    ledger) is finished from the real schema, not trusted from the ledger."""
    db = str(tmp_path / "patterns.db")
    _legacy(db,
            "CREATE TABLE state_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, entity_id TEXT NOT NULL, domain TEXT NOT NULL, "
            "old_state TEXT, new_state TEXT NOT NULL, area_id TEXT, hour INTEGER, "
            "day_of_week INTEGER, triggered_by TEXT DEFAULT 'system', "
            "person TEXT DEFAULT 'unknown')",
            schema.LEDGER_DDL,
            "INSERT INTO nova_schema_components VALUES ('pattern_log', 6, 'earlier')")
    assert ps.upgrade_store(db, _store(schema, "patterns")).ok
    assert "source_confidence" in _cols(db, "state_changes")
    assert "pattern_type" in _cols(db, "suggestions")


def test_failed_upgrade_rolls_the_whole_file_back(ps, schema, tmp_path, monkeypatch):
    db = str(tmp_path / "patterns.db")
    _legacy(db,
            "CREATE TABLE state_changes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT NOT NULL, entity_id TEXT NOT NULL, domain TEXT NOT NULL, "
            "old_state TEXT, new_state TEXT NOT NULL, area_id TEXT, hour INTEGER, "
            "day_of_week INTEGER, triggered_by TEXT DEFAULT 'system')")
    before_tables, before_cols = _tables(db), _cols(db, "state_changes")
    broken = schema.Component(name="goals", version=1, tables=("goals",),
                              statements=("CREATE TABLE goals (",))
    monkeypatch.setitem(schema.COMPONENTS, "goals", broken)
    status = ps.upgrade_store(db, _store(schema, "patterns"))
    assert status.state == "failed" and not status.ok
    assert "OperationalError" in status.error
    assert _tables(db) == before_tables
    assert _cols(db, "state_changes") == before_cols


def test_missing_store_is_left_for_its_owner(ps, schema, tmp_path):
    results = ps.upgrade_existing(str(tmp_path))
    assert {s.state for s in results.values()} == {"absent"}
    assert not any(tmp_path.rglob("*.db"))
    assert "nova" not in results           # nova.db stays lazy (FTS5 / memory backend)
    assert set(results) == {s.key for s in schema.STORES if s.upgrade_at_setup}


def test_corrupt_store_fails_safely_and_is_not_touched(ps, tmp_path):
    (tmp_path / "nova").mkdir()
    bad = tmp_path / "nova" / "knowledge.db"
    bad.write_bytes(b"not a database at all" * 50)
    before = bad.read_bytes()
    results = ps.upgrade_existing(str(tmp_path))
    assert results["knowledge"].state == "failed"
    assert bad.read_bytes() == before
    assert ps.last_upgrade()["knowledge"].state == "failed"


def test_concurrent_upgrades_of_one_file_serialise(ps, schema, tmp_path):
    db = str(tmp_path / "conversations.db")
    _legacy(db, "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp TEXT NOT NULL, device_id TEXT NOT NULL DEFAULT 'unknown', "
                "role TEXT NOT NULL, content TEXT NOT NULL)")
    store = _store(schema, "conversations")
    out = []
    threads = [threading.Thread(target=lambda: out.append(ps.upgrade_store(db, store)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(s.ok for s in out)
    assert sorted(s.state for s in out) == ["current", "current", "current", "upgraded"]


def test_older_release_sql_still_reads_and_writes_after_upgrade(ps, schema, tmp_path):
    """Rollback: an older release's statements, which name only the columns
    it knew, keep working on an upgraded file; the ledger is ignored."""
    db = str(tmp_path / "conversations.db")
    _legacy(db, "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp TEXT NOT NULL, device_id TEXT NOT NULL DEFAULT 'unknown', "
                "role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL)")
    assert ps.upgrade_store(db, _store(schema, "conversations")).ok
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO conversations (timestamp, device_id, role, content) "
                     "VALUES ('2026-01-02T00:00:00', 'dev', 'assistant', 'ok')")
        assert conn.execute("SELECT id, timestamp, device_id, role, content "
                            "FROM conversations").fetchall()[0][4] == "ok"


def test_ensure_column_tolerates_a_proven_race_only(ps, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "x.db"))
    conn.execute("CREATE TABLE t (a TEXT)")
    assert ps.ensure_column(conn, "t", "b", "TEXT") is True
    assert ps.ensure_column(conn, "t", "b", "TEXT") is False
    conn.close()


def test_setup_upgrades_stores_in_the_executor_and_never_blocks(load):
    src = (ce.COMP / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    setup = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "async_setup_entry")
    calls = [n for n in ast.walk(setup) if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith("async_add_executor_job")
             and n.args and ast.unparse(n.args[0]).endswith("upgrade_existing")]
    assert len(calls) == 1
    guarded = [n for n in ast.walk(setup) if isinstance(n, ast.Try)
               and any(c in ast.walk(n) for c in calls)]
    assert guarded, "the storage upgrade must be wrapped so it can never fail setup"


def test_persistence_package_is_home_assistant_free():
    for path in (ce.COMP / "persistence").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = ([a.name for a in node.names] if isinstance(node, ast.Import)
                        else [node.module or ""])
                assert not any(m.startswith("homeassistant") for m in mods), path.name
                if isinstance(node, ast.ImportFrom) and node.level > 1:
                    raise AssertionError(f"{path.name} imports outside the package")


# ── JSON files ──────────────────────────────────────────────────────────────

def test_read_json_tells_missing_ok_and_corrupt_apart(files, tmp_path):
    p = tmp_path / "state.json"
    assert files.read_json(p).status == files.MISSING
    p.write_text('{"a": 1}')
    assert files.read_json(p).status == files.OK and files.read_json(p).value == {"a": 1}
    p.write_text('{"a": ')
    r = files.read_json(p)
    assert r.status == files.CORRUPT and r.value is None and r.error


def test_atomic_write_replaces_whole_file_and_keeps_its_mode(files, tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{}")
    os.chmod(p, 0o640)
    files.write_json_atomic(p, {"b": 2}, indent=2)
    assert json.loads(p.read_text()) == {"b": 2}
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640
    assert [x.name for x in tmp_path.iterdir()] == ["state.json"]


def test_failed_atomic_write_leaves_the_old_file(files, tmp_path):
    p = tmp_path / "state.json"
    p.write_text('{"keep": true}')
    with pytest.raises(TypeError):
        files.write_json_atomic(p, {"bad": object()})
    assert json.loads(p.read_text()) == {"keep": True}
    assert [x.name for x in tmp_path.iterdir()] == ["state.json"]


def test_corrupt_reasoning_cache_loads_empty(load, tmp_path, monkeypatch):
    rc = load("reasoning_cache")
    p = tmp_path / "reasoning_cache.json"
    p.write_text("{broken")
    monkeypatch.setattr(rc, "CACHE_PATH", p)
    monkeypatch.setattr(rc, "_loaded", False)
    monkeypatch.setattr(rc, "_cache", {})
    assert rc.load() == 0
    rc._cache["sig"] = {"speak": True, "refreshed": 1}
    rc.save()
    assert json.loads(p.read_text()) == {"sig": {"speak": True, "refreshed": 1}}
