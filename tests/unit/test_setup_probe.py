"""Model tests for the first run setup screens (setup_probe.py)."""
from __future__ import annotations

import asyncio
import base64
import sys
import types

import pytest

if "aiohttp" not in sys.modules:
    _aiohttp = types.ModuleType("aiohttp")
    _aiohttp.ClientTimeout = lambda **kw: None
    _aiohttp.ClientSession = object
    sys.modules["aiohttp"] = _aiohttp


def test_rejects_images_matches_groq_text_only_error(load):
    errors = load("providers.errors")
    assert errors.rejects_images(Exception("messages[1].content must be a string")) is True
    assert errors.rejects_images(
        errors.ProviderError(errors.ProviderErrorKind.UNSUPPORTED_CAPABILITY, "groq")) is True
    assert errors.rejects_images(
        errors.ProviderError(errors.ProviderErrorKind.RATE_LIMITED, "groq")) is False


def test_default_vision_model_is_shared(load):
    const = load("const")
    assert const.DEFAULT_VISION_MODEL == "qwen/qwen3.8-27b"


class _HTTPError(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status_code = status


def test_openai_picture_refusal_is_unsupported_capability(load):
    errors = load("providers.errors")
    exc = _HTTPError(400, "Invalid content type. image_url is only supported by certain models.")
    assert errors.normalize_error(exc, "openai").kind is errors.ProviderErrorKind.UNSUPPORTED_CAPABILITY


def test_a_refused_key_is_authentication_failed(load):
    # Proves the chain behind "a key in the wrong field is refused": a 401
    # from the provider becomes authentication_failed (setup_roles maps that
    # to invalid_auth, tested in test_setup_roles.py).
    errors = load("providers.errors")
    assert errors.normalize_error(_HTTPError(401, "Incorrect API key provided"), "openai").kind \
        is errors.ProviderErrorKind.AUTHENTICATION_FAILED
    # Google answers a bad key with 400, not 401.
    # The second is Gemini's real chat endpoint reply to a bad key (checked
    # live with a fake key, 2026-10-04); its body is a JSON list.
    for text in ("API key not valid. Please pass a valid API key.",
                 '[{"error": {"code": 400, "message": "Please pass a valid API key", '
                 '"status": "INVALID_ARGUMENT"}}]',
                 "API_KEY_INVALID", "Invalid API key"):
        assert errors.normalize_error(_HTTPError(400, text), "gemini").kind \
            is errors.ProviderErrorKind.AUTHENTICATION_FAILED, text
    # A 400 that is not about the key is unchanged.
    assert errors.normalize_error(_HTTPError(400, "bad field"), "gemini").kind \
        is errors.ProviderErrorKind.INVALID_REQUEST


@pytest.fixture
def sp(load):
    return load("setup_probe")


def test_test_picture_is_a_valid_png(sp):
    head, data = sp.TEST_PICTURE.split(",", 1)
    assert head == "data:image/png;base64"
    assert base64.b64decode(data)[:8] == b"\x89PNG\r\n\x1a\n"


class _Resp:
    def __init__(self, text):
        self.text, self.tool_calls = text, []


def _wire(sp, monkeypatch, outcome):
    """Fake the client and the chat call. `outcome(job_model, messages)`
    returns text or raises."""
    calls = []
    llm = __import__("jc.llm_provider", fromlist=["x"])
    monkeypatch.setattr(llm, "create_provider",
                        lambda provider, key, model, base: type("C", (), {"model": model})())
    activity = __import__("jc.providers.activity", fromlist=["x"])
    manager = __import__("jc.providers.manager", fromlist=["x"])

    async def fake_chat(hass, client, messages, **kw):
        calls.append((client.model, messages, kw))
        return _Resp(outcome(client.model, messages))

    async def fake_close(hass, client):
        return None
    monkeypatch.setattr(activity, "execute_chat", fake_chat)
    monkeypatch.setattr(manager, "async_close_client", fake_close)
    return calls


async def test_text_probe_passes(sp, fake_hass, monkeypatch):
    calls = _wire(sp, monkeypatch, lambda m, msgs: "pong")
    assert await sp.probe_model(fake_hass, sp.Job("groq", "m"), "k", None) is None
    assert calls[0][1][0]["content"] == "Reply with the word pong."


async def test_probe_leaves_room_for_reasoning_models(sp, fake_hass, monkeypatch):
    # Nova's defaults are reasoning models; they spend output tokens thinking
    # first, so a tiny budget returns an empty reply.
    calls = _wire(sp, monkeypatch, lambda m, msgs: "pong")
    await sp.probe_model(fake_hass, sp.Job("groq", "m"), "k", None)
    assert calls[0][2]["max_tokens"] == 256


async def test_picture_probe_sends_the_picture(sp, fake_hass, monkeypatch):
    calls = _wire(sp, monkeypatch, lambda m, msgs: "red")
    assert await sp.probe_model(fake_hass, sp.Job("gemini", "v", picture=True), "k", None) is None
    parts = calls[0][1][0]["content"]
    assert parts[1] == {"type": "image_url", "image_url": {"url": sp.TEST_PICTURE}}
    assert calls[0][2]["data_category"] == "vision"


async def test_picture_refusal_is_unsupported_capability(sp, fake_hass, monkeypatch):
    def refuse(m, msgs):
        raise Exception("messages[1].content must be a string")
    _wire(sp, monkeypatch, refuse)
    assert await sp.probe_model(
        fake_hass, sp.Job("groq", "t", picture=True), "k", None) == "unsupported_capability"


async def test_rate_limit_is_not_reported_as_no_pictures(sp, fake_hass, monkeypatch):
    errors = __import__("jc.providers.errors", fromlist=["x"])

    def limited(m, msgs):
        raise errors.ProviderError(errors.ProviderErrorKind.RATE_LIMITED, "groq")
    _wire(sp, monkeypatch, limited)
    assert await sp.probe_model(
        fake_hass, sp.Job("groq", "t", picture=True), "k", None) == "rate_limited"


async def test_empty_reply_fails_and_is_logged(sp, fake_hass, monkeypatch, caplog):
    # The screen says "the provider's reason is in Home Assistant's log", so
    # every failure path must write one.
    _wire(sp, monkeypatch, lambda m, msgs: "   ")
    assert await sp.probe_model(fake_hass, sp.Job("groq", "m"), "k", None) == "malformed_response"
    assert "groq model m failed its test" in caplog.text


async def test_client_build_failure_is_logged(sp, fake_hass, monkeypatch, caplog):
    llm = __import__("jc.llm_provider", fromlist=["x"])

    def broken(*a):
        raise RuntimeError("no sdk")
    monkeypatch.setattr(llm, "create_provider", broken)
    assert await sp.probe_model(fake_hass, sp.Job("openai", "m"), "k", None) is not None
    assert "openai model m failed its test" in caplog.text


async def test_same_model_for_several_roles_is_tested_once(sp, fake_hass, monkeypatch):
    calls = _wire(sp, monkeypatch, lambda m, msgs: "ok")
    job = sp.Job("groq", "m")
    out = await sp.probe_all(fake_hass, {"classifier": job, "reasoning": job,
                                         "camera_reasoning": job}, {"groq": ("k", None)})
    assert out == {"classifier": None, "reasoning": None, "camera_reasoning": None}
    assert len(calls) == 1


async def test_ollama_models_run_one_after_another(sp, fake_hass, monkeypatch):
    running = {"now": 0, "most": 0}

    async def slow_probe(hass, job, key, base):
        running["now"] += 1
        running["most"] = max(running["most"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1
        return None
    monkeypatch.setattr(sp, "probe_model", slow_probe)
    jobs = {"conversation": sp.Job("ollama", "a"), "classifier": sp.Job("ollama", "b"),
            "vision": sp.Job("ollama", "c", picture=True)}
    await sp.probe_all(fake_hass, jobs, {"ollama": ("", "http://x:11434")})
    assert running["most"] == 1


async def test_different_providers_run_together(sp, fake_hass, monkeypatch):
    running = {"now": 0, "most": 0}

    async def slow_probe(hass, job, key, base):
        running["now"] += 1
        running["most"] = max(running["most"], running["now"])
        await asyncio.sleep(0.01)
        running["now"] -= 1
        return None
    monkeypatch.setattr(sp, "probe_model", slow_probe)
    jobs = {"conversation": sp.Job("anthropic", "a"), "classifier": sp.Job("groq", "b")}
    await sp.probe_all(fake_hass, jobs, {"anthropic": ("k", None), "groq": ("k", None)})
    assert running["most"] == 2
