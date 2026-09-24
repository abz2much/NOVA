"""Native Ollama ``/api/chat`` provider coverage."""
from __future__ import annotations

from io import BytesIO
import json

import pytest


@pytest.fixture
def llm(load):
    return load("llm_provider")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()


def _provider(llm, monkeypatch, response, *, api_key=""):
    captured = {}

    def _urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(response)

    monkeypatch.setattr(llm, "urlopen", _urlopen)
    monkeypatch.setattr(llm.OllamaProvider, "_num_ctx", staticmethod(lambda: 16384))
    return llm.OllamaProvider(
        api_key, "qwen", "http://gpu.local:11434/v1"
    ), captured


def test_ollama_registered_as_native_provider(llm):
    assert llm.PROVIDERS["ollama"] is llm.OllamaProvider
    assert issubclass(llm.OllamaProvider, llm.LLMProvider)
    assert not issubclass(llm.OllamaProvider, llm.OpenAIProvider)


@pytest.mark.parametrize(("value", "expected"), [
    ("http://gpu.local:11434", "http://gpu.local:11434"),
    ("http://gpu.local:11434/", "http://gpu.local:11434"),
    ("http://gpu.local:11434/v1", "http://gpu.local:11434"),
    ("https://proxy.example/ollama/v1", "https://proxy.example/ollama"),
    (None, None),
])
def test_native_url_normalization(llm, value, expected):
    assert llm.OllamaProvider._normalize_url(value) == expected


def test_chat_sends_native_controls_and_parses_text_usage(llm, monkeypatch):
    provider, captured = _provider(llm, monkeypatch, {
        "message": {"role": "assistant", "content": "Nova online"},
        "prompt_eval_count": 12,
        "eval_count": 3,
        "done": True,
    })

    result = provider.chat(
        [{"role": "user", "content": "status"}],
        max_tokens=80,
        temperature=0.2,
    )

    request = captured["request"]
    payload = json.loads(request.data)
    assert request.full_url == "http://gpu.local:11434/api/chat"
    assert captured["timeout"] == llm.OLLAMA_TIMEOUT
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["keep_alive"] == llm.OLLAMA_KEEP_ALIVE
    assert payload["options"] == {
        "num_ctx": 16384, "num_predict": 80, "temperature": 0.2,
    }
    assert result["text"] == "Nova online"
    assert result["usage"] == {"input_tokens": 12, "output_tokens": 3}


@pytest.mark.parametrize(("content", "expected"), [
    (
        "<think>\nI should answer directly.\n</think>\n\nNova online",
        "Nova online",
    ),
    (
        "I should answer directly.\nThe user asked for two sentences.\n"
        "</think>\n\nNova online",
        "Nova online",
    ),
    ("Nova online", "Nova online"),
    ("Explain the literal token </think> carefully.",
     "Explain the literal token </think> carefully."),
])
def test_chat_hides_qwen_thinking_envelope(llm, monkeypatch, content, expected):
    provider, _ = _provider(
        llm, monkeypatch, {"message": {"role": "assistant", "content": content}}
    )

    result = provider.chat([{"role": "user", "content": "status"}])

    assert result["text"] == expected


def test_chat_sends_optional_bearer_auth_only_when_configured(llm, monkeypatch):
    response = {"message": {"content": "ok"}}
    protected, protected_capture = _provider(
        llm, monkeypatch, response, api_key="endpoint-secret"
    )
    protected.chat([{"role": "user", "content": "hi"}])
    assert protected_capture["request"].get_header("Authorization") == (
        "Bearer endpoint-secret"
    )

    public, public_capture = _provider(llm, monkeypatch, response)
    public.chat([{"role": "user", "content": "hi"}])
    assert public_capture["request"].get_header("Authorization") is None


def test_chat_converts_inline_images_and_tool_history(llm, monkeypatch):
    provider, captured = _provider(
        llm, monkeypatch, {"message": {"content": "done"}}
    )
    provider.chat([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is here?"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64,aW1hZ2U="}},
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "get_state", "arguments": "{\"id\":\"x\"}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "on"},
    ])

    messages = json.loads(captured["request"].data)["messages"]
    assert messages[0] == {
        "role": "user", "content": "What is here?", "images": ["aW1hZ2U="],
    }
    assert messages[1]["tool_calls"] == [{
        "function": {"name": "get_state", "arguments": {"id": "x"}},
    }]
    assert messages[2]["tool_name"] == "get_state"


def test_chat_sends_tools_and_parses_native_tool_calls(llm, monkeypatch):
    provider, captured = _provider(llm, monkeypatch, {
        "message": {
            "content": "",
            "tool_calls": [{
                "function": {
                    "name": "turn_on",
                    "arguments": {"entity_id": "light.kitchen"},
                },
            }],
        },
    })
    tools = [{
        "type": "function",
        "function": {"name": "turn_on", "description": "", "parameters": {}},
    }]
    result = provider.chat([{"role": "user", "content": "lights"}], tools=tools)

    assert json.loads(captured["request"].data)["tools"] == tools
    assert result["tool_calls"][0]["name"] == "turn_on"
    assert result["tool_calls"][0]["args"] == {"entity_id": "light.kitchen"}
    assert result["tool_calls"][0]["id"].startswith("call_ollama_")


def test_model_override_is_sent(llm, monkeypatch):
    provider, captured = _provider(
        llm, monkeypatch, {"message": {"content": "ok"}}
    )
    provider.chat(
        [{"role": "user", "content": "hi"}], model_override="vision-model"
    )
    assert json.loads(captured["request"].data)["model"] == "vision-model"


def test_remote_image_url_is_rejected(llm):
    with pytest.raises(ValueError, match="inline data URLs"):
        llm.OllamaProvider._messages_to_native([{
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": "https://example.test/camera.jpg"},
            }],
        }])


def test_http_error_surfaces_status_without_headers(llm, monkeypatch):
    provider = llm.OllamaProvider(
        "endpoint-secret", "qwen", "http://gpu.local:11434"
    )

    def _raise(*args, **kwargs):
        raise llm.HTTPError(
            "http://gpu.local:11434/api/chat",
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"error":"unauthorized"}'),
        )

    monkeypatch.setattr(llm, "urlopen", _raise)
    with pytest.raises(RuntimeError, match="Ollama HTTP 401: unauthorized") as exc:
        provider.chat([{"role": "user", "content": "hi"}])
    assert "endpoint-secret" not in str(exc.value)


def test_invalid_json_response_is_rejected(llm, monkeypatch):
    provider, _ = _provider(llm, monkeypatch, b"not-json")
    with pytest.raises(RuntimeError, match="invalid JSON"):
        provider.chat([{"role": "user", "content": "hi"}])
