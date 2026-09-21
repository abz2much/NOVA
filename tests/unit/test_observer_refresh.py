"""observer.refresh_tier_providers (Phase 3, v7.108.0).

A classifier/reasoning provider or model change made through the panel
while Observer is already running used to sit stale until Observer was
toggled off and back on (or Nova reloaded) — everything else (Observer's
listeners, appliance monitor, briefings, cognitive core) kept running
against the OLD tier clients. This rebuilds just the two tier providers
live, the same way vision/camera-reasoning already apply on their very
next analysis (camera.py's per-call _make_client).
"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def obs(load):
    return load("observer")


def _fake_provider(name):
    return SimpleNamespace(name=name)


async def test_refresh_is_noop_when_not_running(obs, fake_hass, monkeypatch):
    calls = []
    monkeypatch.setattr(obs, "create_tier_provider", lambda cfg, tier: calls.append(tier) or _fake_provider(tier))
    obs._STATE.running = False

    await obs.refresh_tier_providers(fake_hass)

    assert calls == []


async def test_refresh_skips_rebuild_for_unrelated_key(obs, fake_hass, monkeypatch):
    calls = []
    monkeypatch.setattr(obs, "create_tier_provider", lambda cfg, tier: calls.append(tier) or _fake_provider(tier))
    obs._STATE.running = True
    obs._STATE.config = {}

    await obs.refresh_tier_providers(fake_hass, {"observer_quiet_start": "23:00"})

    assert calls == []


async def test_refresh_rebuilds_both_tier_providers_when_running(obs, fake_hass, monkeypatch):
    calls = []

    def fake_create(cfg, tier):
        calls.append(tier)
        return _fake_provider(f"{tier}-provider")

    monkeypatch.setattr(obs, "create_tier_provider", fake_create)
    obs._STATE.running = True
    obs._STATE.config = {"classifier_provider": "groq"}
    obs._STATE.classifier_provider = _fake_provider("stale-classifier")
    obs._STATE.reasoning_provider = _fake_provider("stale-reasoning")

    await obs.refresh_tier_providers(fake_hass, {"reasoning_provider": "openai"})

    assert set(calls) == {"classifier", "reasoning"}
    assert obs._STATE.classifier_provider.name == "classifier-provider"
    assert obs._STATE.reasoning_provider.name == "reasoning-provider"


async def test_refresh_merges_latest_runtime_config(obs, fake_hass, monkeypatch):
    """The rebuilt providers must see the NEW provider/model, not the config
    Observer started with — mirrors start()'s own runtime_config merge."""
    seen_configs = []
    monkeypatch.setattr(
        obs, "create_tier_provider",
        lambda cfg, tier: seen_configs.append(dict(cfg)) or _fake_provider(tier),
    )
    obs._STATE.running = True
    obs._STATE.config = {"reasoning_provider": "groq"}
    fake_hass.data["nova"] = {
        "entry1": {"runtime_config": {"reasoning_provider": "anthropic", "reasoning_model": "claude-sonnet-5"}},
    }

    await obs.refresh_tier_providers(fake_hass, {"reasoning_provider": "anthropic"})

    assert all(c.get("reasoning_provider") == "anthropic" for c in seen_configs)
    assert all(c.get("reasoning_model") == "claude-sonnet-5" for c in seen_configs)


async def test_refresh_failure_keeps_the_previous_provider_live(obs, fake_hass, monkeypatch):
    """A failed rebuild (bad key, network hiccup at construction time) must
    never leave Observer with no tier provider at all — keep serving the
    last good one rather than erroring the whole tick."""
    def boom(cfg, tier):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(obs, "create_tier_provider", boom)
    obs._STATE.running = True
    obs._STATE.config = {}
    stale_classifier = _fake_provider("stale-classifier")
    stale_reasoning = _fake_provider("stale-reasoning")
    obs._STATE.classifier_provider = stale_classifier
    obs._STATE.reasoning_provider = stale_reasoning

    await obs.refresh_tier_providers(fake_hass, {"classifier_provider": "openai"})

    assert obs._STATE.classifier_provider is stale_classifier
    assert obs._STATE.reasoning_provider is stale_reasoning
