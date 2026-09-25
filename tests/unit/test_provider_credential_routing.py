"""Tests for per-provider credential routing (Phase 2, v7.107.0).

Before this phase, a tier/role pointed at a provider other than the
installation's primary could still receive the primary's shared key — e.g.
vision_provider=openai while llm_provider=groq sent the Groq key to OpenAI's
API. resolve_provider_credential is the single routing rule every role
(Main Agent, classifier/reasoning/review tiers, vision, camera-reasoning)
now goes through: each provider gets only its own credential, with a narrow
fallback to the legacy shared key ONLY for the installation's saved primary
cloud provider — never for a different provider, and never for ollama/custom.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def lp(load):
    return load("llm_provider")


# ── each provider resolves its own dedicated field first ─────────────────────

@pytest.mark.parametrize("provider,field", [
    ("groq", "groq_api_key"),
    ("openai", "openai_api_key"),
    ("anthropic", "anthropic_api_key"),
    ("gemini", "gemini_api_key"),
    ("custom", "custom_api_key"),
    ("ollama", "ollama_api_key"),
])
def test_resolves_own_dedicated_field(lp, provider, field):
    config = {field: "OWN-KEY", "llm_provider": "irrelevant-other-provider"}
    assert lp.resolve_provider_credential(config, provider) == "OWN-KEY"


def test_unknown_provider_returns_empty(lp):
    assert lp.resolve_provider_credential({"api_key": "X"}, "not-a-real-provider") == ""


# ── the core regression: no cross-provider reuse ──────────────────────────────

def test_mismatched_role_provider_never_gets_the_primarys_key(lp):
    """The exact shape of the pre-Phase-2 bug: llm_provider=groq with a
    shared api_key, but this role is configured for openai — openai must
    get nothing, not the Groq key."""
    config = {"llm_provider": "groq", "api_key": "GROQ-SECRET"}
    assert lp.resolve_provider_credential(config, "openai") == ""
    assert lp.resolve_provider_credential(config, "anthropic") == ""
    assert lp.resolve_provider_credential(config, "gemini") == ""


def test_matching_role_provider_does_get_the_shared_key_pre_migration(lp):
    """The narrow, intentional exception: this role's provider IS the saved
    primary, and no provider-specific field is populated yet (pre-migration
    or migration failed) — the shared key resolves here, and only here."""
    config = {"llm_provider": "groq", "api_key": "GROQ-SECRET"}
    assert lp.resolve_provider_credential(config, "groq") == "GROQ-SECRET"


def test_provider_specific_field_wins_over_shared_key_once_migrated(lp):
    config = {
        "llm_provider": "groq", "api_key": "OLD-SHARED-KEY",
        "groq_api_key": "MIGRATED-KEY",
    }
    assert lp.resolve_provider_credential(config, "groq") == "MIGRATED-KEY"


@pytest.mark.parametrize("provider", ["ollama", "custom"])
def test_ollama_and_custom_never_receive_the_shared_key(lp, provider):
    """Nova can't prove a self-hosted endpoint was the shared key's intended
    destination — even when llm_provider names it as the saved primary."""
    config = {"llm_provider": provider, "api_key": "SHARED-SECRET"}
    assert lp.resolve_provider_credential(config, provider) == ""


def test_unset_llm_provider_defaults_to_groq_like_the_rest_of_the_codebase(lp):
    config = {"api_key": "GROQ-SECRET"}  # no llm_provider key at all
    assert lp.resolve_provider_credential(config, "groq") == "GROQ-SECRET"
    assert lp.resolve_provider_credential(config, "openai") == ""


# ── create_tier_provider uses the resolver, per-tier, independently ──────────

def _routing():
    import sys
    return sys.modules["jc.providers.routing"]


def test_create_tier_provider_resolves_each_providers_own_key(lp, monkeypatch):
    seen = []

    def _fake_build(spec):
        seen.append((spec.provider, spec.api_key))
        return object()

    monkeypatch.setattr(_routing(), "build_provider", _fake_build)

    config = {
        "llm_provider": "groq", "api_key": "GROQ-SECRET",
        "classifier_provider": "openai", "openai_api_key": "OPENAI-SECRET",
        "reasoning_provider": "anthropic", "anthropic_api_key": "ANTHROPIC-SECRET",
    }
    lp.create_tier_provider(config, "classifier")
    lp.create_tier_provider(config, "reasoning")
    lp.create_tier_provider(config, "conversation")

    by_provider = dict(seen)
    assert by_provider["openai"] == "OPENAI-SECRET"
    assert by_provider["anthropic"] == "ANTHROPIC-SECRET"
    assert by_provider["groq"] == "GROQ-SECRET"


def test_create_tier_provider_does_not_leak_primary_key_to_unmigrated_other_provider(lp, monkeypatch):
    """A tier pointed at a different provider than the primary, with no
    dedicated key of its own yet, gets nothing — not the primary's key."""
    seen = []
    monkeypatch.setattr(
        _routing(), "build_provider",
        lambda spec: seen.append((spec.provider, spec.api_key)),
    )
    config = {
        "llm_provider": "groq", "api_key": "GROQ-SECRET",
        "classifier_provider": "openai",  # no openai_api_key configured
    }
    lp.create_tier_provider(config, "classifier")
    assert seen == [("openai", "")]
