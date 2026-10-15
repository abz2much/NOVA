"""Every conversation store operation closes its connection (8.49.40).

``with conn:`` on a sqlite3 connection only commits or rolls back; it never
closes. Each read and write used to leave its connection open until the
garbage collector got to it.
"""
import pytest


@pytest.fixture
def tracked(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    opened = []
    real = db._store.connect

    def connect(path, **kw):
        conn = real(path, **kw)
        opened.append(conn)
        return conn
    monkeypatch.setattr(db._store, "connect", connect)
    return db, opened


def _still_open(conns):
    left = []
    for conn in conns:
        try:
            conn.execute("SELECT 1")
            left.append(conn)
        except Exception:     # a closed connection refuses any statement
            pass
    return left


@pytest.mark.parametrize("call", [
    lambda db: db.save_message("user", "hello", "dev"),
    lambda db: db.get_recent_messages(),
    lambda db: db.get_recent_messages(subject="someone"),
    lambda db: db.get_stats(),
    lambda db: db.delete_conversation("dev"),
    lambda db: db.purge_old_records(),
    lambda db: db.save_sentinel_event("lock.side", "unlocked"),
    lambda db: db.save_activity(message="x"),
    lambda db: db.get_recent_activity(),
    lambda db: db.get_activity_count_today(),
    lambda db: db.get_activity_counts(),
    lambda db: db.health(),
])
def test_each_operation_closes_its_connection(tracked, call):
    db, opened = tracked
    call(db)
    assert opened, "the operation never opened the store"
    assert _still_open(opened) == []


def test_a_failed_write_still_closes_and_rolls_back(tracked):
    db, opened = tracked
    db.save_message("user", "kept", "dev")
    assert db.save_message("robot", "refused by the role check", "dev") is None
    assert _still_open(opened) == []
    assert [m["content"] for m in db.get_recent_messages(device_id="dev")] == ["kept"]
