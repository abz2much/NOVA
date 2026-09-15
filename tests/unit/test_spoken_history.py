"""Spoken History (v7.104.0) — the last things Nova sent to a speaker.

Pure DB + in-memory-mirror functions, isolated against a tmp-path db exactly
like provider_activity.py / decision_record.py / automation_trials.py's own
tests. async_announce's own recording hook is covered separately in
test_announce_resilience.py (it needs a real delivery simulation), and the
deterministic local "repeat that" command is covered in test_local_engine.py.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def sh(load):
    # The `load` fixture caches the exec'd module under sys.modules across
    # tests in this file (same reason automation_trials.py's tests isolate
    # DB state via a fresh tmp_path db per test); spoken_history.py's
    # in-memory mirror is separate module-level state that needs the same
    # explicit reset, or a value recorded by one test leaks into the next.
    mod = load("spoken_history")
    mod._last = None
    return mod


def test_record_and_list_recent_round_trip(sh, tmp_path):
    db = str(tmp_path / "conversations.db")
    new_id = sh.record("Welcome home, sir.", "welcome", ["media_player.kitchen"], db_path=db)
    assert new_id is not None

    entries = sh.list_recent(db_path=db)
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == new_id
    assert e["text"] == "Welcome home, sir."
    assert e["source"] == "welcome"
    assert e["speakers"] == ["media_player.kitchen"]
    assert e["delivery_state"] == "sent"
    assert e["repeat_of_id"] is None


def test_failed_speech_is_never_recorded():
    """There is no 'failure' delivery_state — a caller simply never calls
    record() when delivery failed. Nothing to assert against the module
    itself beyond record() requiring non-empty speakers/text/source."""


def test_record_with_empty_speakers_or_text_is_a_noop(sh, tmp_path):
    db = str(tmp_path / "conversations.db")
    assert sh.record("", "welcome", ["media_player.kitchen"], db_path=db) is None
    assert sh.record("hello", "welcome", [], db_path=db) is None
    assert sh.list_recent(db_path=db) == []


def test_record_never_raises_on_a_broken_db_path(sh, monkeypatch):
    """Recording failure must never break speech — record() swallows and
    returns None rather than raising, even when the underlying connect
    fails outright."""
    def _boom(_db_path):
        raise RuntimeError("disk full")
    monkeypatch.setattr(sh, "_connect", _boom)
    result = sh.record("hello", "welcome", ["media_player.kitchen"], db_path="/nonexistent/x.db")
    assert result is None


def test_retention_keeps_at_most_100_entries_newest_first(sh, tmp_path):
    db = str(tmp_path / "conversations.db")
    for i in range(105):
        sh.record(f"message {i}", "reminder", ["media_player.a"], db_path=db)

    entries = sh.list_recent(db_path=db)
    assert len(entries) == 100
    # newest first: the very last inserted (104) leads, the oldest 5 (0-4) were pruned
    assert entries[0]["text"] == "message 104"
    assert entries[-1]["text"] == "message 5"


def test_get_returns_one_entry_by_id(sh, tmp_path):
    db = str(tmp_path / "conversations.db")
    new_id = sh.record("hello", "reminder", ["media_player.a"], db_path=db)
    row = sh.get(new_id, db_path=db)
    assert row is not None
    assert row["text"] == "hello"


def test_get_returns_none_for_missing_id(sh, tmp_path):
    db = str(tmp_path / "conversations.db")
    assert sh.get(999, db_path=db) is None


def test_repeat_of_id_is_flattened_to_the_true_original(sh, tmp_path):
    """A repeat of a repeat must always reference the true original, never
    chain through the intermediate repeat entry."""
    db = str(tmp_path / "conversations.db")
    original_id = sh.record("hello", "reminder", ["media_player.a"], db_path=db)
    repeat_id = sh.record("hello", "repeat", ["media_player.a"], repeat_of_id=original_id, db_path=db)
    row = sh.get(repeat_id, db_path=db)
    assert row["repeat_of_id"] == original_id

    # Repeating the repeat must still point at the ORIGINAL, not at repeat_id.
    second_repeat_id = sh.record("hello", "repeat", ["media_player.a"], repeat_of_id=repeat_id, db_path=db)
    row2 = sh.get(second_repeat_id, db_path=db)
    assert row2["repeat_of_id"] == original_id


def test_get_last_returns_none_before_anything_recorded(sh):
    assert sh.get_last() is None


def test_get_last_mirrors_the_most_recent_successful_record(sh, tmp_path):
    db = str(tmp_path / "conversations.db")
    sh.record("first", "reminder", ["media_player.a"], db_path=db)
    new_id = sh.record("second", "alert", ["media_player.b"], db_path=db)
    last = sh.get_last()
    assert last["id"] == new_id
    assert last["text"] == "second"
    assert last["source"] == "alert"


def test_get_last_never_touches_sqlite(sh, tmp_path, monkeypatch):
    """The in-memory mirror must be safe to call directly on the event
    loop — it must never open a database connection."""
    db = str(tmp_path / "conversations.db")
    sh.record("hello", "reminder", ["media_player.a"], db_path=db)

    def _forbidden(*a, **k):
        raise AssertionError("get_last() must never touch SQLite")
    monkeypatch.setattr(sh, "_connect", _forbidden)
    assert sh.get_last() is not None  # must succeed without calling _connect


def test_hydrate_loads_the_latest_row_into_the_mirror(sh, tmp_path):
    """Simulates a restart: the module-level mirror starts empty, and
    hydrate() must fill it from whatever is already on disk, so a voice
    'repeat that' works immediately without Nova needing to speak
    something new first."""
    db = str(tmp_path / "conversations.db")
    new_id = sh.record("last message before restart", "briefing", ["media_player.a"], db_path=db)
    sh._last = None  # simulate a fresh process with no in-memory state yet

    assert sh.get_last() is None
    sh.hydrate(db_path=db)
    last = sh.get_last()
    assert last is not None
    assert last["id"] == new_id
    assert last["text"] == "last message before restart"


def test_hydrate_on_empty_db_leaves_mirror_none(sh, tmp_path):
    db = str(tmp_path / "conversations.db")
    sh.hydrate(db_path=db)
    assert sh.get_last() is None


def test_hydrate_never_raises_on_a_broken_db(sh, monkeypatch):
    """Setup must fail open — a history-store problem can never block
    integration setup."""
    def _boom(_db_path):
        raise RuntimeError("disk full")
    monkeypatch.setattr(sh, "_connect", _boom)
    sh.hydrate(db_path="/nonexistent/x.db")  # must not raise


def test_configure_points_default_db_at_hass_config_path(sh):
    class _FakeConfig:
        def path(self, *parts):
            return "/fake/config/" + "/".join(parts)
    class _FakeHass:
        config = _FakeConfig()
    sh.configure(_FakeHass())
    assert sh._DEFAULT_DB == "/fake/config/nova/conversations.db"
