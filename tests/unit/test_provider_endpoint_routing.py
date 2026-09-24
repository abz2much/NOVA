"""Self-hosted endpoint routing and validation."""
from __future__ import annotations

import pytest


@pytest.fixture
def llm(load):
    return load("llm_provider")


@pytest.mark.parametrize(("raw", "expected"), [
    ("gpu.local", "http://gpu.local:11434"),
    ("10.0.4.96", "http://10.0.4.96:11434"),
    ("10.0.4.96:11500", "http://10.0.4.96:11500"),
    ("2001:db8::10", "http://[2001:db8::10]:11434"),
    ("https://models.example.test/ollama/", "https://models.example.test/ollama"),
])
def test_normalize_ollama_endpoint(llm, raw, expected):
    assert llm.normalize_provider_endpoint(raw, "ollama") == expected


def test_custom_endpoint_keeps_explicit_path_and_does_not_invent_port(llm):
    assert llm.normalize_provider_endpoint(
        "https://models.example.test/v1/", "custom"
    ) == "https://models.example.test/v1"


@pytest.mark.parametrize("raw", [
    "ftp://gpu.local:11434",
    "http://user:secret@gpu.local:11434",
    "http://gpu.local:11434?token=secret",
    "http://gpu.local:11434/#fragment",
    "http://gpu.local:not-a-port",
])
def test_rejects_unsafe_or_invalid_endpoint(llm, raw):
    with pytest.raises(ValueError):
        llm.normalize_provider_endpoint(raw, "ollama")


def test_provider_specific_endpoint_wins_over_legacy(llm):
    config = {
        "ollama_base_url": "ollama.internal",
        "custom_base_url": "https://custom.internal/v1",
        "llm_base_url": "http://legacy.invalid:11434",
    }
    assert llm.resolve_provider_endpoint(config, "ollama") == (
        "http://ollama.internal:11434"
    )
    assert llm.resolve_provider_endpoint(config, "custom") == (
        "https://custom.internal/v1"
    )


def test_legacy_endpoint_remains_a_compatibility_fallback(llm):
    config = {"llm_base_url": "http://legacy-gpu:11434/v1"}
    assert llm.resolve_provider_endpoint(config, "ollama") == (
        "http://legacy-gpu:11434/v1"
    )


def test_migrated_endpoints_never_cross_fallback_to_legacy(llm):
    config = {
        "self_hosted_endpoints_migrated": True,
        "ollama_base_url": "http://ollama.lan:11434",
        "custom_base_url": "",
        "llm_base_url": "http://legacy-gpu:11434/v1",
    }
    assert llm.resolve_provider_endpoint(config, "ollama") == "http://ollama.lan:11434"
    assert llm.resolve_provider_endpoint(config, "custom") is None


def test_endpoint_length_is_bounded(llm):
    with pytest.raises(ValueError, match="too long"):
        llm.normalize_provider_endpoint("https://" + "a" * 3000, "custom")


def test_cloud_provider_never_receives_a_self_hosted_endpoint(llm):
    config = {"llm_base_url": "http://gpu.local:11434"}
    assert llm.resolve_provider_endpoint(config, "groq") is None
    assert llm.resolve_provider_endpoint(config, "openai") is None


def test_unknown_provider_is_not_silently_replaced_with_groq(llm):
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        llm.create_provider("typo", "secret", "model")


def test_ollama_requires_an_explicit_endpoint(llm):
    with pytest.raises(ValueError, match="endpoint is not configured"):
        llm.create_provider("ollama", "", "model", None)
