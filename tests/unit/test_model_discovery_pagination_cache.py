"""Pagination-cursor safety and the bounded, generation-guarded model-list
cache behind ``nova/list_models`` (providers.discovery).

These are the pure, network-independent helpers only — the aiohttp fetch is
covered end-to-end in tests/integration/test_websocket_security.py (PHACC).
Redirect handling is covered in test_model_discovery_redirects.py.
"""
from __future__ import annotations

import asyncio

import pytest


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def d(load):
    return load("providers.discovery")


@pytest.fixture
def m(d):
    """The pagination helpers under the names this suite has always used."""
    from jc.providers.registry import DESCRIPTORS
    return {
        "_next_page_cursor": d.next_page_cursor,
        "_page_query_params": d.page_query_params,
        "_MAX_DISCOVERY_PAGES": d.MAX_DISCOVERY_PAGES,
        "_MAX_DISCOVERY_MODELS": d.MAX_DISCOVERY_MODELS,
        "_MAX_CURSOR_LEN": d.MAX_CURSOR_LEN,
        "_PAGINATION_STYLE": {pid: desc.discovery.pagination
                              for pid, desc in DESCRIPTORS.items()
                              if desc.discovery.pagination},
    }


@pytest.fixture
def clock():
    return _Clock()


@pytest.fixture
def cache(d, clock):
    return d.ModelListCache(clock=clock)


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


# ── cache: keys, hit, miss, expiry ──────────────────────────────────────────────

GROQ = ("groq", "https://api.groq.com/openai/v1/models")
OPENAI = ("openai", "https://api.openai.com/v1/models")


def _set(cache, key, value):
    return cache.set(key, value, generation=cache.generation(key[0]))


def test_cache_miss_returns_none(cache):
    assert cache.get(GROQ) is None


def test_cache_set_then_get_hits(cache):
    assert _set(cache, GROQ, (["a", "b"], False, []))
    assert cache.get(GROQ) == (["a", "b"], False, [])


def test_cache_entry_expires_after_ttl(cache, clock):
    _set(cache, GROQ, (["a"], False, []))
    clock.now += cache.ttl + 1
    assert cache.get(GROQ) is None
    assert len(cache) == 0     # expired entry is evicted, not just ignored


def test_cache_distinguishes_distinct_custom_endpoints(cache):
    _set(cache, ("custom", "https://one.example.test/v1/models"), (["a"], False, []))
    assert cache.get(("custom", "https://two.example.test/v1/models")) is None


def test_cache_size_is_bounded(cache, clock):
    for i in range(cache.max_entries + 10):
        clock.now += 1
        _set(cache, ("custom", f"https://host{i}.test/v1/models"), (["x"], False, []))
    assert len(cache) <= cache.max_entries


def test_cache_limits_are_small(d):
    assert d.MODEL_CACHE_TTL == 300.0
    assert d.MODEL_CACHE_MAX_ENTRIES == 32


# ── cache: invalidation ─────────────────────────────────────────────────────────

def test_invalidate_specific_provider_leaves_others(cache):
    _set(cache, GROQ, (["a"], False, []))
    _set(cache, OPENAI, (["b"], False, []))
    cache.invalidate("groq")
    assert cache.get(GROQ) is None
    assert cache.get(OPENAI) == (["b"], False, [])


def test_invalidate_all_clears_everything(cache):
    _set(cache, GROQ, (["a"], False, []))
    _set(cache, OPENAI, (["b"], False, []))
    cache.invalidate(None)
    assert len(cache) == 0
    assert cache._inflight == {}


def test_module_invalidate_delegates_to_the_shared_cache(d):
    d.MODEL_CACHE.set(GROQ, (["a"], False, []), generation=d.MODEL_CACHE.generation("groq"))
    d.invalidate_model_cache("groq")
    assert d.MODEL_CACHE.get(GROQ) is None


# ── cache: generation guard (defect 8) ──────────────────────────────────────────

def test_fetch_started_before_invalidation_cannot_repopulate(cache):
    before = cache.generation("groq")
    cache.invalidate("groq")                       # credential changed mid-fetch
    assert cache.set(GROQ, (["stale"], False, []), generation=before) is False
    assert cache.get(GROQ) is None


def test_global_invalidation_also_blocks_a_stale_fetch(cache):
    before = cache.generation("openai")
    cache.invalidate()
    assert cache.set(OPENAI, (["stale"], False, []), generation=before) is False


def test_other_providers_generation_is_unaffected(cache):
    before = cache.generation("openai")
    cache.invalidate("groq")
    assert cache.set(OPENAI, (["fresh"], False, []), generation=before) is True


@pytest.mark.asyncio
async def test_concurrent_callers_share_one_fetch(cache):
    calls = 0
    gate = asyncio.Event()

    async def fetch():
        nonlocal calls
        calls += 1
        await gate.wait()
        return (["m"], False, [])

    first = asyncio.ensure_future(cache.dedupe(GROQ, fetch))
    second = asyncio.ensure_future(cache.dedupe(GROQ, fetch))
    await asyncio.sleep(0)
    gate.set()
    assert await first == await second == (["m"], False, [])
    assert calls == 1
    assert cache._inflight == {}


@pytest.mark.asyncio
async def test_caller_after_invalidation_does_not_join_the_stale_fetch(cache):
    """The race: a fetch with the old credential is in flight, the
    credential changes, and a new request arrives. The new request must
    start its own fetch, and the stale one must not be cached."""
    gate = asyncio.Event()
    fetched = []

    async def stale():
        await gate.wait()
        fetched.append("stale")
        return (["old-model"], False, [])

    async def fresh():
        fetched.append("fresh")
        return (["new-model"], False, [])

    stale_generation = cache.generation("groq")
    stale_task = asyncio.ensure_future(cache.dedupe(GROQ, stale))
    await asyncio.sleep(0)

    cache.invalidate("groq")
    fresh_generation = cache.generation("groq")
    assert await cache.dedupe(GROQ, fresh) == (["new-model"], False, [])
    assert cache.set(GROQ, (["new-model"], False, []), generation=fresh_generation)

    gate.set()
    stale_result = await stale_task
    assert cache.set(GROQ, stale_result, generation=stale_generation) is False
    assert cache.get(GROQ) == (["new-model"], False, [])
    assert fetched == ["fresh", "stale"]


@pytest.mark.asyncio
async def test_a_failed_fetch_is_not_cached_and_clears_inflight(cache):
    async def boom():
        raise RuntimeError("401")

    with pytest.raises(RuntimeError):
        await cache.dedupe(GROQ, boom)
    assert cache._inflight == {}
    assert cache.get(GROQ) is None


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_the_shared_fetch(cache):
    gate = asyncio.Event()

    async def fetch():
        await gate.wait()
        return (["m"], False, [])

    first = asyncio.ensure_future(cache.dedupe(GROQ, fetch))
    second = asyncio.ensure_future(cache.dedupe(GROQ, fetch))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    gate.set()
    assert await second == (["m"], False, [])
    with pytest.raises(asyncio.CancelledError):
        await first
