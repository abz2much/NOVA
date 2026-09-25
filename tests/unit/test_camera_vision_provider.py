"""Vision provider handling (issue #17): text-only model for vision + the
blocking provider construction.

- the vision and camera-reasoning clients are owned by the entry's
  ProviderManager (pooled per configuration, replaced and closed on change,
  closed on unload); without a loaded entry they are closed after use.
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


def _lp(cam):
    import importlib
    return importlib.import_module(cam.__name__.rsplit(".", 1)[0] + ".llm_provider")


class _Client:
    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        self.closed = 0

    def close(self):
        self.closed += 1


def _patch_config(cam, monkeypatch, config):
    async def fake_config(hass):
        return dict(config)
    monkeypatch.setattr(cam, "_camera_config", fake_config)


def _counting_create(cam, monkeypatch):
    built = []

    def fake_create(provider, api_key, model, base_url):
        client = _Client(provider, model)
        client.api_key, client.base_url = api_key, base_url
        built.append(client)
        return client

    monkeypatch.setattr(_lp(cam), "create_provider", fake_create)
    return built


# ── Spec resolution ─────────────────────────────────────────────────────────

def test_client_spec_returns_none_without_key(cam):
    assert cam._client_spec({"api_key": "", "groq_api_key": ""}, "groq", "m") is None


def test_client_spec_uses_dedicated_field_not_shared_key(cam):
    """Phase 2, v7.107.0 regression: a role configured for openai must never
    receive the Groq primary's shared key."""
    config = {"api_key": "GROQ-SHARED-KEY", "llm_provider": "groq"}
    assert cam._client_spec(config, "openai", "gpt-4o-mini") is None
    spec = cam._client_spec({**config, "openai_api_key": "sk-own"}, "openai", "gpt-4o-mini")
    assert spec.api_key == "sk-own"


def test_client_spec_ollama_needs_no_credential(cam):
    spec = cam._client_spec(
        {"api_key": "", "llm_provider": "ollama", "llm_base_url": "http://ollama.lan:11434"},
        "ollama", "llava")
    assert spec is not None
    assert spec.api_key == ""
    assert spec.base_url == "http://ollama.lan:11434"


def test_client_spec_ignores_stale_legacy_endpoint_after_migration(cam):
    """Phase 6 defect 3: once the endpoints were split, the old shared
    llm_base_url must not be used for Ollama or custom."""
    config = {"llm_base_url": "http://stale.lan:11434",
              "self_hosted_endpoints_migrated": True, "ollama_base_url": ""}
    spec = cam._client_spec(config, "ollama", "llava")
    assert spec.base_url is None
    migrated = {**config, "ollama_base_url": "http://gpu.lan:11434"}
    assert cam._client_spec(migrated, "ollama", "llava").base_url == "http://gpu.lan:11434"


@pytest.mark.asyncio
async def test_camera_config_carries_secrets_and_migration_flag(cam, monkeypatch, fake_hass):
    """Phase 6 defects 2 and 3: the camera roles resolve from the full
    effective configuration, including the secrets.yaml credential overlay
    and the endpoint migration flag, not from single-key reads."""
    import importlib
    pkg = cam.__name__.rsplit(".", 1)[0]
    nova_config = importlib.import_module(pkg + ".nova_config")
    ha_secrets = importlib.import_module(pkg + ".ha_secrets")
    monkeypatch.setattr(nova_config, "get_all", lambda: {
        "self_hosted_endpoints_migrated": True, "ollama_base_url": ""})
    monkeypatch.setattr(ha_secrets, "overlay_credentials",
                        lambda merged: {**merged, "openai_api_key": "sk-from-secrets"})

    class _E:
        entry_id = "e1"
        data = {"llm_base_url": "http://stale.lan:11434"}
        options = {}

    fake_hass.config_entries = types.SimpleNamespace(async_entries=lambda domain=None: [_E()])
    config = await cam._camera_config(fake_hass)

    assert cam._client_spec(config, "openai", "gpt-4o").api_key == "sk-from-secrets"
    assert cam._client_spec(config, "ollama", "llava").base_url is None


# ── Lifecycle: runtime-owned pooling, replacement, fallback ─────────────────

@pytest.mark.asyncio
async def test_camera_client_is_pooled_by_the_runtime_manager(cam, monkeypatch, fake_hass,
                                                             nova_runtime, load):
    manager_mod = load("providers.manager")
    manager = manager_mod.ProviderManager(fake_hass)
    nova_runtime(fake_hass, providers=manager)
    _patch_config(cam, monkeypatch, {"groq_api_key": "gsk"})
    built = _counting_create(cam, monkeypatch)

    async with cam._camera_client(fake_hass, "groq", "v1", "FB", binding="vision") as c1:
        pass
    async with cam._camera_client(fake_hass, "groq", "v1", "FB", binding="vision") as c2:
        pass
    assert c1 is c2 and len(built) == 1          # reused, not rebuilt
    assert c1.closed == 0                         # still bound as "vision"

    async with cam._camera_client(fake_hass, "groq", "v2", "FB", binding="vision") as c3:
        pass
    assert c3 is not c1
    await manager._drain()
    assert c1.closed == 1                         # replaced client closed once

    await manager.async_close()                   # unload
    assert c3.closed == 1 and c1.closed == 1


@pytest.mark.asyncio
async def test_camera_client_without_runtime_is_closed_after_use(cam, monkeypatch, fake_hass):
    fake_hass.config_entries = types.SimpleNamespace(async_entries=lambda domain=None: [])
    _patch_config(cam, monkeypatch, {"groq_api_key": "gsk"})
    built = _counting_create(cam, monkeypatch)

    async with cam._camera_client(fake_hass, "groq", "v1", "FB", binding="vision") as client:
        assert client.closed == 0
    assert built == [client]
    assert client.closed == 1


@pytest.mark.asyncio
async def test_camera_client_falls_back_when_no_client_can_be_built(cam, monkeypatch, fake_hass):
    fake_hass.config_entries = types.SimpleNamespace(async_entries=lambda domain=None: [])
    _patch_config(cam, monkeypatch, {})
    async with cam._camera_client(fake_hass, "openai", "gpt-4o", "FB", binding="vision") as c:
        assert c == "FB"

    def boom(*a):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(_lp(cam), "create_provider", boom)
    _patch_config(cam, monkeypatch, {"groq_api_key": "gsk"})
    async with cam._camera_client(fake_hass, "groq", "m", "FB", binding="vision") as c:
        assert c == "FB"


def test_camera_module_keeps_no_provider_cache(cam):
    """Phase 6 defect 7: no process-global client cache survives unload."""
    assert not hasattr(cam, "_PROVIDER_CACHE")
    assert not hasattr(cam, "_make_client")


def test_vision_model_rejects_images_classifier(cam):
    groq_400 = ("Error code: 400 - {'error': {'message': "
                "'messages[1].content must be a string', 'type': "
                "'invalid_request_error'}}")
    assert cam._vision_model_rejects_images(Exception(groq_400)) is True
    assert cam._vision_model_rejects_images(Exception("rate limit exceeded")) is False
    assert cam._vision_model_rejects_images(Exception("connection reset")) is False


def test_vision_rejection_recognised_from_normalized_error(cam, load):
    errors = load("providers.errors")
    exc = errors.ProviderError(errors.ProviderErrorKind.UNSUPPORTED_CAPABILITY, "groq")
    assert cam._vision_model_rejects_images(exc) is True
