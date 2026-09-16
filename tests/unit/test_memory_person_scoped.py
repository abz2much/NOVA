"""Phase 2 — person-scoped episodic continuity and coherent exchange
retrieval in memory.py.

Same fixture shape as test_memory.py (real, isolated FTS5 for the fallback
path; a hand-rolled fake for the ChromaDB path since chromadb isn't a hard
dependency).
"""
import json

import pytest


@pytest.fixture
def mem(load, tmp_path, monkeypatch):
    m = load("memory")
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "nova.db"))
    monkeypatch.setattr(m, "MEMORY_DIR", str(tmp_path / "nova_memory"))
    monkeypatch.setattr(m, "_chromadb_available", False)
    monkeypatch.setattr(m, "_collection", None)
    monkeypatch.setattr(m, "_fts_available", False)
    return m


def _raw_metadata_rows(mem):
    import sqlite3
    conn = sqlite3.connect(mem.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT content, metadata FROM memory_fts").fetchall()
    conn.close()
    return [(r["content"], json.loads(r["metadata"])) for r in rows]


# ── store_memory: optional metadata, never null, never "None" ───────────────

def test_subject_and_turn_id_omitted_from_metadata_when_not_given(mem):
    mem.store_memory("hello there friend", role="user", conversation_id="conv-a")
    _, meta = _raw_metadata_rows(mem)[0]
    assert "subject" not in meta
    assert "turn_id" not in meta


def test_subject_and_turn_id_present_when_given(mem):
    mem.store_memory("hello there friend", role="user", conversation_id="conv-a",
                      subject="alice", turn_id="42")
    _, meta = _raw_metadata_rows(mem)[0]
    assert meta["subject"] == "alice"
    assert meta["turn_id"] == "42"


def test_turn_id_stored_as_string_not_the_word_none(mem):
    # Guards the exact defect the design called out: never write the
    # literal string "None" -- turn_id must be omitted, not stringified,
    # when the caller has no real id.
    mem.store_memory("hello there friend", role="user", conversation_id="conv-a",
                      turn_id=None)
    _, meta = _raw_metadata_rows(mem)[0]
    assert "turn_id" not in meta
    assert meta.get("turn_id") != "None"


# ── ChromaDB: subject filter ─────────────────────────────────────────────────

class _FakeCollection:
    def __init__(self, docs=None):
        self.last_where = "UNSET"
        self.last_get_where = "UNSET"
        self._docs = docs or []  # list of (id, text, meta) for .get()

    def query(self, query_texts, n_results, where=None):
        self.last_where = where
        return {"documents": [["a match"]], "metadatas": [[{"role": "user", "timestamp": "t"}]],
                "distances": [[0.1]]}

    def get(self, where=None):
        self.last_get_where = where
        docs, metas = [], []
        for _id, text, meta in self._docs:
            if where and "turn_id" in where and meta.get("turn_id") != where["turn_id"]:
                continue
            docs.append(text)
            metas.append(meta)
        return {"documents": docs, "metadatas": metas}


def test_chromadb_subject_filter_added_to_where(mem, monkeypatch):
    fake = _FakeCollection()
    monkeypatch.setattr(mem, "_chromadb_available", True)
    monkeypatch.setattr(mem, "_collection", fake)
    mem.search_memory("query", k=5, subject="alice")
    assert fake.last_where == {"subject": "alice"}


def test_chromadb_sibling_lookup_uses_turn_id_only_not_conversation_id(mem, monkeypatch):
    fake = _FakeCollection(docs=[
        ("id1", "the question", {"role": "user", "timestamp": "t1",
                                  "conversation_id": "conv-A", "subject": "alice", "turn_id": "42"}),
        ("id2", "the answer", {"role": "assistant", "timestamp": "t2",
                                "conversation_id": "conv-A", "subject": "alice", "turn_id": "42"}),
    ])
    monkeypatch.setattr(mem, "_chromadb_available", True)
    monkeypatch.setattr(mem, "_collection", fake)
    sibling = mem._find_sibling("42", hit_conversation_id="conv-A", hit_subject="alice", hit_role="user")
    assert sibling is not None
    assert sibling["text"] == "the answer"
    # The exact lookup used turn_id only -- no conversation_id in the where.
    assert fake.last_get_where == {"turn_id": "42"}


# ── FTS5: subject filter must survive a high-volume over-fetch ──────────────

def test_fts5_subject_filter_survives_more_than_25_higher_ranked_decoys(mem):
    """Empirically proven scenario (see conversation history / design notes):
    the plain over-fetch-then-Python-filter approach truncates via SQL LIMIT
    before the Python filter ever runs, so 25+ higher-ranked rows from
    another subject can exclude the real target entirely. This must not
    happen once the LIKE pre-filter is in place."""
    query_words = "garage code changed recently"
    for i in range(30):
        # Heavy term repetition -> high BM25 rank, all belonging to "bob".
        mem.store_memory(
            f"garage code changed recently garage code changed garage code {i}",
            role="user", conversation_id=f"conv-decoy-{i}", subject="bob",
        )
    # Real target: matches the query, weaker rank (no term stuffing),
    # belongs to "alice".
    mem.store_memory(
        "the garage code changed recently, just so you know",
        role="user", conversation_id="conv-real", subject="alice",
    )

    results = mem.search_memory(query_words, k=3, subject="alice")
    assert len(results) == 1
    assert results[0]["subject"] == "alice"
    assert "just so you know" in results[0]["text"]


def test_fts5_subject_filter_handles_adversarial_values(mem):
    """Embedded quote, backslash, and LIKE-wildcard characters in the
    subject value must not break the escaped pattern or cause a false
    match/miss."""
    tricky = ['alice', 'o"brien', 'jo_ann', '50%off', 'back\\slash']
    for i, subj in enumerate(tricky):
        mem.store_memory(f"garage code message {i}", role="user",
                          conversation_id=f"conv-{i}", subject=subj)
        mem.store_memory(f"garage code decoy {i}", role="user",
                          conversation_id=f"conv-decoy-{i}", subject=subj + "x")
    for subj in tricky:
        results = mem.search_memory("garage code message", k=5, subject=subj)
        assert len(results) == 1, f"failed for subject={subj!r}"
        assert results[0]["subject"] == subj


def test_fts5_sibling_lookup_uses_turn_id_pattern_not_conversation_scoped_query(mem):
    mem.store_memory("the question", role="user", conversation_id="conv-A",
                      subject="alice", turn_id="42")
    mem.store_memory("the answer", role="assistant", conversation_id="conv-A",
                      subject="alice", turn_id="42")
    sibling = mem._find_sibling("42", hit_conversation_id="conv-A", hit_subject="alice", hit_role="user")
    assert sibling is not None
    assert sibling["text"] == "the answer"
    assert sibling["role"] == "assistant"


def test_sibling_lookup_rejects_cross_conversation_and_cross_subject_collision(mem):
    """A coincidental turn_id-string match from a DIFFERENT conversation and
    subject must be rejected -- turn_id alone is not sufficient, the
    conversation_id/subject/role validation is authoritative."""
    mem.store_memory("real question", role="user", conversation_id="conv-A",
                      subject="alice", turn_id="42")
    mem.store_memory("real answer", role="assistant", conversation_id="conv-A",
                      subject="alice", turn_id="42")
    # Coincidental collision: same turn_id string, different conversation/subject.
    mem.store_memory("unrelated message", role="user", conversation_id="conv-B",
                      turn_id="42")

    sibling = mem._find_sibling("42", hit_conversation_id="conv-A", hit_subject="alice", hit_role="user")
    assert sibling is not None
    assert sibling["text"] == "real answer"

    # And the collision row itself must never be returned as alice's sibling.
    assert sibling["text"] != "unrelated message"


def test_missing_sibling_returns_none(mem):
    mem.store_memory("lonely question, no assistant reply stored", role="user",
                      conversation_id="conv-A", subject="alice", turn_id="99")
    sibling = mem._find_sibling("99", hit_conversation_id="conv-A", hit_subject="alice", hit_role="user")
    assert sibling is None


def test_sibling_lookup_never_touches_the_conversations_table(mem, monkeypatch):
    """Pairing must keep working after the original conversations-table row
    is purged -- _find_sibling must never query it at all. Proven by making
    any attempt to import/query the conversations DB raise."""
    mem.store_memory("the question", role="user", conversation_id="conv-A",
                      subject="alice", turn_id="42")
    mem.store_memory("the answer", role="assistant", conversation_id="conv-A",
                      subject="alice", turn_id="42")

    import sys
    poisoned = object()

    class _PoisonedDatabaseModule:
        def __getattr__(self, name):
            raise AssertionError(
                "sibling lookup must never touch database.py / the "
                "conversations table"
            )

    monkeypatch.setitem(sys.modules, "jc.database", _PoisonedDatabaseModule())
    sibling = mem._find_sibling("42", hit_conversation_id="conv-A", hit_subject="alice", hit_role="user")
    assert sibling is not None
    assert sibling["text"] == "the answer"


# ── get_conversation_context: pairing, dedup, fallback, fencing ─────────────

def test_paired_exchange_returned_chronologically_as_one_result(mem):
    mem.store_memory("what's the garage code", role="user", conversation_id="conv-A",
                      subject="alice", turn_id="1")
    mem.store_memory("it's 4471", role="assistant", conversation_id="conv-A",
                      subject="alice", turn_id="1")
    ctx = mem.get_conversation_context("garage code", k=3, conversation_id="conv-A")
    assert "what's the garage code" in ctx
    assert "4471" in ctx
    q_pos = ctx.index("what's the garage code")
    a_pos = ctx.index("4471")
    assert q_pos < a_pos  # user before assistant


def test_duplicate_hits_from_both_halves_collapse_to_one_exchange(mem, monkeypatch):
    """If both the user and assistant records independently match the
    search query (both appear in raw search_memory results), pairing must
    not emit the exchange twice."""
    monkeypatch.setattr(mem, "search_memory", lambda *a, **k: [
        {"text": "what's the garage code", "role": "user", "timestamp": "t1",
         "conversation_id": "conv-A", "subject": "alice", "turn_id": "1"},
        {"text": "it's 4471", "role": "assistant", "timestamp": "t2",
         "conversation_id": "conv-A", "subject": "alice", "turn_id": "1"},
    ])
    monkeypatch.setattr(mem, "_find_sibling", lambda turn_id, **kw: (
        {"text": "it's 4471", "role": "assistant", "timestamp": "t2"}
        if kw.get("hit_role") == "user" else
        {"text": "what's the garage code", "role": "user", "timestamp": "t1"}
    ))
    ctx = mem.get_conversation_context("garage code", k=3, conversation_id="conv-A")
    assert ctx.count("4471") == 1
    assert ctx.count("what's the garage code") == 1


def test_legacy_record_without_turn_id_keeps_single_message_format(mem):
    mem.store_memory("a legacy message with no turn_id at all", role="user",
                      conversation_id="conv-A")
    ctx = mem.get_conversation_context("legacy message", k=3, conversation_id="conv-A")
    assert "a legacy message with no turn_id at all" in ctx


def test_missing_sibling_falls_back_to_single_message_format(mem):
    mem.store_memory("orphaned turn with a turn_id but no assistant reply",
                      role="user", conversation_id="conv-A", subject="alice", turn_id="7")
    ctx = mem.get_conversation_context("orphaned turn", k=3, conversation_id="conv-A")
    assert "orphaned turn with a turn_id but no assistant reply" in ctx


def test_conversation_scoped_search_tried_before_subject_fallback(mem, monkeypatch):
    calls = []

    def fake_search(query, k=5, hours=None, conversation_id=None, subject=None):
        calls.append({"conversation_id": conversation_id, "subject": subject})
        if conversation_id:
            return [{"text": "conv-scoped hit", "role": "user", "timestamp": "t",
                      "conversation_id": conversation_id, "subject": None, "turn_id": None}]
        return []

    monkeypatch.setattr(mem, "search_memory", fake_search)
    ctx = mem.get_conversation_context("q", k=3, conversation_id="conv-A", subject="alice")
    assert "conv-scoped hit" in ctx
    # Only ONE call was made -- conversation-scoped succeeded, so the
    # subject fallback must never have been tried (never combined/unioned).
    assert len(calls) == 1
    assert calls[0] == {"conversation_id": "conv-A", "subject": None}


def test_subject_fallback_tried_only_when_conversation_scoped_is_empty(mem, monkeypatch):
    calls = []

    def fake_search(query, k=5, hours=None, conversation_id=None, subject=None):
        calls.append({"conversation_id": conversation_id, "subject": subject})
        if subject:
            return [{"text": "subject-scoped hit", "role": "user", "timestamp": "t",
                      "conversation_id": "conv-OLD", "subject": subject, "turn_id": None}]
        return []

    monkeypatch.setattr(mem, "search_memory", fake_search)
    ctx = mem.get_conversation_context("q", k=3, conversation_id="conv-A", subject="alice")
    assert "subject-scoped hit" in ctx
    assert len(calls) == 2
    assert calls[0] == {"conversation_id": "conv-A", "subject": None}
    assert calls[1] == {"conversation_id": None, "subject": "alice"}


def test_no_subject_fallback_when_subject_is_none(mem, monkeypatch):
    calls = []

    def fake_search(query, k=5, hours=None, conversation_id=None, subject=None):
        calls.append(1)
        return []

    monkeypatch.setattr(mem, "search_memory", fake_search)
    ctx = mem.get_conversation_context("q", k=3, conversation_id="conv-A", subject=None)
    assert ctx == ""
    assert len(calls) == 1  # no second (subject) call attempted at all


def test_paired_exchange_output_is_fenced_exactly_once(mem, monkeypatch):
    # Same deterministic-token approach as
    # test_memory.py::test_fence_markers_match_and_wrap_content, applied via
    # a spy so real production call sites (get_conversation_context, which
    # never passes _token) still exercise the real fence() call, just once,
    # over the WHOLE paired block rather than per line.
    calls = []
    real_fence = mem._fence_retrieved_text

    def spy(content, **kw):
        calls.append(content)
        return real_fence(content, _token="deadbeef")

    monkeypatch.setattr(mem, "_fence_retrieved_text", spy)

    mem.store_memory("what's the garage code", role="user", conversation_id="conv-A",
                      subject="alice", turn_id="1")
    mem.store_memory("it's 4471", role="assistant", conversation_id="conv-A",
                      subject="alice", turn_id="1")
    ctx = mem.get_conversation_context("garage code", k=3, conversation_id="conv-A")

    assert len(calls) == 1  # fenced exactly once, not once per line
    assert "what's the garage code" in calls[0] and "4471" in calls[0]

    begin_marker = "\nBEGIN_MEMORY_deadbeef\n"
    end_marker = "\nEND_MEMORY_deadbeef"
    assert ctx.count(begin_marker) == 1
    assert ctx.count(end_marker) == 1
    begin_at = ctx.index(begin_marker)
    end_at = ctx.index(end_marker)
    q_at = ctx.index("what's the garage code")
    a_at = ctx.index("4471")
    assert begin_at < q_at < a_at < end_at
