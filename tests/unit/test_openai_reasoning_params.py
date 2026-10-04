"""OpenAI's reasoning models need max_completion_tokens and no temperature.
Fake SDK client only; nothing contacts OpenAI."""
import sys
import types

import pytest


@pytest.fixture
def lp(load):
    return load("llm_provider")


def _provider(lp, cls_name, model):
    sent = {}

    def create(**kwargs):
        sent.update(kwargs)
        message = types.SimpleNamespace(content="ok", tool_calls=[])
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)

    provider = object.__new__(getattr(lp, cls_name))
    provider.model, provider.api_key, provider.base_url = model, "k", None
    provider._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    return provider, sent


def _request(lp):
    models = sys.modules["jc.providers.models"]
    return models.ChatRequest.from_legacy(
        [{"role": "user", "content": "hi"}], None, 64, 0.0, None)


@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5", "o3-mini", "o4-mini", "o1",
                                   "gpt-5-mini-2025-08-07", "ft:gpt-5-mini:org::abc123",
                                   "ft:o4-mini:org::abc123"])
def test_openai_reasoning_models_get_max_completion_tokens(lp, model):
    """Hidden reasoning comes out of max_completion_tokens, so room for it is
    added on top of what the caller asked for, and the effort is set low.
    Without that, a 64 token budget can run out before any text is written."""
    oc = sys.modules["jc.providers.openai_compatible"]
    provider, sent = _provider(lp, "OpenAIProvider", model)
    provider.complete(_request(lp))
    assert sent["max_completion_tokens"] == 64 + oc._REASONING_TOKEN_ROOM
    assert sent["reasoning_effort"] == "low"
    assert "max_tokens" not in sent and "temperature" not in sent


@pytest.mark.parametrize("model", ["gpt-5-chat-latest", "o1-mini", "o1-preview"])
def test_reasoning_models_that_reject_effort_get_none(lp, model):
    provider, sent = _provider(lp, "OpenAIProvider", model)
    provider.complete(_request(lp))
    assert sent["max_completion_tokens"] > 64
    assert "reasoning_effort" not in sent and "max_tokens" not in sent


def test_older_openai_models_are_unchanged(lp):
    provider, sent = _provider(lp, "OpenAIProvider", "gpt-4o")
    provider.complete(_request(lp))
    assert sent["max_tokens"] == 64 and sent["temperature"] == 0.0


@pytest.mark.parametrize("cls_name,model", [("GeminiProvider", "gemini-3.6-flash"),
                                            ("CustomProvider", "gpt-5-mini")])
def test_other_openai_compatible_providers_are_unchanged(lp, cls_name, model):
    provider, sent = _provider(lp, cls_name, model)
    provider.complete(_request(lp))
    assert sent["max_tokens"] == 64 and sent["temperature"] == 0.0
