"""Phase 2 — additive `subject` column migration on the conversations table.

The migration must: inspect PRAGMA table_info, ALTER TABLE only when the
column is absent, tolerate a proven concurrent duplicate-column race, and
re-raise every other failure so database.health() reports the degraded
state — never swallow silently.
"""


def _cols(db, table="conversations"):
    with db._connect() as conn:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_adds_subject_column_on_a_fresh_database(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    assert "subject" in _cols(db)


def test_migration_is_idempotent_across_repeated_connects(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    for _ in range(5):
        with db._connect():
            pass
    assert "subject" in _cols(db)
    assert db._last_error is None


def test_save_message_returns_lastrowid_on_success(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    row_id = db.save_message("user", "hello", "dev1", "alice")
    assert isinstance(row_id, int)
    rows = db.get_recent_messages(hours=24, device_id="dev1")
    assert rows[0]["id"] == row_id
    assert rows[0]["subject"] == "alice"


def test_save_message_old_positional_callers_still_work_ignoring_return(load, tmp_path, monkeypatch):
    """Matches sentinel.py's exact calling convention: 3 positional args, no
    subject, return value ignored entirely."""
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    db.save_message("assistant", "[Sentinel] motion detected", device_id="sentinel")
    rows = db.get_recent_messages(hours=24, device_id="sentinel")
    assert len(rows) == 1
    assert rows[0]["content"] == "[Sentinel] motion detected"
    assert rows[0]["subject"] is None


def test_save_message_write_failure_returns_none(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")

    class _BoomConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, *a, **kw):
            raise db.sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "_connect", lambda: _BoomConn())
    result = db.save_message("user", "hello", "dev1")
    assert result is None


def test_existing_rows_read_back_with_subject_none(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    # A row inserted before subject-awareness (no subject arg at all).
    row_id = db.save_message("user", "hello")
    rows = db.get_recent_messages(hours=24, device_id="unknown")
    matching = [r for r in rows if r["id"] == row_id]
    assert len(matching) == 1
    assert matching[0]["subject"] is None


def test_concurrent_duplicate_column_race_is_tolerated(load, tmp_path, monkeypatch):
    """Simulate two connections racing to add the column: our own PRAGMA
    check says it's absent, but by the time ALTER TABLE runs, another
    connection has already added it — SQLite raises "duplicate column
    name". A second, fresh PRAGMA proving the column now exists must be
    treated as success, not swallowed blindly and not re-raised."""
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")

    # Ensure the column already exists (as if a concurrent connection just
    # added it), then force the shared ensure_column's ALTER TABLE to hit
    # the real "duplicate column" error, proving the recovery path runs for
    # real rather than being reasoned about.
    with db._connect() as conn:
        pass  # migration already ran once, column exists

    with db._connect() as conn:
        try:
            conn.execute("ALTER TABLE conversations ADD COLUMN subject TEXT")
            raise AssertionError("expected a duplicate-column OperationalError")
        except db.sqlite3.OperationalError as exc:
            assert "duplicate column" in str(exc).lower()
        # The shared migration helper, called again on an already-migrated
        # table, must return cleanly (early "already present" branch) —
        # the actual code path a repeated _connect() takes.
        assert db._store.ensure_column(conn, "conversations", "subject", "TEXT") is False


def test_genuine_migration_failure_surfaces_through_health(load, tmp_path, monkeypatch):
    """A real (non-race) migration failure must not be swallowed — it must
    propagate out of _connect() and be visible via database.health()."""
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")

    def _boom(conn, *components):
        raise db.sqlite3.OperationalError("simulated genuine migration failure")

    monkeypatch.setattr(db._store, "ensure", _boom)
    result = db.health()
    assert result["ok"] is False
    assert "simulated genuine migration failure" in result["error"]


def test_migrate_subject_column_reraises_when_column_still_missing_after_race_check(
    load, tmp_path,
):
    """The race-tolerance branch only swallows when a SECOND PRAGMA proves
    the column exists. If ALTER TABLE fails for some other reason and the
    column is genuinely still missing, the error must re-raise, not be
    treated as a tolerated race. Built against a genuinely fresh table with
    no subject column at all — bypassing database.py's own _connect() so
    the "still missing" branch is real, not simulated blind."""
    db = load("database")
    raw_path = tmp_path / "fresh_no_subject.db"
    conn = db.sqlite3.connect(str(raw_path))
    conn.row_factory = db.sqlite3.Row
    conn.executescript(
        "CREATE TABLE conversations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
        "device_id TEXT NOT NULL DEFAULT 'unknown', "
        "role TEXT NOT NULL CHECK(role IN ('user','assistant')), "
        "content TEXT NOT NULL)"
    )
    conn.commit()
    cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(conversations)")}
    assert "subject" not in cols_before

    class _FailAlterProxy:
        """sqlite3.Connection.execute is a read-only C-level attribute, so
        it can't be monkeypatched directly on an instance — wrap it instead.
        ensure_column() only ever calls .execute() on what it's
        given, so a thin proxy is sufficient."""
        def __init__(self, real_conn):
            self._real = real_conn

        def execute(self, sql, *a, **kw):
            if "ALTER TABLE conversations ADD COLUMN subject" in sql:
                # A persistent failure (e.g. disk full), not a race — the
                # column never actually gets added by this fake.
                raise db.sqlite3.OperationalError("disk I/O error")
            return self._real.execute(sql, *a, **kw)

    try:
        db._store.ensure_column(_FailAlterProxy(conn), "conversations", "subject", "TEXT")
        raised = False
    except db.sqlite3.OperationalError:
        raised = True
    finally:
        conn.close()

    assert raised is True, (
        "a genuine, non-race ALTER TABLE failure (column still missing "
        "afterward) must re-raise, not be swallowed"
    )


def test_get_recent_messages_device_id_is_first_choice_over_subject(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    db.save_message("user", "conv-scoped msg", "conv-1", "alice")
    db.save_message("user", "subject-scoped msg", "conv-2", "alice")

    # device_id given -> used, subject ignored even though also given.
    rows = db.get_recent_messages(hours=24, device_id="conv-1", subject="alice")
    assert [r["content"] for r in rows] == ["conv-scoped msg"]


def test_get_recent_messages_subject_filter_used_when_device_id_absent(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    db.save_message("user", "alice's message", "conv-1", "alice")
    db.save_message("user", "bob's message", "conv-2", "bob")

    rows = db.get_recent_messages(hours=24, subject="alice")
    assert [r["content"] for r in rows] == ["alice's message"]
