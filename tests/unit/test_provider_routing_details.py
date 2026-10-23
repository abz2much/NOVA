"""Provider routing: how a role gets its provider, model, credential and
endpoint, and how an adapter is built for that choice.

The adapter itself is a recorder, so these pin the arguments it is built
with. Credential and endpoint isolation have their own files
(test_provider_credential_routing and test_provider_endpoint_routing)."""
import hashlib
import logging
import types

import pytest


@pytest.fixture
def rt(load):
    return load("providers.routing")


@pytest.fixture
def built(rt, monkeypatch):
    """Replace descriptor() so build_provider makes a recording adapter."""
    box = types.SimpleNamespace(calls=[], known={"groq", "ollama", "gemini", "custom",
                                                 "openai", "anthropic"})
    real = rt.descriptor

    def fake(provider):
        if provider not in box.known:
            return None
        base = real(provider)

        def adapter(**kw):
            box.calls.append((provider, kw))
            return types.SimpleNamespace(name=provider, **kw)
        return types.SimpleNamespace(adapter=adapter, default_base_url=base.default_base_url,
                                     endpoint_field=base.endpoint_field)

    monkeypatch.setattr(rt, "descriptor", fake)
    return box


# ── constants ────────────────────────────────────────────────────────────────

def test_the_role_fields_and_tiers(rt):
    assert rt.ROLE_FIELDS == {
        "conversation": ("llm_provider", "model"),
        "classifier": ("classifier_provider", "classifier_model"),
        "reasoning": ("reasoning_provider", "reasoning_model"),
        "review": ("review_provider", "review_model"),
        "vision": ("vision_provider", "vision_model"),
        "camera_reasoning": ("camera_reasoning_provider", "camera_reasoning_model"),
        "suggestion_review": ("suggestion_review_provider", "suggestion_review_model"),
    }
    assert rt.TIERS == ("classifier", "reasoning", "review", "conversation")


def test_default_models_are_only_for_cloud_providers_that_have_one(rt):
    assert rt.DEFAULT_MODELS == {
        "groq": "openai/gpt-oss-120b", "openai": "gpt-5-mini",
        "gemini": "gemini-3.6-flash", "anthropic": "claude-sonnet-5-5"}


# ── the spec ─────────────────────────────────────────────────────────────────

def test_a_spec_hides_its_key_and_fingerprints_a_digest_of_it(rt):
    spec = rt.ProviderSpec("groq", "m", api_key="sk-secret", base_url="http://h")
    assert "sk-secret" not in repr(spec)
    digest = hashlib.sha256(b"sk-secret").hexdigest()
    material = "\0".join(("groq", "m", "http://h", digest))
    assert spec.fingerprint() == hashlib.sha256(material.encode()).hexdigest()


def test_the_fingerprint_changes_with_each_part(rt):
    base = rt.ProviderSpec("groq", "m", "k", "u").fingerprint()
    for other in (rt.ProviderSpec("openai", "m", "k", "u"), rt.ProviderSpec("groq", "n", "k", "u"),
                  rt.ProviderSpec("groq", "m", "j", "u"), rt.ProviderSpec("groq", "m", "k", "v")):
        assert other.fingerprint() != base
    assert rt.ProviderSpec("groq", "m").fingerprint() == \
        rt.ProviderSpec("groq", "m", "", None).fingerprint()


# ── routing a mismatched model ───────────────────────────────────────────────

def test_a_cloud_provider_with_an_ollama_tagged_model_is_routed_to_ollama(rt):
    assert rt.normalize_routing("Gemini ", " gemma4:26b ", None) == (
        "ollama", None,
        "model 'gemma4:26b' uses Ollama tag syntax but provider was 'gemini' — "
        "routing to ollama (default base URL)")
    assert rt.normalize_routing("groq", "llama3.3:70b", "http://gpu:11434") == (
        "ollama", "http://gpu:11434",
        "model 'llama3.3:70b' uses Ollama tag syntax but provider was 'groq' — "
        "routing to ollama")


@pytest.mark.parametrize("provider, model", [
    ("ollama", "gemma4:26b"), ("custom", "m:latest"), ("groq", "gpt-oss-120b"),
    ("groq", "a:b:c"), ("groq", ":x"), ("groq", "x:"), ("", "gemma:7b"), (None, None)])
def test_other_cases_are_left_as_they_are(rt, provider, model):
    routed, url, note = rt.normalize_routing(provider, model, "u")
    assert routed == (provider or "").lower().strip() and url == "u" and note is None


def test_resolving_a_spec_applies_the_routing_and_warns(rt, caplog):
    config = {"ollama_api_key": "ok", "gemini_api_key": "gk",
              "ollama_base_url": "http://gpu", "gemini_base_url": "ignored"}
    with caplog.at_level(logging.WARNING, logger=rt._LOGGER.name):
        spec = rt.resolve_spec(config, "gemini", "gemma4:26b")
    assert (spec.provider, spec.model) == ("ollama", "gemma4:26b")
    assert spec.api_key == "ok" and spec.base_url == "http://gpu"
    assert [r.getMessage() for r in caplog.records] == [
        "LLM routing corrected: model 'gemma4:26b' uses Ollama tag syntax but provider "
        "was 'gemini' — routing to ollama (default base URL)"]


def test_resolving_an_ordinary_spec_is_quiet(rt, caplog):
    with caplog.at_level(logging.DEBUG, logger=rt._LOGGER.name):
        spec = rt.resolve_spec({"groq_api_key": "gk"}, " Groq", "llama-3.3")
    assert spec == rt.ProviderSpec("groq", "llama-3.3", "gk", None)
    assert caplog.records == []


def test_the_tier_is_passed_on_to_the_endpoint_lookup(rt):
    config = {"classifier_base_url": "http://tier-host", "ollama_base_url": "http://own"}
    spec = rt.resolve_spec(config, "ollama", "llama3", tier="classifier")
    assert spec.base_url == "http://tier-host"
    assert rt.resolve_spec(config, "ollama", "llama3").base_url == "http://own"


# ── the tier specs ───────────────────────────────────────────────────────────

def test_each_tier_has_its_defaults(rt):
    for tier in ("classifier", "reasoning", "review"):
        spec = rt.tier_spec({"groq_api_key": "gk"}, tier)
        assert (spec.provider, spec.model, spec.api_key, spec.base_url) == (
            "groq", "openai/gpt-oss-120b", "gk", None)


def test_the_conversation_tier_follows_the_saved_provider_and_model(rt):
    spec = rt.tier_spec({"llm_provider": "openai", "model": "gpt-5-mini",
                         "openai_api_key": "ok"}, "conversation")
    assert (spec.provider, spec.model, spec.api_key) == ("openai", "gpt-5-mini", "ok")
    default = rt.tier_spec({}, "conversation")
    assert (default.provider, default.model) == ("groq", "openai/gpt-oss-120b")


def test_a_tier_specific_choice_overrides_the_default(rt):
    config = {"reasoning_provider": "anthropic", "reasoning_model": "claude-x",
              "anthropic_api_key": "ak", "llm_provider": "groq"}
    spec = rt.tier_spec(config, "reasoning")
    assert (spec.provider, spec.model, spec.api_key) == ("anthropic", "claude-x", "ak")
    own = rt.tier_spec({"conversation_provider": "gemini", "conversation_model": "gm",
                        "llm_provider": "groq", "gemini_api_key": "gk"}, "conversation")
    assert (own.provider, own.model) == ("gemini", "gm")


def test_a_tier_model_in_ollama_form_on_a_cloud_provider_is_rerouted(rt):
    spec = rt.tier_spec({"review_provider": "openai", "review_model": "gemma4:26b",
                         "ollama_base_url": "http://gpu", "ollama_api_key": "o"}, "review")
    assert (spec.provider, spec.api_key, spec.base_url) == ("ollama", "o", "http://gpu")


@pytest.mark.parametrize("tier", ["vision", "nonsense", "", "camera_reasoning"])
def test_an_unknown_tier_is_an_error(rt, tier):
    with pytest.raises(ValueError, match=f"Unknown tier: {tier}"):
        rt.tier_spec({}, tier)


# ── building adapters ────────────────────────────────────────────────────────

def test_an_adapter_is_built_with_the_spec_and_the_default_endpoint(rt, built):
    provider = rt.build_provider(rt.ProviderSpec("gemini", "gm", "gk"))
    assert built.calls == [("gemini", {
        "api_key": "gk", "model": "gm", "base_url": rt.descriptor("gemini").default_base_url})]
    assert provider.name == "gemini"
    assert built.calls[0][1]["base_url"]


def test_the_specs_own_endpoint_wins_over_the_default(rt, built):
    rt.build_provider(rt.ProviderSpec("gemini", "gm", "gk", "http://proxy"))
    assert built.calls[0][1]["base_url"] == "http://proxy"


def test_a_provider_with_no_default_endpoint_gets_none(rt, built):
    rt.build_provider(rt.ProviderSpec("groq", "m", "k"))
    assert built.calls[0][1]["base_url"] is None


def test_an_unknown_provider_is_a_configuration_error(rt, load):
    errors = load("providers.errors")
    with pytest.raises(errors.ProviderConfigurationError) as caught:
        rt.build_provider(rt.ProviderSpec("nope", "m", "k"))
    assert caught.value.kind is errors.ProviderErrorKind.INVALID_REQUEST
    assert "Unknown LLM provider" in caught.value.diagnostic_text()


def test_create_provider_builds_what_was_asked_for(rt, built):
    rt.create_provider(" Groq ", "gk", "llama-3.3", "http://h")
    assert built.calls == [("groq", {"api_key": "gk", "model": "llama-3.3",
                                     "base_url": "http://h"})]


def test_a_missing_key_is_built_as_blank(rt, built):
    rt.create_provider("groq", None, "m")
    assert built.calls[0][1]["api_key"] == ""


def test_a_rerouted_model_drops_the_credential_and_keeps_the_endpoint(rt, built, caplog):
    with caplog.at_level(logging.WARNING, logger=rt._LOGGER.name):
        rt.create_provider("groq", "gk-secret", "gemma4:26b", "http://gpu:11434")
    assert built.calls == [("ollama", {"api_key": "", "model": "gemma4:26b",
                                       "base_url": "http://gpu:11434"})]
    assert [r.getMessage() for r in caplog.records] == [
        "LLM routing corrected: model 'gemma4:26b' uses Ollama tag syntax but provider "
        "was 'groq' — routing to ollama"]


def test_a_tier_provider_is_built_from_its_spec_and_logged_at_debug(rt, built, caplog):
    with caplog.at_level(logging.DEBUG, logger=rt._LOGGER.name):
        rt.create_tier_provider({"groq_api_key": "gk"}, "reasoning")
    assert built.calls == [("groq", {"api_key": "gk", "model": "openai/gpt-oss-120b",
                                     "base_url": None})]
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "Creating reasoning tier provider: groq / openai/gpt-oss-120b")]


# ── keys, locations ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("key, provider", [
    ("sk-ant-abc", "anthropic"), ("gsk_abc", "groq"), ("AIzaabc", "gemini"),
    ("sk-abc", "openai"), ("  sk-ant-x ", "anthropic"), ("something", "groq"),
    ("", "groq"), (None, "groq"), ("aiza", "groq")])
def test_the_provider_is_guessed_from_the_key_shape(rt, key, provider):
    assert rt.detect_provider_from_key(key) == provider


@pytest.mark.parametrize("url, local", [
    ("http://localhost:11434", True), ("http://LOCALHOST", True),
    ("http://nas.local/v1", True), ("http://127.0.0.1:8080", True),
    ("http://192.168.1.5", True), ("http://10.0.0.2", True), ("http://[::1]:8080", True),
    ("http://8.8.8.8", False), ("https://api.example.com", False),
    ("http://localhost.example.com", False), ("", False), (None, False),
    ("not a url", False)])
def test_only_a_clearly_local_address_counts_as_local(rt, url, local):
    assert rt.is_local_url(url) is local


@pytest.mark.parametrize("name, base_url, where", [
    ("groq", None, "cloud"), ("anthropic", "http://localhost", "cloud"),
    ("ollama", "https://far.example.com", "local"),
    ("custom", "http://192.168.1.9", "local"), ("custom", "https://far.example.com", "unknown"),
    ("custom", None, "unknown"), ("mystery", "http://nas.local", "local"),
    ("mystery", None, "unknown")])
def test_where_a_provider_runs(rt, name, base_url, where):
    provider = types.SimpleNamespace(name=name, base_url=base_url)
    assert rt.execution_location(provider) == where


def test_a_provider_with_no_name_or_endpoint_is_unknown(rt):
    assert rt.execution_location(object()) == "unknown"
