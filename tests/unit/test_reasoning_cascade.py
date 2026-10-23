"""How reasoning_loop.decide() falls back as capabilities drop away.

Order of preference: local rules, then a fresh learned verdict, then the
cloud model guarded by the connectivity breaker, then a stale learned verdict,
and finally the Local Mind. The collaborators (rules, cache, Local Mind) are
replaced with small stand-ins so only the routing between them is under test.

The breaker is process-wide state: every test starts and ends with it closed,
and its clock is a fake one the test moves by hand.
"""
from __future__ import annotations

import pytest

FLOOR_MESSAGE = "Local Mind handled it."
REFUSED = RuntimeError("connection refused")   # not transient, so never retried


class Cascade:
    """Handles on the stand-ins a test may steer or inspect."""

    def __init__(self, rl, clock):
        self.rl = rl
        self.clock = clock
        self.fresh = None        # what reasoning_cache.get returns for a fresh read
        self.stale = None        # ... and for get(..., ignore_age=True)
        self.learned: list[tuple] = []
        self.floor_calls = 0
        self.local_answer = None

    async def decide(self, hass, provider, trace=None, **changes):
        event = {
            "honorific": "sir",
            "event_summary": "binary_sensor.shed_window changed to on",
            "home_state_summary": "away",
            "classifier_urgency": "medium",
            "classifier_category": "general",
            "recent_announcements": [],
            "anyone_home": False,
            "entity_id": "binary_sensor.shed_window",
            "device_class": "window",
            "from_state": "off",
            "to_state": "on",
            "friendly_name": "Shed Window",
        }
        event.update(changes)
        if trace is not None:
            event["trace"] = trace
        return await self.rl.decide(hass, provider, **event)


@pytest.fixture(autouse=True)
def closed_breaker(connectivity):
    connectivity.reset()
    yield
    connectivity.reset()


@pytest.fixture
def cascade(reasoning_loop, connectivity, load, monkeypatch):
    clock = {"now": 10_000.0}
    monkeypatch.setattr(connectivity, "_now", lambda: clock["now"])
    state = Cascade(reasoning_loop, clock)

    cache = load("reasoning_cache")
    monkeypatch.setattr(
        cache, "get", lambda sig, ignore_age=False: state.stale if ignore_age else state.fresh)
    monkeypatch.setattr(cache, "remember", lambda *args: state.learned.append(args))
    monkeypatch.setattr(cache, "note_hit", lambda *a, **k: None)
    monkeypatch.setattr(cache, "note_cloud_call", lambda *a, **k: None)

    monkeypatch.setattr(reasoning_loop, "_try_local_reasoning",
                        lambda *a, **k: state.local_answer)
    monkeypatch.setattr(reasoning_loop, "_rich_mode", lambda hass: False)

    async def floor(*args, **kwargs):
        state.floor_calls += 1
        return {"speak": True, "message": FLOOR_MESSAGE, "urgency": "medium"}

    monkeypatch.setattr(load("local_mind"), "assess", floor)
    return state


def _reply(speak: bool, **fields) -> str:
    import json
    return json.dumps({"speak": speak, **fields})


async def _trip(cascade, hass, provider_factory, times=2):
    failing = provider_factory(exc=REFUSED)
    for _ in range(times):
        await cascade.decide(hass, failing)
    return failing


# ── provider failures ───────────────────────────────────────────────────────

async def test_unreachable_provider_falls_to_local_mind(cascade, fake_hass,
                                                        provider_factory, connectivity):
    provider = provider_factory(exc=REFUSED)
    trace: dict = {}
    out = await cascade.decide(fake_hass, provider, trace=trace)
    assert isinstance(out, dict) and "speak" in out
    assert provider.calls == 1
    assert out["message"] == FLOOR_MESSAGE
    assert trace["origin"] == "local_mind"
    assert connectivity.status()["consecutive_failures"] == 1
    assert cascade.learned == []                  # a failure is never learned


async def test_unreadable_reply_counts_as_failure(cascade, fake_hass,
                                                  provider_factory, connectivity):
    provider = provider_factory(replies=["I think you should probably say something"])
    out = await cascade.decide(fake_hass, provider)
    assert out["message"] == FLOOR_MESSAGE
    assert "provider_failure" in out
    assert connectivity.status()["consecutive_failures"] == 1
    assert cascade.learned == []


async def test_failure_prefers_stale_learned_verdict(cascade, fake_hass, provider_factory):
    cascade.stale = {"speak": False, "urgency": "medium"}
    trace: dict = {}
    out = await cascade.decide(fake_hass, provider_factory(exc=REFUSED), trace=trace)
    assert out["speak"] is False
    assert trace["origin"] == "cache"
    assert cascade.floor_calls == 0


# ── the breaker ─────────────────────────────────────────────────────────────

async def test_two_failures_open_the_breaker_and_skip_the_network(
        cascade, fake_hass, provider_factory, connectivity):
    failing = provider_factory(exc=REFUSED)
    await cascade.decide(fake_hass, failing)
    assert connectivity.status()["state"] == "closed"     # one is not enough
    await cascade.decide(fake_hass, failing)
    assert connectivity.status()["state"] == "open"

    spare = provider_factory(replies=[_reply(True, message="Never sent.", urgency="high")])
    out = await cascade.decide(fake_hass, spare)
    assert failing.calls == 2 and spare.calls == 0
    assert out["message"] == FLOOR_MESSAGE


async def test_breaker_holds_until_cooldown_then_probe_recovers(
        cascade, fake_hass, provider_factory, connectivity):
    await _trip(cascade, fake_hass, provider_factory)

    good = provider_factory(replies=[_reply(True, message="Recovered, sir.", urgency="low")])
    cascade.clock["now"] += 59                        # still inside the 60s cooldown
    await cascade.decide(fake_hass, good)
    assert good.calls == 0

    cascade.clock["now"] += 61                        # well past the cooldown
    out = await cascade.decide(fake_hass, good)
    assert good.calls == 1                            # the probe went through
    assert out["speak"] is True
    assert connectivity.status()["state"] == "closed"
    assert connectivity.status()["consecutive_failures"] == 0


async def test_failed_probe_reopens_at_once(cascade, fake_hass, provider_factory, connectivity):
    failing = await _trip(cascade, fake_hass, provider_factory)
    cascade.clock["now"] += 120
    await cascade.decide(fake_hass, failing)
    assert failing.calls == 3
    assert connectivity.status()["state"] == "open"
    assert connectivity.status()["cooldown_remaining_s"] == 60


# ── provider answers ────────────────────────────────────────────────────────

async def test_spoken_verdict_is_returned_and_learned(cascade, fake_hass,
                                                      provider_factory, connectivity):
    provider = provider_factory(
        replies=[_reply(True, message="Sir, the shed window opened.", urgency="high")])
    trace: dict = {}
    out = await cascade.decide(fake_hass, provider, trace=trace)
    assert provider.calls == 1
    assert out["speak"] is True
    assert out["urgency"] == "high"
    assert "shed window" in out["message"].lower()
    assert trace["origin"] == "provider"
    assert [entry[1:] for entry in cascade.learned] == [(True, "high")]
    assert connectivity.status()["total_successes"] >= 1


async def test_silent_verdict_is_returned_and_learned_at_classifier_urgency(
        cascade, fake_hass, provider_factory):
    provider = provider_factory(replies=[_reply(False, reason="routine")])
    out = await cascade.decide(fake_hass, provider)
    assert out["speak"] is False
    assert [entry[1:] for entry in cascade.learned] == [(False, "medium")]
    assert cascade.floor_calls == 0


# ── earlier layers short-circuit the model ──────────────────────────────────

async def test_local_rule_answer_never_reaches_the_model(cascade, fake_hass, provider_factory):
    cascade.local_answer = {"speak": False, "reason": "rule said no"}
    provider = provider_factory(replies=[_reply(True, message="x", urgency="high")])
    out = await cascade.decide(fake_hass, provider)
    assert out == {"speak": False, "reason": "rule said no"}
    assert provider.calls == 0


async def test_fresh_learned_verdict_never_reaches_the_model(cascade, fake_hass,
                                                             provider_factory):
    cascade.fresh = {"speak": False, "urgency": "medium"}
    provider = provider_factory(replies=[_reply(True, message="x", urgency="high")])
    trace: dict = {}
    out = await cascade.decide(fake_hass, provider, trace=trace)
    assert out["speak"] is False
    assert trace["origin"] == "cache"
    assert provider.calls == 0
