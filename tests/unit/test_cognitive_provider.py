"""Unreadable provider replies are provider failures (Phase 8, PROV-004/008).

A reply that is not a valid decision falls back to the existing Local Mind
procedure, is never learned into the reasoning cache, never makes a repeat
of the same event read learned silence, adds no retry and no extra provider
call, and never leaks the reply's words into logs. Cancellation propagates.

Focused run:
    python -m pytest tests/unit/test_cognitive_provider.py -q
"""
import asyncio
import logging

import pytest


@pytest.fixture
def prov(load):
    return load("cognitive.provider")


@pytest.fixture
def m(load):
    return load("cognitive.models")


# ── Validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,kind", [
    ("the service is overloaded", "unreadable"),
    ("", "unreadable"),
    ("[1, 2]", "unreadable"),
    ('"speak"', "unreadable"),
    ('{"reason": "x"}', "incomplete"),
    ('{"speak": "false"}', "invalid_field"),
    ('{"speak": 1, "message": "hi"}', "invalid_field"),
    ('{"speak": true}', "empty_message"),
    ('{"speak": true, "message": "   "}', "empty_message"),
    ('{"speak": true, "message": 42}', "empty_message"),
    (None, "unreadable"),
])
def test_unusable_replies_are_failures(prov, m, raw, kind):
    out = prov.parse_reply(raw, classifier_urgency="medium")
    assert isinstance(out, m.ProviderFailure)
    assert out.kind == kind
    if isinstance(raw, str) and raw:
        assert raw not in out.detail


@pytest.mark.parametrize("raw,speak,urgency", [
    ('{"speak": false, "reason": "routine"}', False, None),
    ('```json\n{"speak": false}\n```', False, None),
    ('Sure: {"speak": true, "message": "Sir, the cellar window.", "urgency": "high"}', True, "high"),
    ('{"speak": true, "message": "Sir.", "urgency": "extreme"}', True, "medium"),
])
def test_valid_replies_become_validated_cacheable_decisions(prov, m, load, raw, speak, urgency):
    policy = load("cognitive.cache_policy")
    out = prov.parse_reply(raw, classifier_urgency="medium")
    assert isinstance(out, m.Decision)
    assert out.speak is speak and out.urgency == urgency
    assert out.validated and out.provider_used and out.origin == m.ORIGIN_PROVIDER
    assert policy.may_cache(out)


def test_cache_policy_rejects_everything_but_validated_provider_decisions(m, load):
    policy = load("cognitive.cache_policy")
    ph = m.Phrase.of("lead_in", sentence="x")
    rejected = [
        m.silent("x", "rule"),
        m.speak("x", "high", ph).with_message("Sir, x."),
        m.Decision(m.ACTION_SILENT, "x", origin=m.ORIGIN_LOCAL_MIND),
        m.Decision(m.ACTION_SILENT, "x", origin=m.ORIGIN_CACHE),
        m.Decision(m.ACTION_SILENT, "x", origin=m.ORIGIN_FALLBACK),
        m.Decision(m.ACTION_SILENT, "x", origin=m.ORIGIN_PROVIDER, provider_used=True,
                   provider_failure="unreadable"),
        m.Decision(m.ACTION_SPEAK, "x", origin=m.ORIGIN_PROVIDER, provider_used=True,
                   cacheable=True, urgency="high", message=""),
        m.Decision(m.ACTION_DEFER, "x", origin=m.ORIGIN_PROVIDER, provider_used=True,
                   cacheable=True),
        None,
    ]
    assert [policy.may_cache(d) for d in rejected] == [False] * len(rejected)


# ── The decision path ───────────────────────────────────────────────────────

@pytest.fixture
def isolated(load, connectivity, monkeypatch):
    rl = load("reasoning_loop")
    cache = load("reasoning_cache")
    coord = load("cognitive.coordinator")
    lm = load("local_mind")
    connectivity.reset()
    monkeypatch.setattr(cache, "_cache", {})
    monkeypatch.setattr(cache, "_loaded", True)
    monkeypatch.setattr(cache, "save", lambda: None)
    monkeypatch.setattr(coord, "_holds", type(coord._holds)())
    monkeypatch.setattr(rl, "_rich_mode", lambda hass: False)
    monkeypatch.setattr(lm, "_connect", lambda: None)
    monkeypatch.setattr(lm, "_recent_events", {})
    monkeypatch.setattr(lm, "_hist_cache", {})
    monkeypatch.setattr(lm, "_days_cache", (0.0, 0.0))
    writes = []
    real_remember = cache.remember
    monkeypatch.setattr(cache, "remember",
                        lambda *a, **k: writes.append(a) or real_remember(*a, **k))
    yield rl, cache, coord, writes
    connectivity.reset()


def _decide(rl, hass, provider, **over):
    kw = dict(honorific="sir",
              event_summary="Cellar Window (binary_sensor.cellar_window) changed from off to on",
              home_state_summary="", classifier_urgency="medium", classifier_category="general",
              recent_announcements=[], anyone_home=False,
              entity_id="binary_sensor.cellar_window", device_class="window",
              from_state="off", to_state="on", friendly_name="Cellar Window")
    kw.update(over)
    return rl.decide(hass, provider, **kw)


async def test_unreadable_reply_falls_back_to_the_local_mind(isolated, fake_hass,
                                                             provider_factory, caplog):
    rl, cache, _, writes = isolated
    secret = "the service is overloaded, token=abc123"
    p = provider_factory(replies=[secret])
    with caplog.at_level(logging.DEBUG):
        out = await _decide(rl, fake_hass, p)
    assert p.calls == 1
    assert out["speak"] is True and out["reason"].startswith("local mind:")
    assert out["provider_failure"] == "unreadable"
    assert writes == [] and cache._cache == {}
    assert secret not in caplog.text and "abc123" not in caplog.text


async def test_repeat_after_unreadable_reply_neither_calls_again_nor_learns_silence(
        isolated, fake_hass, provider_factory):
    rl, cache, _, writes = isolated
    p = provider_factory(replies=["not json", "not json either"])
    first = await _decide(rl, fake_hass, p)
    second = await _decide(rl, fake_hass, p)
    assert p.calls == 1
    assert first["speak"] is True and second["speak"] is True
    assert writes == [] and cache._cache == {}


async def test_the_hold_expires_and_is_bounded(isolated, fake_hass, provider_factory,
                                               monkeypatch):
    rl, cache, coord, writes = isolated
    clock = {"t": 1000.0}
    monkeypatch.setattr(coord, "_monotonic", lambda: clock["t"])
    p = provider_factory(replies=["nope", '{"speak": false, "reason": "routine"}'])
    await _decide(rl, fake_hass, p)
    clock["t"] += coord.UNREADABLE_HOLD_S + 1
    out = await _decide(rl, fake_hass, p)
    assert p.calls == 2 and out == {"speak": False, "reason": "routine"}
    assert len(writes) == 1 and writes[0][1] is False   # a validated verdict is learned
    for i in range(coord.UNREADABLE_HOLD_MAX + 20):
        coord._hold(f"sig-{i}")
    assert len(coord._holds) == coord.UNREADABLE_HOLD_MAX


@pytest.mark.parametrize("reply", ['{"speak": true}', '{"speak": "yes"}', '{"urgency": "low"}'])
async def test_incomplete_or_invalid_replies_are_failures_too(isolated, fake_hass,
                                                              provider_factory, reply):
    rl, cache, _, writes = isolated
    p = provider_factory(replies=[reply])
    out = await _decide(rl, fake_hass, p)
    assert out["reason"].startswith("local mind:") and "provider_failure" in out
    assert writes == []


async def test_a_valid_silent_reply_is_still_learned(isolated, fake_hass, provider_factory):
    rl, cache, _, writes = isolated
    p = provider_factory(replies=['{"speak": false, "reason": "routine"}'])
    out = await _decide(rl, fake_hass, p)
    assert out == {"speak": False, "reason": "routine"}
    assert len(writes) == 1
    again = await _decide(rl, fake_hass, p)
    assert p.calls == 1 and again["speak"] is False      # served from the cache


async def test_a_stale_validated_verdict_beats_the_local_mind_after_an_unreadable_reply(
        isolated, fake_hass, provider_factory, monkeypatch):
    rl, cache, _, writes = isolated
    import time as _t
    sig = cache.signature("binary_sensor", "window", "general", "off", "on", False, "medium")
    cache._cache[sig] = {"speak": False, "urgency": "medium", "hits": 0,
                         "created": 0.0, "refreshed": _t.time() - cache.REFRESH_AGE - 10}
    p = provider_factory(replies=["garbage"])
    out = await _decide(rl, fake_hass, p)
    assert p.calls == 1
    assert out["speak"] is False and "local cache" in out["reason"]
    assert writes == []


async def test_cancellation_propagates_and_leaves_no_trace(isolated, fake_hass,
                                                           provider_factory, connectivity):
    rl, cache, coord, writes = isolated
    p = provider_factory(exc=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _decide(rl, fake_hass, p)
    assert writes == [] and dict(coord._holds) == {}
    assert connectivity.status()["consecutive_failures"] == 0


async def test_a_transport_failure_is_unchanged(isolated, fake_hass, provider_factory,
                                                connectivity):
    rl, cache, coord, writes = isolated
    p = provider_factory(exc=RuntimeError("connection refused"))
    out = await _decide(rl, fake_hass, p)
    assert p.calls == 1 and out["reason"].startswith("local mind:")
    assert "provider_failure" not in out
    assert connectivity.status()["consecutive_failures"] == 1
    assert dict(coord._holds) == {} and writes == []
