"""database.health() reports a failing conversation store instead of
hiding it, and a working store as fine."""
from __future__ import annotations


def test_a_writable_store_is_healthy(load, tmp_path, monkeypatch):
    db = load("database")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "nova" / "conversations.db")
    assert db.health() == {"ok": True, "error": ""}


def test_a_store_that_cannot_be_made_reports_why(load, tmp_path, monkeypatch):
    db = load("database")
    not_a_folder = tmp_path / "plain_file"
    not_a_folder.write_text("in the way")
    monkeypatch.setattr(db, "DB_PATH", not_a_folder / "nova" / "conversations.db")
    report = db.health()
    assert report["ok"] is False and report["error"]
