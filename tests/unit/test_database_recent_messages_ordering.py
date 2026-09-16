"""Fix: get_recent_messages() must select the NEWEST `limit` matching rows,
not the oldest. The prior query ordered ascending before LIMIT, which
returns the oldest matching rows whenever more than `limit` rows exist in
the time window -- wrong for continuity/reseeding, which needs the most
recent history. The fix selects the newest rows (timestamp DESC, id DESC)
in an inner query, then re-sorts just that selected set back into
chronological (oldest-first) order for the return value, preserving the
existing caller contract.

Real, isolated SQLite (no mocks) -- inserts more rows than `limit` so a
naive ascending-then-LIMIT bug would be directly observable.
"""


def test_device_scoped_returns_newest_rows_in_chronological_order(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    for i in range(10):
        db.save_message("user", f"message {i}", "dev1")

    rows = db.get_recent_messages(hours=24, device_id="dev1", limit=3)

    # Newest 3 of 10 (indices 7, 8, 9), returned oldest-to-newest.
    assert [r["content"] for r in rows] == ["message 7", "message 8", "message 9"]


def test_subject_scoped_returns_newest_rows_in_chronological_order(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    for i in range(10):
        db.save_message("user", f"message {i}", f"conv-{i}", "alice")

    rows = db.get_recent_messages(hours=24, subject="alice", limit=3)

    assert [r["content"] for r in rows] == ["message 7", "message 8", "message 9"]


def test_unscoped_returns_newest_rows_in_chronological_order(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    for i in range(10):
        db.save_message("user", f"message {i}", f"dev-{i}")

    rows = db.get_recent_messages(hours=24, limit=3)

    assert [r["content"] for r in rows] == ["message 7", "message 8", "message 9"]


def test_device_scoped_still_applies_the_time_window(load, tmp_path, monkeypatch):
    """The scope/window filters must still apply -- this fix only changes
    which end of the matching set gets selected, not what matches."""
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    db.save_message("user", "in scope", "dev1")
    db.save_message("user", "wrong device", "dev2")

    rows = db.get_recent_messages(hours=24, device_id="dev1", limit=10)

    assert [r["content"] for r in rows] == ["in scope"]


def test_device_scoped_returns_fewer_than_limit_when_fewer_rows_exist(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")
    for i in range(2):
        db.save_message("user", f"message {i}", "dev1")

    rows = db.get_recent_messages(hours=24, device_id="dev1", limit=10)

    assert [r["content"] for r in rows] == ["message 0", "message 1"]


def test_ordering_is_deterministic_even_with_identical_timestamps(load, tmp_path, monkeypatch):
    """id DESC as a tiebreaker for rows sharing a timestamp -- guards against
    flaky ordering on fast machines where several inserts land in the same
    timestamp tick."""
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "conversations.db")

    # Force identical timestamps by inserting directly, bypassing
    # save_message's own now() call.
    with db._connect() as conn:
        for i in range(5):
            conn.execute(
                "INSERT INTO conversations (timestamp, device_id, role, content, subject) "
                "VALUES (?,?,?,?,?)",
                ("2026-01-01T00:00:00", "dev1", "user", f"same-ts message {i}", None),
            )

    rows = db.get_recent_messages(hours=24 * 365, device_id="dev1", limit=3)

    # Newest by id (2, 3, 4 -- the last three inserted), still ascending by id.
    assert [r["content"] for r in rows] == [
        "same-ts message 2", "same-ts message 3", "same-ts message 4",
    ]
