"""Pagination-cursor safety and the bounded server-side cache for
``nova/list_models`` (Phase 3, v7.108.0).

These are the pure, network-independent helpers only — _fetch_models itself
does real aiohttp I/O and is covered end-to-end in
tests/integration/test_websocket_security.py (PHACC), same split as Phase 1's
_resolve_model_discovery_request / test_websocket_security.py.
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "websocket.py"


def _load():
    wanted = {
        "_PAGINATION_STYLE",
        "_MAX_DISCOVERY_PAGES",
        "_MAX_DISCOVERY_MODELS",
        "_MAX_CURSOR_LEN",
        "_next_page_cursor",
        "_page_query_params",
        "_MODEL_CACHE_TTL",
        "_MODEL_CACHE_MAX_ENTRIES",
        "_MODEL_CACHE",
        "_MODEL_CACHE_INFLIGHT",
        "_model_cache_key",
        "_model_cache_get",
        "_model_cache_set",
        "invalidate_model_cache",
    }
    tree = ast.parse(SRC.read_text())
    nodes = []
    found = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {t.id for t in targets if isinstance(t, ast.Name)}
            if names & wanted:
                nodes.append(node)
                found.update(names & wanted)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            nodes.append(node)
            found.add(node.name)

    missing = wanted - found
    assert not missing, f"pagination/cache helpers missing: {sorted(missing)}"

    namespace = {"time": time, "Optional": __import__("typing").Optional, "Any": __import__("typing").Any}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SRC), "exec"), namespace)
    return namespace


@pytest.fixture
def m():
    return _load()


# ── pagination cursor safety ──────────────────────────────────────────────────

def test_gemini_next_page_cursor_accepted(m):
    cursor = m["_next_page_cursor"]("pageToken", {"nextPageToken": "opaque-token-abc"})
    assert cursor == "opaque-token-abc"


def test_gemini_no_next_page_token_stops(m):
    assert m["_next_page_cursor"]("pageToken", {"models": []}) is None


def test_anthropic_next_page_cursor_requires_has_more_true(m):
    cursor = m["_next_page_cursor"]("after_id", {"has_more": True, "last_id": "model_123"})
    assert cursor == "model_123"


@pytest.mark.parametrize("data", [
    {"has_more": False, "last_id": "model_123"},
    {"has_more": "true", "last_id": "model_123"},   # not literally True
    {"last_id": "model_123"},                        # has_more missing
])
def test_anthropic_next_page_cursor_refuses_when_has_more_not_true(m, data):
    assert m["_next_page_cursor"]("after_id", data) is None


@pytest.mark.parametrize("style,data", [
    ("pageToken", {"nextPageToken": 12345}),               # wrong type
    ("pageToken", {"nextPageToken": ""}),                   # empty
    ("pageToken", {"nextPageToken": "   "}),                # blank after strip
    ("pageToken", {"nextPageToken": "x" * 3000}),            # oversized
    ("pageToken", {"nextPageToken": "https://attacker.invalid/v1/models?page=2"}),  # looks like a URL
    ("after_id", {"has_more": True, "last_id": None}),
    ("after_id", {"has_more": True, "last_id": ["not", "a", "string"]}),
    (None, {"nextPageToken": "abc"}),                        # unpaginated provider
    ("pageToken", "not-a-dict"),
    ("pageToken", None),
])
def test_malformed_pagination_data_stops_safely(m, style, data):
    assert m["_next_page_cursor"](style, data) is None


def test_unknown_pagination_style_returns_none(m):
    assert m["_next_page_cursor"]("some-future-style", {"next": "x"}) is None


# ── query params never leak a full URL, only opaque values ───────────────────

def test_page_query_params_gemini_first_page(m):
    assert m["_page_query_params"]("pageToken", None) == {"pageSize": "100"}


def test_page_query_params_gemini_subsequent_page(m):
    assert m["_page_query_params"]("pageToken", "tok-2") == {"pageSize": "100", "pageToken": "tok-2"}


def test_page_query_params_anthropic_subsequent_page(m):
    assert m["_page_query_params"]("after_id", "model_5") == {"limit": "100", "after_id": "model_5"}


def test_page_query_params_unpaginated_provider_is_empty(m):
    assert m["_page_query_params"](None, None) == {}


# ── bounded limits ─────────────────────────────────────────────────────────────

def test_pagination_limits_are_strict_and_small(m):
    assert 1 <= m["_MAX_DISCOVERY_PAGES"] <= 10
    assert 50 <= m["_MAX_DISCOVERY_MODELS"] <= 2000
    assert m["_MAX_CURSOR_LEN"] <= 4096


def test_only_gemini_and_anthropic_are_paginated(m):
    # Explicitly NOT adding provider names just to grow this map — pagination
    # is only for the two providers with a real, documented paginated API.
    assert set(m["_PAGINATION_STYLE"]) == {"gemini", "anthropic"}


# ── cache: key never includes a credential ────────────────────────────────────

def test_cache_key_is_provider_and_url_only(m):
    key = m["_model_cache_key"]("groq", "https://api.groq.com/openai/v1/models")
    assert key == ("groq", "https://api.groq.com/openai/v1/models")
    assert "gsk_" not in str(key)  # nothing credential-shaped could be in it


def test_cache_key_distinguishes_distinct_custom_endpoints(m):
    a = m["_model_cache_key"]("custom", "https://one.example.test/v1/models")
    b = m["_model_cache_key"]("custom", "https://two.example.test/v1/models")
    assert a != b


# ── cache: hit, miss, expiry ───────────────────────────────────────────────────

def test_cache_miss_returns_none(m):
    assert m["_model_cache_get"](("groq", "https://api.groq.com/openai/v1/models")) is None


def test_cache_set_then_get_hits(m):
    key = ("groq", "https://api.groq.com/openai/v1/models")
    m["_model_cache_set"](key, ["a", "b"], False)
    assert m["_model_cache_get"](key) == (["a", "b"], False, [])


def test_cache_entry_expires_after_ttl(m, monkeypatch):
    key = ("groq", "https://api.groq.com/openai/v1/models")
    m["_model_cache_set"](key, ["a"], False)
    # Jump the clock past the TTL rather than sleeping in a test — capture the
    # real value first, since m["time"] IS the process's time module (a
    # singleton), so patching it in place would make the replacement call
    # itself recursively.
    future = time.monotonic() + m["_MODEL_CACHE_TTL"] + 1
    monkeypatch.setattr(m["time"], "monotonic", lambda: future)
    assert m["_model_cache_get"](key) is None
    assert key not in m["_MODEL_CACHE"]  # expired entry is evicted, not just ignored


# ── cache: bounded size ────────────────────────────────────────────────────────

def test_cache_size_is_bounded(m):
    cap = m["_MODEL_CACHE_MAX_ENTRIES"]
    for i in range(cap + 10):
        m["_model_cache_set"]((f"custom", f"https://host{i}.test/v1/models"), ["x"], False)
    assert len(m["_MODEL_CACHE"]) <= cap


# ── cache: invalidation ─────────────────────────────────────────────────────────

def test_invalidate_specific_provider_leaves_others(m):
    m["_model_cache_set"](("groq", "https://api.groq.com/openai/v1/models"), ["a"], False)
    m["_model_cache_set"](("openai", "https://api.openai.com/v1/models"), ["b"], False)
    m["invalidate_model_cache"]("groq")
    assert m["_model_cache_get"](("groq", "https://api.groq.com/openai/v1/models")) is None
    assert m["_model_cache_get"](("openai", "https://api.openai.com/v1/models")) == (["b"], False, [])


def test_invalidate_all_clears_everything(m):
    m["_model_cache_set"](("groq", "https://api.groq.com/openai/v1/models"), ["a"], False)
    m["_model_cache_set"](("openai", "https://api.openai.com/v1/models"), ["b"], False)
    m["invalidate_model_cache"](None)
    assert m["_MODEL_CACHE"] == {}
    assert m["_MODEL_CACHE_INFLIGHT"] == {}


def test_invalidate_matches_only_the_named_providers_url(m):
    m["_model_cache_set"](("custom", "https://one.example.test/v1/models"), ["a"], False)
    m["invalidate_model_cache"]("custom")
    assert m["_model_cache_get"](("custom", "https://one.example.test/v1/models")) is None
