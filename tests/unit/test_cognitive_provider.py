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

from fakes import attach_runtime, make_runtime


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
def runtime(load, fake_hass):
    """One loaded Nova entry whose runtime owns the provider holds."""
    rt = make_runtime(load)
    attach_runtime(fake_hass, rt)
    return rt


@pytest.fixture
def isolated(load, connectivity, monkeypatch, runtime):
    rl = load("reasoning_loop")
    cache = load("reasoning_cache")
    lm = load("local_mind")
    connectivity.reset()
    monkeypatch.setattr(cache, "_cache", {})
    monkeypatch.setattr(cache, "_loaded", True)
    monkeypatch.setattr(cache, "save", lambda: None)
    monkeypatch.setattr(rl, "_rich_mode", lambda hass: False)
    monkeypatch.setattr(lm, "_connect", lambda: None)
    monkeypatch.setattr(lm, "_recent_events", {})
    monkeypatch.setattr(lm, "_hist_cache", {})
    monkeypatch.setattr(lm, "_days_cache", (0.0, 0.0))
    writes = []
    real_remember = cache.remember
    monkeypatch.setattr(cache, "remember",
                        lambda *a, **k: writes.append(a) or real_remember(*a, **k))
    yield rl, cache, runtime.provider_holds, writes
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
                                               monkeypatch, load):
    rl, cache, holds, writes = isolated
    clock = {"t": 1000.0}
    monkeypatch.setattr(holds, "_clock", lambda: clock["t"])
    p = provider_factory(replies=["nope", '{"speak": false, "reason": "routine"}'])
    await _decide(rl, fake_hass, p)
    clock["t"] += holds.hold_s + 1
    out = await _decide(rl, fake_hass, p)
    assert p.calls == 2 and out == {"speak": False, "reason": "routine"}
    assert len(writes) == 1 and writes[0][1] is False   # a validated verdict is learned
    hmod = load("cognitive.holds")
    assert (holds.hold_s, holds.max_holds) == (60.0, 64) == (hmod.HOLD_S, hmod.MAX_HOLDS)
    for i in range(hmod.MAX_HOLDS + 20):
        holds.hold(f"sig-{i}")
    assert len(holds) == hmod.MAX_HOLDS
    holds.clear()
    holds.clear()                                       # idempotent
    assert len(holds) == 0


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
    rl, cache, holds, writes = isolated
    p = provider_factory(exc=asyncio.CancelledError())
    before = connectivity.status()["total_successes"]
    with pytest.raises(asyncio.CancelledError):
        await _decide(rl, fake_hass, p)
    assert writes == [] and len(holds) == 0
    st = connectivity.status()
    assert st["consecutive_failures"] == 0 and st["total_successes"] == before


async def test_a_transport_failure_is_unchanged(isolated, fake_hass, provider_factory,
                                                connectivity):
    rl, cache, holds, writes = isolated
    p = provider_factory(exc=RuntimeError("connection refused"))
    out = await _decide(rl, fake_hass, p)
    assert p.calls == 1 and out["reason"].startswith("local mind:")
    assert "provider_failure" not in out
    assert connectivity.status()["consecutive_failures"] == 1
    assert len(holds) == 0 and writes == []


async def test_the_coordinator_collects_the_snapshot_from_the_summary(
        isolated, fake_hass, provider_factory, monkeypatch, connectivity, load):
    """With no structured fields, the snapshot is backfilled from the event
    summary, frozen, and handed to the fallback unchanged."""
    rl, cache, _, writes = isolated
    coord = load("cognitive.coordinator")
    seen = []

    async def _capture(hass, snapshot, honorific):
        seen.append(snapshot)
        return {"speak": False, "reason": "captured"}

    monkeypatch.setattr(coord, "_local_mind", _capture)
    monkeypatch.setattr(connectivity, "allow_request", lambda: False)   # breaker open
    p = provider_factory(replies=[])
    recent = ["Earlier announcement"]
    out = await _decide(rl, fake_hass, p, entity_id="", device_class="", from_state="",
                        to_state="", friendly_name="", recent_announcements=recent)
    recent.append("later")
    assert out == {"speak": False, "reason": "captured"} and p.calls == 0
    s = seen[0]
    assert (s.entity_id, s.domain, s.friendly_name, s.from_state, s.to_state) == (
        "binary_sensor.cellar_window", "binary_sensor", "Cellar Window", "off", "on")
    assert s.recent_announcements == ("Earlier announcement",)
    assert s.category == "general" and s.urgency == "medium" and s.anyone_home is False


# ── Provider health follows validation (breaker ordering) ──────────────────

_WINDOW = "Cellar Window (binary_sensor.cellar_window) changed from off to on"
_DOOR = "Shed Door (binary_sensor.shed_door) changed from off to on"


def _door(rl, hass, p):
    return _decide(rl, hass, p, event_summary=_DOOR, entity_id="binary_sensor.shed_door",
                   device_class="door", friendly_name="Shed Door")


async def test_unusable_replies_count_against_the_breaker(isolated, fake_hass,
                                                          provider_factory, connectivity):
    """Malformed replies for different signatures accumulate failures and
    open the breaker; nothing is learned."""
    rl, cache, holds, writes = isolated
    p = provider_factory(replies=["garbage one", "garbage two", "never asked"])
    before = connectivity.status()["total_successes"]
    await _decide(rl, fake_hass, p)
    assert connectivity.status()["consecutive_failures"] == 1
    assert connectivity.status()["total_successes"] == before
    await _door(rl, fake_hass, p)
    st = connectivity.status()
    assert st["state"] == "open" and st["consecutive_failures"] == 2
    out = await _decide(rl, fake_hass, p, device_class="window", anyone_home=True)
    assert p.calls == 2 and out["reason"].startswith("local mind:")   # breaker open
    assert writes == [] and len(holds) == 2


async def test_a_valid_reply_records_success_and_restores_health(isolated, fake_hass,
                                                                 provider_factory,
                                                                 connectivity):
    rl, cache, holds, writes = isolated
    p = provider_factory(replies=["garbage", '{"speak": false, "reason": "routine"}'])
    before = connectivity.status()["total_successes"]
    await _decide(rl, fake_hass, p)
    assert connectivity.status()["consecutive_failures"] == 1
    out = await _door(rl, fake_hass, p)
    st = connectivity.status()
    assert out == {"speak": False, "reason": "routine"}
    assert st["state"] == "closed" and st["consecutive_failures"] == 0
    assert st["total_successes"] == before + 1 and len(writes) == 1


def _half_open(connectivity):
    connectivity.record_failure()
    connectivity.record_failure()
    connectivity._BREAKER.opened_at -= connectivity._OPEN_COOLDOWN + 1   # cooldown over
    assert connectivity.status()["state"] == "open"


async def test_a_held_signature_consumes_no_half_open_probe(isolated, fake_hass,
                                                            provider_factory, connectivity):
    """A held event goes to the fallback before the breaker is asked, so the
    single half-open probe is left for a different event, which recovers."""
    rl, cache, holds, writes = isolated
    p = provider_factory(replies=['{"speak": false, "reason": "routine"}'])
    sig = cache.signature("binary_sensor", "window", "general", "off", "on", False, "medium")
    holds.hold(sig)
    _half_open(connectivity)
    out = await _decide(rl, fake_hass, p)
    assert p.calls == 0 and out["reason"].startswith("local mind:")
    assert connectivity._BREAKER.half_open_probes == 0         # probe not consumed
    out = await _door(rl, fake_hass, p)                       # a different signature
    assert p.calls == 1 and out == {"speak": False, "reason": "routine"}
    assert connectivity.status()["state"] == "closed"


async def test_an_unusable_probe_reply_reopens_the_breaker(isolated, fake_hass,
                                                           provider_factory, connectivity):
    rl, cache, holds, writes = isolated
    p = provider_factory(replies=["still garbage"])
    _half_open(connectivity)
    await _door(rl, fake_hass, p)
    assert p.calls == 1 and connectivity.status()["state"] == "open"
    assert writes == []


async def test_without_a_loaded_runtime_nothing_is_held(load, fake_hass, provider_factory,
                                                        connectivity, monkeypatch):
    """No loaded Nova entry: no hold store exists, so nothing module-level
    fills in; the reply still falls back and is never learned."""
    rl, cache = load("reasoning_loop"), load("reasoning_cache")
    connectivity.reset()
    monkeypatch.setattr(cache, "_cache", {})
    monkeypatch.setattr(cache, "_loaded", True)
    monkeypatch.setattr(cache, "save", lambda: None)
    monkeypatch.setattr(rl, "_rich_mode", lambda hass: False)
    p = provider_factory(replies=["garbage", "garbage"])
    await _decide(rl, fake_hass, p)
    await _decide(rl, fake_hass, p)
    assert p.calls == 2 and cache._cache == {}
    connectivity.reset()


# ── Holds are owned by the loaded runtime ──────────────────────────────────

async def test_a_reload_starts_with_no_holds(isolated, fake_hass, provider_factory, load,
                                             connectivity):
    """The hold lives on the entry's NovaRuntime. Unloading clears it and a
    reload (a new runtime, possibly a changed provider or configuration)
    asks the provider again at once."""
    rl, cache, holds, writes = isolated
    rt_mod = load("runtime")
    p = provider_factory(replies=["garbage", "garbage"])
    await _decide(rl, fake_hass, p)
    await _decide(rl, fake_hass, p)
    assert p.calls == 1 and len(holds) == 1                # held in this runtime

    first = fake_hass.config_entries.async_entries("nova")[0]
    rt_mod.clear_runtime(first)                            # unload
    assert len(holds) == 0
    rt_mod.clear_runtime(first)                            # idempotent

    connectivity.reset()
    reloaded = make_runtime(load)
    attach_runtime(fake_hass, reloaded)
    assert reloaded.provider_holds is not holds and len(reloaded.provider_holds) == 0
    changed = provider_factory(replies=['{"speak": false, "reason": "routine"}'])
    out = await _decide(rl, fake_hass, changed)
    assert changed.calls == 1 and out == {"speak": False, "reason": "routine"}


def test_each_runtime_gets_its_own_bounded_holds(load):
    a, b = make_runtime(load), make_runtime(load)
    hmod = load("cognitive.holds")
    assert isinstance(a.provider_holds, hmod.ProviderHolds)
    assert a.provider_holds is not b.provider_holds
    a.provider_holds.hold("sig")
    assert a.provider_holds.held("sig") and not b.provider_holds.held("sig")
    assert a.provider_holds.max_holds == 64 and a.provider_holds.hold_s == 60.0


def test_the_hold_is_refreshed_and_the_oldest_is_dropped(load):
    hmod = load("cognitive.holds")
    clock = {"t": 0.0}
    h = hmod.ProviderHolds(hold_s=10, max_holds=3, clock=lambda: clock["t"])
    for sig in ("a", "b", "c"):
        h.hold(sig)
    h.hold("a")                                           # refresh moves it to newest
    h.hold("d")                                           # evicts the oldest: b
    assert [s for s in "abcd" if h.held(s)] == ["a", "c", "d"]
    clock["t"] = 10.0
    assert not h.held("a") and len(h) == 2                # expired entries are dropped
    h.hold("e")
    assert len(h) == 1                                    # prune on write


def test_no_hold_state_is_kept_at_module_level():
    """The coordinator and the hold store keep no module-level mutable state:
    a module-global cache would survive unload and leak across a reload."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "nova" / "cognitive"
    for name in ("coordinator.py", "holds.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Call) and "getLogger" in ast.unparse(value.func):
                    continue
                assert not isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Call)), (
                    name, ast.unparse(node))
        assert "global " not in (root / name).read_text(encoding="utf-8"), name
