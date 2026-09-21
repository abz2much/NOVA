"""Vision provider handling (issue #17): text-only model for vision + the
blocking provider construction.

- _make_client caches a provider per (provider, model, key, base_url), so a
  provider is constructed once (callers run it in an executor, so this keeps the
  blocking SSL setup off the hot path).
- _vision_model_rejects_images turns the Groq/OpenAI 400 for a text-only model
  ("messages[1].content must be a string") into an actionable signal.
"""
import sys
import types

import pytest


@pytest.fixture
def cam(load, monkeypatch):
    if "aiohttp" not in sys.modules:
        monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    return load("cam" if False else "camera")


class _Entry:
    entry_id = "e1"
    options: dict = {}
    data = {"api_key": "k", "llm_base_url": ""}


class _Entries:
    def async_entries(self, domain):
        return [_Entry()]


class _Hass:
    def __init__(self):
        self.data = {}
        self.config_entries = _Entries()


def test_make_client_caches_provider(cam, monkeypatch):
    cam._PROVIDER_CACHE.clear()
    calls = {"n": 0}

    def fake_create(provider, api_key, model, base_url):
        calls["n"] += 1
        return types.SimpleNamespace(provider=provider, model=model)

    import importlib
    lp = importlib.import_module(cam.__name__.rsplit(".", 1)[0] + ".llm_provider")
    monkeypatch.setattr(lp, "create_provider", fake_create)

    hass = _Hass()
    c1 = cam._make_client(hass, "groq", "some/vision-model", fallback="FB")
    c2 = cam._make_client(hass, "groq", "some/vision-model", fallback="FB")
    assert c1 is c2                      # cached — same instance
    assert calls["n"] == 1               # constructed once
    # a different model is a different cache entry
    cam._make_client(hass, "groq", "other-model", fallback="FB")
    assert calls["n"] == 2


def test_make_client_returns_fallback_without_key(cam):
    cam._PROVIDER_CACHE.clear()

    class _NoKeyEntry(_Entry):
        data = {"api_key": "", "groq_api_key": ""}

    class _NoKeyEntries:
        def async_entries(self, domain):
            return [_NoKeyEntry()]

    hass = _Hass()
    hass.config_entries = _NoKeyEntries()
    assert cam._make_client(hass, "groq", "m", fallback="FB") == "FB"


def test_make_client_uses_dedicated_field_not_shared_key(cam, monkeypatch):
    """Phase 2, v7.107.0 regression: vision/camera-reasoning provider
    resolution used to always send the shared primary key (or its
    groq_api_key alias) regardless of which provider was actually
    configured — e.g. vision_provider=openai while the primary is groq
    would send the Groq key to OpenAI. Each provider must get only its own
    dedicated credential."""
    cam._PROVIDER_CACHE.clear()
    seen = {}

    def fake_create(provider, api_key, model, base_url):
        seen["provider"], seen["api_key"] = provider, api_key
        return types.SimpleNamespace(provider=provider, model=model)

    import importlib
    lp = importlib.import_module(cam.__name__.rsplit(".", 1)[0] + ".llm_provider")
    monkeypatch.setattr(lp, "create_provider", fake_create)

    class _MismatchedEntry(_Entry):
        # Primary is groq with a shared key; this role is configured for
        # openai, which has NO dedicated key of its own yet.
        data = {"api_key": "GROQ-SHARED-KEY", "llm_provider": "groq"}

    class _MismatchedEntries:
        def async_entries(self, domain):
            return [_MismatchedEntry()]

    hass = _Hass()
    hass.config_entries = _MismatchedEntries()
    result = cam._make_client(hass, "openai", "gpt-4o-mini", fallback="FB")

    assert result == "FB"  # no key resolved for openai -> falls back, doesn't leak groq's


def test_make_client_ollama_needs_no_credential(cam, monkeypatch):
    """Ollama alone is a normal, fully working configuration with an empty
    credential — it must not fall back just because no key is set."""
    cam._PROVIDER_CACHE.clear()
    seen = {}

    def fake_create(provider, api_key, model, base_url):
        seen["provider"], seen["api_key"] = provider, api_key
        return types.SimpleNamespace(provider=provider, model=model)

    import importlib
    lp = importlib.import_module(cam.__name__.rsplit(".", 1)[0] + ".llm_provider")
    monkeypatch.setattr(lp, "create_provider", fake_create)

    class _OllamaEntry(_Entry):
        data = {"api_key": "", "llm_provider": "ollama", "llm_base_url": "http://ollama.lan:11434"}

    class _OllamaEntries:
        def async_entries(self, domain):
            return [_OllamaEntry()]

    hass = _Hass()
    hass.config_entries = _OllamaEntries()
    result = cam._make_client(hass, "ollama", "llava", fallback="FB")

    assert result != "FB"
    assert seen["provider"] == "ollama"
    assert seen["api_key"] == ""


def test_vision_model_rejects_images_classifier(cam):
    groq_400 = ("Error code: 400 - {'error': {'message': "
                "'messages[1].content must be a string', 'type': "
                "'invalid_request_error'}}")
    assert cam._vision_model_rejects_images(Exception(groq_400)) is True
    assert cam._vision_model_rejects_images(Exception("rate limit exceeded")) is False
    assert cam._vision_model_rejects_images(Exception("connection reset")) is False
