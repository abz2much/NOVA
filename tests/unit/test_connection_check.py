"""The setup-time LLM check (llm_provider.test_connection, v6.92.0).

Setup should fail fast with a config-flow error key on a bad URL or key,
rather than install into a broken state. ``None`` means the model answered.
"""
from __future__ import annotations

import pytest

OK, CANT_CONNECT, BAD_AUTH, UNKNOWN = None, "cannot_connect", "invalid_auth", "unknown"


@pytest.fixture
def llm(load):
    return load("llm_provider")


class StubModel:
    """A model client that answers with ``reply`` or raises ``error``.

    Records each chat() call so a test can see whether the check asked."""

    def __init__(self, reply: str = "pong", *, error: BaseException | None = None,
                 tool_calls: list | None = None) -> None:
        self.reply, self.error = reply, error
        self.tool_calls = tool_calls or []
        self.asked: list = []

    def chat(self, messages, tools=None, max_tokens=512, **kwargs):
        self.asked.append(messages)
        if self.error is not None:
            raise self.error
        return {"text": self.reply, "tool_calls": list(self.tool_calls)}


@pytest.fixture
def use_client(llm, monkeypatch):
    """Make create_provider hand back ``client`` and remember its arguments."""
    built: list[tuple] = []

    def _use(client):
        def factory(provider, key, model, base_url=None):
            built.append((provider, key, model, base_url))
            return client
        monkeypatch.setattr(llm, "create_provider", factory)
        return built

    return _use


# ── classifying a failure into a config-flow key ────────────────────────────

@pytest.mark.parametrize("error, key", [
    (Exception("401 Unauthorized: invalid api key"), BAD_AUTH),
    (Exception("Authentication failed"), BAD_AUTH),
    (Exception("403 Forbidden"), BAD_AUTH),
    (Exception("Connection refused"), CANT_CONNECT),
    (Exception("Cannot connect to host: timed out"), CANT_CONNECT),
    (OSError("getaddrinfo failed"), CANT_CONNECT),
    (Exception("Network is unreachable"), CANT_CONNECT),
    (Exception("something entirely unexpected"), UNKNOWN),
    (ValueError(""), UNKNOWN),
])
def test_error_classification(llm, error, key):
    assert llm._classify_conn_error(error) == key


# ── the end-to-end check ────────────────────────────────────────────────────

async def test_answering_model_passes_and_is_asked_once(llm, fake_hass, use_client):
    model = StubModel("pong")
    built = use_client(model)
    result = await llm.test_connection(
        fake_hass, "ollama", "", "gemma4:26b", "http://x:11434/v1")
    assert result is OK
    assert len(model.asked) == 1
    assert built == [("ollama", "", "gemma4:26b", "http://x:11434/v1")]


async def test_tool_call_only_reply_still_counts_as_an_answer(llm, fake_hass, use_client):
    use_client(StubModel("", tool_calls=[{"id": "t1", "name": "noop", "args": {}}]))
    assert await llm.test_connection(fake_hass, "groq", "test-token", "m", None) is OK


async def test_blank_base_url_is_passed_as_none(llm, fake_hass, use_client):
    built = use_client(StubModel())
    assert await llm.test_connection(fake_hass, "groq", "test-token", "m", "") is OK
    assert built[0][3] is None


async def test_no_client_built_means_cannot_connect(llm, fake_hass, use_client):
    use_client(None)
    assert await llm.test_connection(fake_hass, "ollama", "", "m", "http://x") == CANT_CONNECT


@pytest.mark.parametrize("reply", ["", "   \n\t"], ids=["empty", "whitespace"])
async def test_empty_completion_is_rejected(llm, fake_hass, use_client, reply):
    use_client(StubModel(reply))
    assert await llm.test_connection(fake_hass, "ollama", "", "m", "http://x") == UNKNOWN


@pytest.mark.parametrize("provider, key, base_url, error, expected", [
    ("groq", "bad", None, Exception("401 invalid api key"), BAD_AUTH),
    ("ollama", "", "http://down:11434", OSError("Connection refused"), CANT_CONNECT),
    ("groq", "test-token", None, RuntimeError("model exploded"), UNKNOWN),
], ids=["auth", "unreachable", "other"])
async def test_chat_failure_maps_to_key(llm, fake_hass, use_client,
                                        provider, key, base_url, error, expected):
    use_client(StubModel(error=error))
    assert await llm.test_connection(fake_hass, provider, key, "m", base_url) == expected


@pytest.mark.parametrize("error, expected", [
    (Exception("could not connect to endpoint"), CANT_CONNECT),
    (Exception("invalid api key format"), BAD_AUTH),
])
async def test_failure_while_building_client(llm, fake_hass, monkeypatch, error, expected):
    def explode(*args, **kwargs):
        raise error
    monkeypatch.setattr(llm, "create_provider", explode)
    assert await llm.test_connection(fake_hass, "ollama", "", "m", "http://x") == expected


async def test_metadata_endpoint_is_refused_before_any_client(llm, fake_hass, use_client):
    # A self-hosted URL pointing at a cloud metadata address never gets a request.
    model = StubModel()
    built = use_client(model)
    result = await llm.test_connection(
        fake_hass, "ollama", "", "m", "http://169.254.169.254/v1")
    assert result == CANT_CONNECT
    assert built == [] and model.asked == []
