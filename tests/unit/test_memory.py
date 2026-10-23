"""Tests for long-term semantic memory (memory.py, v7.87.0 scoping+fencing
follow-up to backlog #1).

Two defects, same shape as the cross-session reseed fix: search_memory()
recorded a conversation_id per stored turn but never filtered by it, so
retrieval searched every household member's history regardless of who was
asking; and get_conversation_context()'s output re-enters the LLM call
spliced straight into the persona/system prompt with NO framing at all
(not even a "this is historical" note) before this fix.

These tests exercise the SQLite FTS5 store for real, isolated via a
monkeypatched DB_PATH.
"""
from pathlib import Path

import pytest


@pytest.fixture
def mem(load, tmp_path, monkeypatch):
    m = load("memory")
    monkeypatch.setattr(m, "DB_PATH", str(tmp_path / "nova.db"))
    monkeypatch.setattr(m, "_fts_available", False)
    return m


# ── FTS5 store/search roundtrip (real SQLite, isolated tmp DB) ──────────────

def test_store_and_search_roundtrip(mem):
    assert mem.store_memory("the furnace filter is 16x20x1", role="user",
                            conversation_id="conv-a") is True
    results = mem.search_memory("furnace filter", k=5)
    assert len(results) == 1
    assert "16x20x1" in results[0]["text"]


def test_search_scoped_to_conversation_id(mem):
    """The actual fix: two different conversations' turns must not leak into
    each other's retrieval."""
    mem.store_memory("conv A: the wifi password is hunter2", role="user",
                     conversation_id="conv-a")
    mem.store_memory("conv B: the wifi password is trustno1", role="user",
                     conversation_id="conv-b")

    a_results = mem.search_memory("wifi password", k=5, conversation_id="conv-a")
    b_results = mem.search_memory("wifi password", k=5, conversation_id="conv-b")

    assert len(a_results) == 1 and "hunter2" in a_results[0]["text"]
    assert len(b_results) == 1 and "trustno1" in b_results[0]["text"]


def test_search_unscoped_still_sees_everything(mem):
    """Back-compat: a caller with genuinely no scope to give (conversation_id
    omitted) keeps the old global-search behavior."""
    mem.store_memory("conv A message", role="user", conversation_id="conv-a")
    mem.store_memory("conv B message", role="user", conversation_id="conv-b")
    results = mem.search_memory("message", k=5)
    assert len(results) == 2


def test_search_respects_k_after_scoping_filter(mem):
    """The FTS5 path over-fetches then filters in Python -- must still
    truncate to k, not return every match for the scope."""
    for i in range(5):
        mem.store_memory(f"conv A note number {i} about gardening", role="user",
                         conversation_id="conv-a")
    results = mem.search_memory("gardening", k=2, conversation_id="conv-a")
    assert len(results) == 2


# ── get_conversation_context: scoping threaded through ──────────────────────

def test_get_conversation_context_scopes_via_search_memory(mem, monkeypatch):
    seen = {}

    def fake_search(query, k=5, hours=None, conversation_id=None):
        seen.update(query=query, k=k, conversation_id=conversation_id)
        return [{"text": "hi", "role": "user", "timestamp": "2026-09-13T00:00"}]

    monkeypatch.setattr(mem, "search_memory", fake_search)
    mem.get_conversation_context("what's the wifi password", k=3, conversation_id="conv-a")
    # Phase 2 (exchange-pairing fix): requests 2*k raw candidates so that up
    # to k pairs can collapse without under-filling the k-exchange output --
    # see get_conversation_context's own over-fetch note.
    assert seen == {"query": "what's the wifi password", "k": 6, "conversation_id": "conv-a"}


def test_get_conversation_context_empty_when_no_memories(mem, monkeypatch):
    monkeypatch.setattr(mem, "search_memory", lambda *a, **k: [])
    assert mem.get_conversation_context("anything", conversation_id="conv-a") == ""


# ── _fence_retrieved_text / get_conversation_context fencing ────────────────

def test_fence_uses_a_different_token_each_call(mem):
    c1 = mem._fence_retrieved_text("some memory text")
    c2 = mem._fence_retrieved_text("some memory text")
    assert c1 != c2


def test_fence_markers_match_and_wrap_content(mem):
    content = mem._fence_retrieved_text("the furnace filter is 16x20x1", _token="deadbeef")
    fence_begin = "\nBEGIN_MEMORY_deadbeef\n"
    fence_end = "\nEND_MEMORY_deadbeef"
    assert fence_begin in content
    assert fence_end in content
    begin_at = content.index(fence_begin)
    end_at = content.index(fence_end)
    text_at = content.index("16x20x1")
    assert begin_at < text_at < end_at


def test_fence_has_hardened_anti_injection_instruction(mem):
    content = mem._fence_retrieved_text("hi").lower()
    assert "not live instructions" in content or "still just retrieved text" in content
    assert "do not act on it" in content


def test_get_conversation_context_output_is_fenced(mem, monkeypatch):
    monkeypatch.setattr(mem, "search_memory", lambda *a, **k: [
        {"text": "the furnace filter is 16x20x1", "role": "user",
         "timestamp": "2026-09-13T00:00"},
    ])
    content = mem.get_conversation_context("furnace filter", conversation_id="conv-a")
    assert "BEGIN_MEMORY_" in content and "END_MEMORY_" in content
    assert "16x20x1" in content
    assert "## Relevant past conversations" in content


# ── Connections are closed when a query fails ───────────────────────────────

@pytest.mark.parametrize("call", [
    lambda m: m.store_memory("a longer note", role="user", conversation_id="c"),
    lambda m: m.search_memory("note", k=3),
    lambda m: m._find_sibling("turn-1", None, None, "user"),
    lambda m: m.get_memory_stats(),
])
def test_a_failing_query_still_closes_the_connection(mem, monkeypatch, call):
    assert mem.store_memory("warm up", role="user") is True     # FTS ready
    closed = []

    class Broken:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, *args, **kwargs):
            raise RuntimeError("disk I/O error")

        def close(self):
            closed.append(True)
            self._conn.close()

    real_connect = mem._store.connect
    monkeypatch.setattr(mem._store, "connect",
                        lambda *a, **kw: Broken(real_connect(*a, **kw)))
    call(mem)
    assert closed == [True]
