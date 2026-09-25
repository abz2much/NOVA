"""Characterization of provider request/response translation.

Written before the provider package existed and kept unchanged across it:
every assertion here describes behaviour the llm_provider façade already had.
The SDK adapters are driven through their ``_client`` seam (the SDKs are not
installed in the unit venv); native Ollama is driven against a real loopback
HTTP server, so no test depends on how the adapter opens connections.
"""
from __future__ import annotations

import http.server
import json
import threading
import types

import pytest


@pytest.fixture
def lp(load):
    return load("llm_provider")


# ── OpenAI-shaped SDK adapters (Groq, OpenAI) ───────────────────────────────

class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Call:
    def __init__(self, id_, name, arguments):
        self.id = id_
        self.function = _Fn(name, arguments)


def _completion(content="", calls=None, usage=(3, 4)):
    message = types.SimpleNamespace(content=content, tool_calls=calls or [])
    u = types.SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=u)


def _sdk_adapter(cls, completion, captured):
    provider = object.__new__(cls)
    provider.model = "base-model"
    provider.api_key = "sk-test"
    provider.base_url = None

    def create(**kwargs):
        captured.append(kwargs)
        return completion
    provider._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    return provider


HISTORY = [
    {"role": "system", "content": "You are Nova."},
    {"role": "user", "content": "Turn on the lamp."},
    {"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "call_service", "arguments": '{"entity_id": "light.lamp"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
]
TOOLS = [{"type": "function", "function": {
    "name": "call_service", "description": "Call a service",
    "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}}}}}]


@pytest.mark.parametrize("cls_name", ["GroqProvider", "OpenAIProvider"])
def test_openai_wire_request_is_the_callers_history(lp, cls_name):
    captured = []
    provider = _sdk_adapter(getattr(lp, cls_name), _completion("  Done.  "), captured)
    result = provider.chat(HISTORY, tools=TOOLS, max_tokens=99, temperature=0.2)

    sent = captured[0]
    assert sent["model"] == "base-model"
    assert sent["messages"] == HISTORY
    assert sent["tools"] == TOOLS and sent["tool_choice"] == "auto"
    assert sent["max_tokens"] == 99 and sent["temperature"] == 0.2
    assert result["text"] == "Done."
    assert result["tool_calls"] == []
    assert result["usage"] == {"input_tokens": 3, "output_tokens": 4}
    assert set(result) == {"text", "tool_calls", "raw", "usage"}


@pytest.mark.parametrize("cls_name", ["GroqProvider", "OpenAIProvider"])
def test_openai_wire_without_tools_sends_no_tool_fields(lp, cls_name):
    captured = []
    provider = _sdk_adapter(getattr(lp, cls_name), _completion("hi"), captured)
    provider.chat([{"role": "user", "content": "hi"}], model_override="other-model")
    assert "tools" not in captured[0] and "tool_choice" not in captured[0]
    assert captured[0]["model"] == "other-model"


@pytest.mark.parametrize("cls_name", ["GroqProvider", "OpenAIProvider"])
def test_openai_wire_tool_calls_are_parsed(lp, cls_name):
    completion = _completion("", [_Call("call_9", "call_service", '{"entity_id": "light.a"}')])
    provider = _sdk_adapter(getattr(lp, cls_name), completion, [])
    result = provider.chat([{"role": "user", "content": "go"}], tools=TOOLS)
    assert result["tool_calls"] == [
        {"id": "call_9", "name": "call_service", "args": {"entity_id": "light.a"}}]


def test_openai_image_content_passes_through_unchanged(lp):
    captured = []
    provider = _sdk_adapter(lp.OpenAIProvider, _completion("a cat"), captured)
    content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
               {"type": "text", "text": "What is this?"}]
    provider.chat([{"role": "user", "content": content}], max_tokens=300)
    assert captured[0]["messages"] == [{"role": "user", "content": content}]


# ── Anthropic ───────────────────────────────────────────────────────────────

class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


def _anthropic(lp, responses):
    provider = object.__new__(lp.AnthropicProvider)
    provider.model = "claude-test"
    provider.api_key = "sk-ant-test"
    provider.base_url = None
    captured = []

    def create(**kwargs):
        captured.append(dict(kwargs))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    provider._client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return provider, captured


def _anthropic_response(blocks, usage=(7, 8)):
    return types.SimpleNamespace(
        content=blocks,
        usage=types.SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]))


def test_anthropic_translates_system_tools_and_tool_history(lp):
    provider, captured = _anthropic(lp, [_anthropic_response([_Block("text", text="Done.")])])
    history = [
        {"role": "system", "content": "You are Nova."},
        {"role": "system", "content": [{"type": "text", "text": "Be brief."}]},
        {"role": "user", "content": "Turn on both lamps."},
        {"role": "assistant", "content": "On it.", "tool_calls": [
            {"id": "a", "type": "function",
             "function": {"name": "call_service", "arguments": '{"entity_id": "light.a"}'}},
            {"id": "b", "type": "function",
             "function": {"name": "call_service", "arguments": "not json"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "ok-a"},
        {"role": "tool", "tool_call_id": "b", "content": "ok-b"},
    ]
    result = provider.chat(history, tools=TOOLS, max_tokens=64, temperature=0.1)

    sent = captured[0]
    assert sent["system"] == "You are Nova.\n\nBe brief."
    assert sent["messages"] == [
        {"role": "user", "content": "Turn on both lamps."},
        {"role": "assistant", "content": [
            {"type": "text", "text": "On it."},
            {"type": "tool_use", "id": "a", "name": "call_service",
             "input": {"entity_id": "light.a"}},
            {"type": "tool_use", "id": "b", "name": "call_service", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "ok-a"},
            {"type": "tool_result", "tool_use_id": "b", "content": "ok-b"},
        ]},
    ]
    assert sent["tools"] == [{"name": "call_service", "description": "Call a service",
                              "input_schema": TOOLS[0]["function"]["parameters"]}]
    assert sent["max_tokens"] == 64 and sent["temperature"] == 0.1
    assert result["text"] == "Done."
    assert result["usage"] == {"input_tokens": 7, "output_tokens": 8}


def test_anthropic_converts_images(lp):
    provider, captured = _anthropic(lp, [_anthropic_response([_Block("text", text="ok")])])
    provider.chat([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        {"type": "image_url", "image_url": {"url": "https://example.test/a.jpg"}},
        {"type": "text", "text": "Describe."},
    ]}])
    assert captured[0]["messages"][0]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "image", "source": {"type": "url", "url": "https://example.test/a.jpg"}},
        {"type": "text", "text": "Describe."},
    ]


def test_anthropic_retries_once_without_deprecated_temperature(lp):
    provider, captured = _anthropic(lp, [
        RuntimeError("temperature is deprecated for this model"),
        _anthropic_response([_Block("text", text="ok")]),
    ])
    result = provider.chat([{"role": "user", "content": "hi"}], temperature=0.3)
    assert result["text"] == "ok"
    assert "temperature" in captured[0] and "temperature" not in captured[1]


def test_anthropic_parses_tool_use_blocks(lp):
    provider, _ = _anthropic(lp, [_anthropic_response([
        _Block("text", text="Let me check. "),
        _Block("tool_use", id="toolu_1", name="get_state", input={"entity_id": "lock.front"}),
    ])])
    result = provider.chat([{"role": "user", "content": "Is it locked?"}], tools=TOOLS)
    assert result["text"] == "Let me check."
    assert result["tool_calls"] == [
        {"id": "toolu_1", "name": "get_state", "args": {"entity_id": "lock.front"}}]


# ── Native Ollama over a real loopback server ───────────────────────────────

class _OllamaServer:
    def __init__(self, reply):
        self.reply = reply
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(self.rfile.read(length) or b"{}"),
                })
                body = json.dumps(outer.reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_ollama_native_request_and_response(lp, monkeypatch):
    monkeypatch.setattr(lp.OllamaProvider, "_num_ctx", staticmethod(lambda: 16384))
    reply = {"message": {"role": "assistant",
                         "content": "<think>\nprivate\n</think>\n\nThe lamp is on.",
                         "tool_calls": [{"function": {"name": "get_state",
                                                      "arguments": {"entity_id": "light.a"}}}]},
             "prompt_eval_count": 11, "eval_count": 5}
    with _OllamaServer(reply) as server:
        provider = lp.OllamaProvider("endpoint-key", "qwen3:8b", server.url + "/v1")
        result = provider.chat(
            [
                {"role": "user", "content": [
                    {"type": "text", "text": "Look."},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
                ]},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "c1", "function": {"name": "get_state", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "on"},
            ],
            tools=TOOLS, max_tokens=77, temperature=0.25, model_override="llava:7b")

    request = server.requests[0]
    body = request["body"]
    assert request["path"] == "/api/chat"
    assert request["headers"]["authorization"] == "Bearer endpoint-key"
    assert body["model"] == "llava:7b"
    assert body["stream"] is False and body["think"] is False
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"num_ctx": 16384, "num_predict": 77, "temperature": 0.25}
    assert body["tools"] == TOOLS
    assert body["messages"] == [
        {"role": "user", "content": "Look.", "images": ["QUJD"]},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "get_state", "arguments": {}}}]},
        {"role": "tool", "content": "on", "tool_name": "get_state"},
    ]
    assert result["text"] == "The lamp is on."
    assert result["usage"] == {"input_tokens": 11, "output_tokens": 5}
    assert [(c["name"], c["args"]) for c in result["tool_calls"]] == [
        ("get_state", {"entity_id": "light.a"})]
    assert result["tool_calls"][0]["id"].startswith("call_ollama_")


def test_ollama_sends_no_authorization_without_its_own_key(lp):
    with _OllamaServer({"message": {"content": "ok"}}) as server:
        lp.OllamaProvider("", "qwen3:8b", server.url).chat([{"role": "user", "content": "hi"}])
    assert "authorization" not in server.requests[0]["headers"]


# ── Registry and routing facts ──────────────────────────────────────────────

def test_provider_ids_and_order(lp):
    assert lp.list_providers() == ["groq", "openai", "ollama", "gemini", "custom", "anthropic"]


def test_default_models(lp):
    assert lp.DEFAULT_MODELS == {
        "groq": "openai/gpt-oss-120b",
        "anthropic": "claude-sonnet-5",
        "openai": "gpt-5-mini",
        "gemini": "gemini-3.6-flash",
    }


def test_ollama_constants(lp):
    assert (lp.OLLAMA_KEEP_ALIVE, lp.OLLAMA_NUM_CTX, lp.OLLAMA_TIMEOUT) == ("30m", 8192, 120.0)


def test_create_provider_requires_an_ollama_endpoint(lp):
    with pytest.raises(ValueError, match="Ollama endpoint is not configured"):
        lp.create_provider("ollama", "", "qwen3:8b", None)


def test_create_provider_rejects_unknown_provider(lp):
    with pytest.raises(ValueError):
        lp.create_provider("not-a-provider", "k", "m")
