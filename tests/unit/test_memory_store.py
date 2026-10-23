"""The long-term memory store: what a stored turn looks like on disk, what a
search returns, the line format of the context block, and the stats.

It runs against a real SQLite FTS5 table in a temporary folder. The scoping,
fencing and exchange pairing have their own files (test_memory.py and
test_memory_person_scoped.py).
"""
import json
import logging
import sqlite3
from datetime import datetime

import pytest


@pytest.fixture
def mem(load, tmp_path, monkeypatch):
    m = load("memory")
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "nova.db"))
    monkeypatch.setattr(m, "_fts_available", False)
    return m


def _rows(mem):
    conn = sqlite3.connect(mem.DB_PATH)
    try:
        return conn.execute(
            "SELECT content, metadata, timestamp FROM memory_fts ORDER BY rowid").fetchall()
    finally:
        conn.close()


# ── where the database is ────────────────────────────────────────────────────

def test_the_database_path_defaults_to_the_standard_one(mem, load, monkeypatch):
    monkeypatch.setattr(mem, "DB_PATH", None)
    assert mem._db_path() == load("paths").nova_db()
    monkeypatch.setattr(mem, "DB_PATH", "/tmp/elsewhere.db")
    assert mem._db_path() == "/tmp/elsewhere.db"


# ── storing ──────────────────────────────────────────────────────────────────

def test_a_stored_turn_is_one_row_with_json_metadata(mem):
    assert mem.store_memory("the porch light flickers at night", role="assistant",
                            device_id="dev1", conversation_id="conv1") is True
    ((content, metadata, stamp),) = _rows(mem)
    assert content == "the porch light flickers at night"
    meta = json.loads(metadata)
    assert list(meta) == ["role", "device_id", "conversation_id", "timestamp"]
    assert meta["role"] == "assistant" and meta["device_id"] == "dev1"
    assert meta["conversation_id"] == "conv1"
    assert meta["timestamp"] == stamp
    assert datetime.fromisoformat(stamp).tzinfo is None


def test_blank_ids_are_stored_as_empty_strings_and_the_role_defaults_to_user(mem):
    mem.store_memory("something worth remembering", device_id=None, conversation_id=None)
    meta = json.loads(_rows(mem)[0][1])
    assert meta == {"role": "user", "device_id": "", "conversation_id": "",
                    "timestamp": meta["timestamp"]}


def test_subject_and_turn_id_are_added_only_when_given(mem):
    mem.store_memory("first message here", subject="alex", turn_id=42)
    mem.store_memory("second message here", subject="", turn_id="")
    first, second = (json.loads(r[1]) for r in _rows(mem))
    assert list(first) == ["role", "device_id", "conversation_id", "timestamp",
                           "subject", "turn_id"]
    assert first["subject"] == "alex" and first["turn_id"] == "42"
    assert "subject" not in second and "turn_id" not in second


@pytest.mark.parametrize("text", ["", None, "    ", "abcd", "  abcd  "])
def test_text_under_five_characters_is_not_stored(mem, text):
    assert mem.store_memory(text) is False
    assert mem.store_memory(text, role="assistant") is False
    assert mem._fts_available is False


def test_five_characters_is_enough_and_the_text_is_stored_as_given(mem):
    assert mem.store_memory("  hello ") is True
    assert _rows(mem)[0][0] == "  hello "


def test_the_table_is_set_up_on_first_use_and_logged_once(mem, caplog):
    with caplog.at_level(logging.INFO, logger=mem._LOGGER.name):
        mem.store_memory("the first stored turn")
        mem.store_memory("the second stored turn")
    assert mem._fts_available is True
    assert [r.getMessage() for r in caplog.records] == ["Nova memory: FTS5 initialized"]
    assert len(_rows(mem)) == 2


def test_a_failed_setup_stores_nothing_and_warns(mem, monkeypatch, caplog):
    def boom(*a, **k):
        raise sqlite3.OperationalError("unable to open")
    monkeypatch.setattr(mem._store, "connect", boom)
    with caplog.at_level(logging.WARNING, logger=mem._LOGGER.name):
        assert mem.store_memory("this will not be stored") is False
    assert mem._fts_available is False
    assert [r.getMessage() for r in caplog.records] == [
        "Nova memory: FTS5 init failed: unable to open"]


def test_init_reports_whether_it_worked(mem, monkeypatch):
    assert mem._init_fts() is True
    monkeypatch.setattr(mem._store, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    assert mem._init_fts() is False


def test_a_failed_insert_is_a_quiet_false(mem, monkeypatch, caplog):
    mem._init_fts()

    class Conn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        def close(self):
            pass

    monkeypatch.setattr(mem._store, "connect", lambda *a, **k: Conn())
    with caplog.at_level(logging.DEBUG, logger=mem._LOGGER.name):
        assert mem.store_memory("a turn that cannot be written") is False
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "FTS5 store failed: database is locked")]


# ── searching ────────────────────────────────────────────────────────────────

def test_a_hit_has_these_fields(mem):
    mem.store_memory("the garage door sticks in winter", role="assistant",
                     conversation_id="c1", subject="alex", turn_id=7)
    (hit,) = mem.search_memory("garage door")
    assert list(hit) == ["text", "role", "timestamp", "score", "conversation_id",
                         "subject", "turn_id"]
    assert hit["text"] == "the garage door sticks in winter"
    assert hit["role"] == "assistant"
    assert hit["timestamp"] == json.loads(_rows(mem)[0][1])["timestamp"]
    assert hit["conversation_id"] == "c1" and hit["subject"] == "alex"
    assert hit["turn_id"] == "7"
    assert isinstance(hit["score"], (int, float)) and hit["score"] >= 0
    assert hit["score"] == round(hit["score"], 3)


def test_missing_ids_come_back_as_none(mem):
    mem.store_memory("the thermostat is in the hall")
    (hit,) = mem.search_memory("thermostat")
    assert (hit["conversation_id"], hit["subject"], hit["turn_id"]) == (None, None, None)


@pytest.mark.parametrize("query", ["", None])
def test_an_empty_query_finds_nothing_and_touches_nothing(mem, query):
    assert mem.search_memory(query) == []
    assert mem._fts_available is False


def test_no_match_gives_an_empty_list(mem):
    mem.store_memory("the thermostat is in the hall")
    assert mem.search_memory("zebra") == []


def test_any_of_the_first_eight_words_can_match(mem):
    mem.store_memory("alpha only here")
    mem.store_memory("beta only here")
    texts = {h["text"] for h in mem.search_memory("alpha beta", k=5)}
    assert texts == {"alpha only here", "beta only here"}


def test_only_the_first_eight_words_of_a_query_count(mem):
    mem.store_memory("needle in the stack")
    query = "one two three four five six seven eight needle"
    assert mem.search_memory(query) == []
    assert len(mem.search_memory("needle one two three four five six seven")) == 1


def test_at_most_k_hits_come_back(mem):
    for n in range(6):
        mem.store_memory(f"kitchen note number {n}")
    assert len(mem.search_memory("kitchen", k=3)) == 3
    assert len(mem.search_memory("kitchen", k=10)) == 6
    assert mem.search_memory("kitchen", k=0) == []


def test_a_row_with_unreadable_metadata_is_still_a_hit_when_unscoped(mem):
    mem._init_fts()
    conn = sqlite3.connect(mem.DB_PATH)
    conn.execute("INSERT INTO memory_fts (content, metadata, timestamp) VALUES (?, ?, ?)",
                 ("legacy kettle note", "not json", "2026-01-01T00:00:00"))
    conn.commit()
    conn.close()
    (hit,) = mem.search_memory("kettle")
    assert hit["text"] == "legacy kettle note"
    assert (hit["role"], hit["timestamp"]) == ("", "")
    assert mem.search_memory("kettle", conversation_id="c1") == []
    assert mem.search_memory("kettle", subject="alex") == []


def test_a_malformed_match_query_is_a_quiet_empty_list(mem, caplog):
    mem.store_memory("the kettle is old")
    with caplog.at_level(logging.DEBUG, logger=mem._LOGGER.name):
        assert mem.search_memory('"unterminated') == []
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["FTS5 search failed"]


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, escaped", [
    ("plain", "plain"), ("50%", "50\\%"), ("a_b", "a\\_b"), ("a\\b", "a\\\\b"),
    ("%_\\", "\\%\\_\\\\"), ("", "")])
def test_like_wildcards_are_escaped(mem, raw, escaped):
    assert mem._like_escape(raw) == escaped


def test_the_metadata_pattern_matches_the_json_fragment(mem):
    assert mem._metadata_like_pattern("subject", "alex") == '%"subject": "alex"%'
    assert mem._metadata_like_pattern("turn_id", "a_b%") == '%"turn\\_id": "a\\_b\\%"%'
    assert mem._metadata_like_pattern("subject", 'q"x') == '%"subject": "q\\\\"x"%'


def test_a_memory_line_trims_the_time_and_the_text(mem):
    line = mem._format_memory_line({
        "timestamp": "2026-03-01T10:20:30.123456", "role": "user", "text": "x" * 400})
    assert line == "[2026-03-01T10:20] user: " + "x" * 300
    assert mem._format_memory_line({}) == "[] : "
    assert mem._format_memory_line({"text": "hi", "role": "assistant"}) == \
        "[] assistant: hi"


def test_the_fence_asks_the_shared_fencer_for_a_memory_block(mem, monkeypatch, load):
    seen = {}

    def fence(content, **kw):
        seen["content"], seen["kw"] = content, kw
        return "FENCED"

    monkeypatch.setattr(load("prompt_fence"), "fence", fence)
    assert mem._fence_retrieved_text("body", _token="tok") == "FENCED"
    assert seen == {"content": "body", "kw": {
        "label": "MEMORY", "noun": "are snippets retrieved from past conversations",
        "callback_noun": "retrieved text", "_token": "tok"}}


# ── the context block ────────────────────────────────────────────────────────

def test_the_context_block_lists_the_hits_under_a_heading_inside_the_fence(
        mem, monkeypatch):
    monkeypatch.setattr(mem, "_fence_retrieved_text", lambda text: f"<<{text}>>")
    monkeypatch.setattr(mem, "search_memory", lambda q, k=5, conversation_id=None,
                        subject=None: [
        {"text": "first", "role": "user", "timestamp": "2026-03-01T10:20:30"},
        {"text": "second", "role": "assistant", "timestamp": "2026-03-01T10:21:00"}])
    assert mem.get_conversation_context("anything") == (
        "<<## Relevant past conversations\n"
        "[2026-03-01T10:20] user: first\n"
        "[2026-03-01T10:21] assistant: second>>")


def test_no_hits_gives_no_block_at_all(mem, monkeypatch):
    monkeypatch.setattr(mem, "search_memory", lambda *a, **k: [])
    assert mem.get_conversation_context("anything") == ""
    assert mem.get_conversation_context("anything", subject="alex") == ""


def test_end_to_end_the_block_holds_the_stored_turn(mem):
    mem.store_memory("the furnace filter is 16x20x1", role="user", conversation_id="c1")
    out = mem.get_conversation_context("furnace filter", conversation_id="c1")
    assert "## Relevant past conversations" in out
    assert "user: the furnace filter is 16x20x1" in out
    assert mem.get_conversation_context("furnace filter", conversation_id="c2") == ""


# ── stats ────────────────────────────────────────────────────────────────────

def test_stats_with_an_empty_store(mem):
    assert mem.get_memory_stats() == {"backend": "fts5", "total_memories": 0}
    assert list(mem.get_memory_stats()) == ["backend", "total_memories"]


def test_stats_count_what_is_stored(mem):
    for n in range(3):
        mem.store_memory(f"remembered turn number {n}")
    assert mem.get_memory_stats() == {"backend": "fts5", "total_memories": 3}


def test_stats_without_a_working_store_say_none(mem, monkeypatch):
    monkeypatch.setattr(mem._store, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    assert mem.get_memory_stats() == {"backend": "none", "total_memories": 0}


def test_stats_survive_a_failing_count(mem, monkeypatch):
    mem._init_fts()

    class Conn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("locked")

        def close(self):
            pass

    monkeypatch.setattr(mem._store, "connect", lambda *a, **k: Conn())
    assert mem.get_memory_stats() == {"backend": "none", "total_memories": 0}
