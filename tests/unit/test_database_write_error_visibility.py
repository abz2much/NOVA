"""Regression test for the write-failure visibility fix (security review,
Finding 6): _connect() already recorded a connect/schema failure in
_last_error, but a successful connect followed by a failure in the INSERT
itself (e.g. disk full) left _last_error at None -- health() would keep
reporting "ok" right after a real write failure went by silently."""


def test_save_message_insert_failure_updates_last_error(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")

    # A real connect succeeds first, clearing _last_error to None...
    with db._connect():
        pass
    assert db._last_error is None

    # ...then the INSERT itself fails for an unrelated reason.
    class _BoomConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, *a, **kw):
            raise db.sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "_connect", lambda: _BoomConn())
    db.save_message("user", "hello", "test_device")

    assert db._last_error is not None
    assert "disk I/O error" in db._last_error


def test_save_message_success_leaves_last_error_none(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    db.save_message("user", "hello", "test_device")
    assert db._last_error is None
    assert db.health()["ok"] is True
