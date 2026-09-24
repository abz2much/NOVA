"""LLM provider usage normalization + chat_with_activity (Phase 5).

groq/openai/anthropic aren't installed in this test venv (Nova imports them
lazily inside each provider's __init__), so provider instances are built via
object.__new__() to skip the real SDK client construction, with a fake
_client injected directly — exactly the seam chat() actually calls through.
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture
def lp(load):
    return load("llm_provider")


# ── fake OpenAI/Groq-shaped SDK objects ─────────────────────────────────────

class _FakeToolCall:
    def __init__(self, id_, name, arguments):
        self.id = id_
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


class _FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeUsage:
    def __init__(self, prompt_tokens=None, completion_tokens=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeCompletion:
    def __init__(self, message, usage=None):
        self.choices = [types.SimpleNamespace(message=message)]
        self.usage = usage


def _make_openai_style(lp, cls, fake_completion):
    provider = object.__new__(cls)
    provider.model = "test-model"
    provider.api_key = "x"
    provider.base_url = None
    provider._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kw: fake_completion)
        )
    )
    return provider


# ── Groq ─────────────────────────────────────────────────────────────────────

def test_groq_usage_extracted_when_present(lp):
    completion = _FakeCompletion(_FakeMessage("hi"), usage=_FakeUsage(10, 20))
    provider = _make_openai_style(lp, lp.GroqProvider, completion)
    result = provider.chat([{"role": "user", "content": "hi"}])
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 20}
    assert result["text"] == "hi"


def test_groq_usage_none_when_response_has_no_usage(lp):
    completion = _FakeCompletion(_FakeMessage("hi"), usage=None)
    provider = _make_openai_style(lp, lp.GroqProvider, completion)
    result = provider.chat([{"role": "user", "content": "hi"}])
    assert result["usage"] == {"input_tokens": None, "output_tokens": None}


def test_groq_usage_partial_fields_missing(lp):
    """A usage object missing one field (some OpenAI-compatible servers do
    this) must report None for that field specifically, not fabricate 0."""
    usage = types.SimpleNamespace(prompt_tokens=5)  # no completion_tokens attr
    completion = _FakeCompletion(_FakeMessage("hi"), usage=usage)
    provider = _make_openai_style(lp, lp.GroqProvider, completion)
    result = provider.chat([{"role": "user", "content": "hi"}])
    assert result["usage"] == {"input_tokens": 5, "output_tokens": None}


# ── OpenAI-compatible providers ──────────────────────────────────────────────

def test_openai_usage_extracted_when_present(lp):
    completion = _FakeCompletion(_FakeMessage("hi"), usage=_FakeUsage(7, 14))
    provider = _make_openai_style(lp, lp.OpenAIProvider, completion)
    result = provider.chat([{"role": "user", "content": "hi"}])
    assert result["usage"] == {"input_tokens": 7, "output_tokens": 14}


def test_openai_usage_none_when_missing(lp):
    completion = _FakeCompletion(_FakeMessage("hi"), usage=None)
    provider = _make_openai_style(lp, lp.OpenAIProvider, completion)
    result = provider.chat([{"role": "user", "content": "hi"}])
    assert result["usage"] == {"input_tokens": None, "output_tokens": None}


# ── Anthropic ─────────────────────────────────────────────────────────────────

class _FakeAnthropicBlock:
    def __init__(self, type_, text=None):
        self.type = type_
        self.text = text


class _FakeAnthropicUsage:
    def __init__(self, input_tokens=None, output_tokens=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def _make_anthropic(lp, content_blocks, usage=None):
    provider = object.__new__(lp.AnthropicProvider)
    provider.model = "claude-test"
    provider.api_key = "x"
    provider.base_url = None
    fake_resp = types.SimpleNamespace(content=content_blocks, usage=usage)
    provider._client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **kw: fake_resp)
    )
    return provider


def test_anthropic_usage_extracted_when_present(lp):
    provider = _make_anthropic(
        lp, [_FakeAnthropicBlock("text", "hi")],
        usage=_FakeAnthropicUsage(11, 22))
    result = provider.chat([{"role": "user", "content": "hi"}])
    assert result["usage"] == {"input_tokens": 11, "output_tokens": 22}
    assert result["text"] == "hi"


def test_anthropic_usage_none_when_response_has_no_usage(lp):
    provider = _make_anthropic(lp, [_FakeAnthropicBlock("text", "hi")], usage=None)
    result = provider.chat([{"role": "user", "content": "hi"}])
    assert result["usage"] == {"input_tokens": None, "output_tokens": None}


# ── execution_location ───────────────────────────────────────────────────────

def test_execution_location_cloud_for_known_hosted_providers(lp):
    for name in ("groq", "gemini", "openai", "anthropic"):
        provider = types.SimpleNamespace(name=name, base_url=None)
        assert lp.execution_location(provider) == "cloud"


def test_execution_location_local_for_ollama(lp):
    provider = types.SimpleNamespace(name="ollama", base_url="http://cloud-looking-host:11434/v1")
    assert lp.execution_location(provider) == "local"  # name alone decides for ollama


def test_execution_location_unknown_for_custom_with_no_local_hint(lp):
    provider = types.SimpleNamespace(name="custom", base_url="https://api.example.com/v1")
    assert lp.execution_location(provider) == "unknown"


@pytest.mark.parametrize("base_url", [
    "http://localhost:8000/v1",
    "http://127.0.0.1:8000/v1",
    "http://192.168.1.50:8000/v1",
    "http://my-server.local:8000/v1",
])
def test_execution_location_local_for_custom_with_local_base_url(lp, base_url):
    provider = types.SimpleNamespace(name="custom", base_url=base_url)
    assert lp.execution_location(provider) == "local"


def test_execution_location_unknown_when_custom_has_no_base_url(lp):
    provider = types.SimpleNamespace(name="custom", base_url=None)
    assert lp.execution_location(provider) == "unknown"


# ── chat_with_activity ───────────────────────────────────────────────────────

class _FakeHass:
    """Minimal enough for chat_with_activity: async_add_executor_job runs the
    callable synchronously, matching tests/fakes.py::FakeHass's own contract."""
    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def fake_provider_activity(monkeypatch):
    """Installs a fake jc.provider_activity module for one test — the real
    module's chat_with_activity local-imports via `from . import
    provider_activity`, which resolves through sys.modules/the jc package,
    not an attribute on jc.llm_provider itself. monkeypatch restores both
    afterward regardless of pass/fail."""
    import sys
    calls = []
    fake = types.SimpleNamespace(record=lambda *a, **k: calls.append(a) or True)
    monkeypatch.setitem(sys.modules, "jc.provider_activity", fake)
    return calls


def test_chat_with_activity_returns_result_unchanged(lp, fake_provider_activity):
    expected = {"text": "hi", "tool_calls": [], "raw": None,
                "usage": {"input_tokens": 1, "output_tokens": 2}}
    provider = types.SimpleNamespace(
        name="groq", model="m", base_url=None,
        chat=lambda *a, **k: expected)

    import asyncio
    result = asyncio.run(lp.chat_with_activity(
        _FakeHass(), provider, [{"role": "user", "content": "hi"}],
        role="llm", data_category="text"))
    assert result is expected


def test_chat_with_activity_records_success_with_correct_fields(lp, fake_provider_activity):
    provider = types.SimpleNamespace(
        name="groq", model="m", base_url=None,
        chat=lambda *a, **k: {"text": "hi", "tool_calls": [], "raw": None,
                              "usage": {"input_tokens": 5, "output_tokens": 6}})

    import asyncio
    asyncio.run(lp.chat_with_activity(
        _FakeHass(), provider, [{"role": "user", "content": "hi"}],
        role="llm", data_category="text"))

    calls = fake_provider_activity
    assert len(calls) == 1
    provider_arg, model_arg, role_arg, location_arg, category_arg, success_arg, in_tok, out_tok, latency = calls[0]
    assert provider_arg == "groq"
    assert model_arg == "m"
    assert role_arg == "llm"
    assert location_arg == "cloud"
    assert category_arg == "text"
    assert success_arg is True
    assert in_tok == 5 and out_tok == 6
    assert isinstance(latency, int) and latency >= 0


def test_chat_with_activity_reraises_original_exception_and_records_failure(lp, fake_provider_activity):
    def _boom(*a, **k):
        raise RuntimeError("provider exploded")

    provider = types.SimpleNamespace(name="groq", model="m", base_url=None, chat=_boom)

    import asyncio
    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(lp.chat_with_activity(
            _FakeHass(), provider, [{"role": "user", "content": "hi"}],
            role="llm", data_category="text"))

    calls = fake_provider_activity
    assert len(calls) == 1
    assert calls[0][5] is False  # success flag
    assert calls[0][6] is None and calls[0][7] is None  # no usage on failure


def test_chat_with_activity_recording_failure_does_not_affect_result(lp, monkeypatch):
    """Activity logging blowing up must never change what the caller sees —
    the successful result still comes back exactly as the provider gave it."""
    import sys

    def _boom_record(*a, **k):
        raise RuntimeError("db down")
    fake_pa = types.SimpleNamespace(record=_boom_record)
    monkeypatch.setitem(sys.modules, "jc.provider_activity", fake_pa)

    expected = {"text": "hi", "tool_calls": [], "raw": None, "usage": {}}
    provider = types.SimpleNamespace(name="groq", model="m", base_url=None,
                                     chat=lambda *a, **k: expected)

    import asyncio
    result = asyncio.run(lp.chat_with_activity(
        _FakeHass(), provider, [{"role": "user", "content": "hi"}],
        role="llm", data_category="text"))
    assert result is expected


def test_chat_with_activity_recording_failure_does_not_mask_original_exception(lp, monkeypatch):
    """If BOTH the provider call and activity recording fail, the caller
    must see the provider's own exception, never the recording failure."""
    import sys

    def _boom_record(*a, **k):
        raise RuntimeError("db down")
    fake_pa = types.SimpleNamespace(record=_boom_record)
    monkeypatch.setitem(sys.modules, "jc.provider_activity", fake_pa)

    def _boom_chat(*a, **k):
        raise ValueError("the real failure")
    provider = types.SimpleNamespace(name="groq", model="m", base_url=None, chat=_boom_chat)

    import asyncio
    with pytest.raises(ValueError, match="the real failure"):
        asyncio.run(lp.chat_with_activity(
            _FakeHass(), provider, [{"role": "user", "content": "hi"}],
            role="llm", data_category="text"))


def test_chat_with_activity_invalid_category_falls_back_to_text(lp, fake_provider_activity):
    provider = types.SimpleNamespace(
        name="groq", model="m", base_url=None,
        chat=lambda *a, **k: {"text": "hi", "tool_calls": [], "raw": None, "usage": {}})

    import asyncio
    asyncio.run(lp.chat_with_activity(
        _FakeHass(), provider, [{"role": "user", "content": "hi"}],
        role="llm", data_category="not-a-real-category"))

    assert fake_provider_activity[0][4] == "text"
