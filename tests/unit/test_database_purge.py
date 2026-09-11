"""Guards for the automatic database purge (Nova Unification item 3, 11 Sept
2026). `purge_old_records` already existed and worked, but was only reachable
by manually calling the nova.database_purge service — nothing ever called it
on its own, so an untouched install's DB just grew forever. These confirm the
purge logic itself is correct; the scheduler wiring in __init__.py (which
needs a live HA stack to exercise for real) is guarded separately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _iso(days_ago: float) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - timedelta(days=days_ago)).isoformat()


def test_purge_removes_old_rows_across_all_three_tables(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")

    old_ts = _iso(40)
    recent_ts = _iso(5)

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO conversations (timestamp, device_id, role, content) VALUES (?,?,?,?)",
            (old_ts, "test", "user", "old message"),
        )
        conn.execute(
            "INSERT INTO conversations (timestamp, device_id, role, content) VALUES (?,?,?,?)",
            (recent_ts, "test", "user", "recent message"),
        )
        conn.execute(
            "INSERT INTO sentinel_events (timestamp, entity_id, event_type, detail) VALUES (?,?,?,?)",
            (old_ts, "binary_sensor.x", "motion", ""),
        )
        conn.execute(
            "INSERT INTO sentinel_events (timestamp, entity_id, event_type, detail) VALUES (?,?,?,?)",
            (recent_ts, "binary_sensor.x", "motion", ""),
        )
        conn.execute(
            "INSERT INTO activity_log (timestamp, entity_id, category, urgency, message) "
            "VALUES (?,?,?,?,?)",
            (old_ts, "", "other", "low", "old activity"),
        )

    db.purge_old_records(days=30)

    with db._connect() as conn:
        convo_rows = [r["content"] for r in conn.execute("SELECT content FROM conversations")]
        assert convo_rows == ["recent message"]
        sentinel_count = conn.execute("SELECT COUNT(*) c FROM sentinel_events").fetchone()["c"]
        assert sentinel_count == 1
        activity_count = conn.execute("SELECT COUNT(*) c FROM activity_log").fetchone()["c"]
        assert activity_count == 0


def test_purge_respects_custom_retention_window(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")

    ten_days_ago = _iso(10)
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO conversations (timestamp, device_id, role, content) VALUES (?,?,?,?)",
            (ten_days_ago, "test", "user", "10 days old"),
        )

    db.purge_old_records(days=30)
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"] == 1

    db.purge_old_records(days=5)
    with db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"] == 0


def test_purge_never_raises_on_a_broken_db(load, tmp_path, monkeypatch):
    db = load("database")
    blocker = tmp_path / "afile"
    blocker.write_text("x")                          # a file where a dir is needed
    monkeypatch.setattr(db, "DB_PATH", blocker / "sub" / "conversations.db")
    assert db.purge_old_records(days=30) == 0         # fails safe, doesn't raise
