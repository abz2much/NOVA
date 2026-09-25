"""Phase 6 provider contracts: typed models, normalized errors, the
descriptor registry, routing isolation, raw-response privacy and adapter
lifecycle. Fakes only; nothing here contacts a real provider."""
from __future__ import annotations

import asyncio
import concurrent.futures
import http.server
import json
import sys
import threading
import types

import pytest


@pytest.fixture
def lp(load):
    return load("llm_provider")


@pytest.fixture
def pk(lp):
    """The providers package modules, loaded through the façade."""
    names = ("models", "errors", "registry", "routing", "base", "reasoning",
             "destinations", "openai_compatible", "groq", "anthropic", "ollama")
    return types.SimpleNamespace(**{n: sys.modules[f"jc.providers.{n}"] for n in names})


SECRET = "sk-SECRET-VALUE-123"


# ── Typed models ────────────────────────────────────────────────────────────

def test_request_round_trips_caller_messages_exactly(pk):
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "hi"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "t", "arguments": '{"a": 1}'}}]},
        {"role": "tool", "tool_call_id": "c", "content": "ok", "name": "t"},
    ]
    request = pk.models.ChatRequest.from_legacy(history, max_tokens=5, temperature=0.1)
    assert [m.to_openai() for m in request.messages] == history
    assert request.data_category == "vision"
    assert request.messages[2].tool_calls[0].args == {"a": 1}
    assert request.messages[3].tool_call_id == "c"


def test_tool_call_history_is_built_from_normalized_values(pk):
    call = pk.models.ToolCall(id="call_1", name="call_service", args={"entity_id": "light.a"})
    response = pk.models.ChatResponse(text="", tool_calls=(call,))
    assert response.assistant_message() == {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "call_service", "arguments": '{"entity_id": "light.a"}'}}],
    }
    assert response.to_legacy()["tool_calls"] == [
        {"id": "call_1", "name": "call_service", "args": {"entity_id": "light.a"}}]


def test_legacy_reply_normalization_drops_foreign_raw(pk):
    response = pk.models.ChatResponse.from_legacy(
        {"text": "hi", "raw": {"reasoning": "private"}, "usage": {"input_tokens": 2}},
        provider="groq", model="m")
    assert response.raw is None
    assert response.to_legacy() == {"text": "hi", "tool_calls": [], "raw": None,
                                    "usage": {"input_tokens": 2, "output_tokens": None}}


def test_image_part_repr_hides_payload(pk):
    part = pk.models.ImagePart(url="data:image/jpeg;base64,SECRETIMAGE")
    assert "SECRETIMAGE" not in repr(part)


def test_invalid_history_arguments_are_flagged_not_guessed(pk):
    message = pk.models.ChatMessage.from_openai({"role": "assistant", "tool_calls": [
        {"id": "a", "function": {"name": "t", "arguments": "not json"}},
        {"id": "b", "function": {"name": "t", "arguments": "[1]"}}]})
    assert [c.arguments_error for c in message.tool_calls] == ["invalid_json", "not_object"]
    assert all(c.args == {} for c in message.tool_calls)


# ── Normalized errors ───────────────────────────────────────────────────────

class _StatusError(Exception):
    def __init__(self, status, message="", body=None):
        super().__init__(message)
        self.status_code = status
        self.body = body


@pytest.mark.parametrize(("status", "message", "kind"), [
    (401, "bad key", "authentication_failed"),
    (403, "nope", "access_denied"),
    (404, "The model `x` does not exist", "model_not_found"),
    (404, "no route", "invalid_endpoint"),
    (400, "messages[1].content must be a string", "unsupported_capability"),
    (413, "Request too large", "invalid_request"),
    (422, "bad field", "invalid_request"),
    (408, "slow", "timeout"),
    (429, "slow down", "rate_limited"),
    (500, "oops", "provider_unavailable"),
    (503, "overloaded", "provider_unavailable"),
])
def test_status_errors_map_to_kinds(pk, status, message, kind):
    error = pk.errors.normalize_error(_StatusError(status, message), "groq")
    assert error.kind.value == kind
    assert error.status == status


@pytest.mark.parametrize(("exc", "kind"), [
    (type("APITimeoutError", (Exception,), {})("t"), "timeout"),
    (TimeoutError("t"), "timeout"),
    (type("APIConnectionError", (Exception,), {})("c"), "connection_failed"),
    (ConnectionRefusedError("refused"), "connection_failed"),
    (concurrent.futures.CancelledError(), "cancelled"),
    (RuntimeError("something odd"), "unknown"),
])
def test_exception_classes_map_to_kinds(pk, exc, kind):
    assert pk.errors.normalize_error(exc, "openai").kind.value == kind


def test_error_messages_never_expose_secrets_headers_or_bodies(pk):
    original = _StatusError(
        401, f"Error code: 401 - Authorization: Bearer {SECRET} body={{'prompt': 'private'}}",
        body={"error": {"code": "invalid_api_key", "message": f"key {SECRET}"}})
    error = pk.errors.normalize_error(original, "openai")
    assert SECRET not in str(error) and SECRET not in repr(error)
    assert "private" not in str(error) and "Bearer" not in str(error)
    assert error.code == "invalid_api_key"
    assert error.__cause__ is original


def test_free_text_codes_are_not_allowed_through(pk):
    original = _StatusError(400, "x", body={"error": {"code": "has spaces and secrets"}})
    assert pk.errors.normalize_error(original, "groq").code is None


def test_task_cancellation_is_never_normalized(pk):
    with pytest.raises(TypeError):
        pk.errors.normalize_error(asyncio.CancelledError(), "groq")


def test_retry_classification(pk):
    E = pk.errors.ProviderErrorKind
    retryable = {k for k in E if pk.errors.ProviderError(k, "groq").retryable}
    assert retryable == {E.RATE_LIMITED, E.TIMEOUT, E.CONNECTION_FAILED, E.PROVIDER_UNAVAILABLE}


@pytest.mark.parametrize(("kind", "key"), [
    ("missing_credential", "invalid_auth"),
    ("authentication_failed", "invalid_auth"),
    ("timeout", "cannot_connect"),
    ("connection_failed", "cannot_connect"),
    ("model_not_found", "unknown"),
])
def test_config_flow_error_keys(pk, kind, key):
    assert pk.errors.config_flow_error_key(pk.errors.ProviderError(kind, "groq")) == key


def test_legacy_heuristics_can_still_read_the_chained_original(pk):
    original = RuntimeError("tool_use_failed: Failed to call a function")
    error = pk.errors.normalize_error(original, "groq")
    assert "tool_use_failed" in pk.errors.error_text(error)
    assert "tool_use_failed" not in str(error)


# ── Registry and distinct identities (defect 1) ─────────────────────────────

def test_gemini_and_custom_have_their_own_identity(lp, pk):
    assert lp.PROVIDERS["gemini"].name == "gemini"
    assert lp.PROVIDERS["custom"].name == "custom"
    assert lp.PROVIDERS["openai"].name == "openai"
    assert len({lp.PROVIDERS[p] for p in ("openai", "gemini", "custom")}) == 3
    assert lp.execution_location(object.__new__(lp.PROVIDERS["gemini"])) == "cloud"
    custom = object.__new__(lp.PROVIDERS["custom"])
    custom.base_url = "http://192.168.1.20:8000/v1"
    assert lp.execution_location(custom) == "local"
    custom.base_url = "https://llm.example.com/v1"
    assert lp.execution_location(custom) == "unknown"


def test_descriptors_agree_with_the_credential_constants(load, pk):
    const = load("const")
    for pid, desc in pk.registry.DESCRIPTORS.items():
        assert desc.credential_field == const.PROVIDER_API_KEY_FIELDS[pid]
        assert desc.legacy_shared_key == (pid in const.CREDENTIAL_LEGACY_FALLBACK_PROVIDERS)
        assert desc.concurrency.max_in_flight >= 1 and desc.concurrency.reason
        assert desc.adapter.concurrency == desc.concurrency
        assert desc.adapter.name == pid
    assert pk.registry.SELF_HOSTED_PROVIDERS == {"ollama", "custom"}


def test_native_ollama_is_not_openai_compatible(lp, pk):
    assert not issubclass(lp.OllamaProvider, pk.openai_compatible.OpenAICompatibleProvider)


# ── Routing isolation ───────────────────────────────────────────────────────

def test_custom_without_its_endpoint_fails_closed(lp, pk):
    """Defect 4: custom never falls back to OpenAI's default endpoint."""
    with pytest.raises(ValueError) as exc:
        lp.create_provider("custom", "k", "my-model", None)
    assert exc.value.kind.value == "invalid_endpoint"


def test_rerouted_model_never_carries_the_cloud_credential(lp):
    provider = lp.create_provider("gemini", SECRET, "gemma4:26b", "http://gpu.lan:11434")
    assert provider.name == "ollama"
    assert provider.api_key == ""


def test_rerouted_tier_uses_ollamas_own_endpoint_and_key(pk):
    spec = pk.routing.tier_spec({
        "llm_provider": "gemini", "gemini_api_key": SECRET,
        "reasoning_provider": "gemini", "reasoning_model": "gemma4:26b",
        "ollama_base_url": "http://gpu.lan:11434", "ollama_api_key": "ollama-own",
    }, "reasoning")
    assert (spec.provider, spec.base_url, spec.api_key) == (
        "ollama", "http://gpu.lan:11434", "ollama-own")


def test_provider_spec_hides_its_credential(pk):
    spec = pk.routing.ProviderSpec(provider="groq", model="m", api_key=SECRET)
    other = pk.routing.ProviderSpec(provider="groq", model="m", api_key="other")
    assert SECRET not in repr(spec) and SECRET not in spec.fingerprint()
    assert spec.fingerprint() != other.fingerprint()


# ── Raw-response privacy ────────────────────────────────────────────────────

def _assert_private(raw):
    text = json.dumps(raw)
    for forbidden in (SECRET, "reasoning", "thinking", "private scratch", "Authorization",
                      "QUJDREVG", "request body", "x-request-id", "req_"):
        assert forbidden not in text, forbidden


def _sdk(cls, create):
    provider = object.__new__(cls)
    provider.model, provider.api_key, provider.base_url = "m", SECRET, None
    provider._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)),
        messages=types.SimpleNamespace(create=create))
    return provider


VISION_HISTORY = [{"role": "user", "content": [
    {"type": "text", "text": "request body"},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJDREVG"}}]}]


@pytest.mark.parametrize("cls_name", ["GroqProvider", "OpenAIProvider"])
def test_openai_shaped_raw_is_sanitized(lp, cls_name):
    message = types.SimpleNamespace(
        content="<think>private scratch</think>\n\nThe answer.",
        reasoning="private scratch", reasoning_content="private scratch",
        tool_calls=[types.SimpleNamespace(id="c1", function=types.SimpleNamespace(
            name="t", arguments='{"x": 1}'))])
    completion = types.SimpleNamespace(
        id="req_123", choices=[types.SimpleNamespace(message=message)], usage=None,
        _request_id="x-request-id")
    result = _sdk(getattr(lp, cls_name), lambda **kw: completion).chat(VISION_HISTORY)
    assert result["text"] == "The answer."
    assert result["raw"] == {"role": "assistant", "content": "The answer.", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": '{"x": 1}'}}]}
    _assert_private(result["raw"])


def test_anthropic_raw_drops_thinking_blocks(lp):
    blocks = [
        types.SimpleNamespace(type="thinking", thinking="private scratch", signature="sig"),
        types.SimpleNamespace(type="redacted_thinking", data="private scratch"),
        types.SimpleNamespace(type="text", text="Visible."),
    ]
    resp = types.SimpleNamespace(id="req_1", content=blocks, usage=None, model="m")
    result = _sdk(lp.AnthropicProvider, lambda **kw: resp).chat(VISION_HISTORY)
    assert result["text"] == "Visible."
    assert result["raw"] == {"role": "assistant", "content": [{"type": "text", "text": "Visible."}]}
    _assert_private(result["raw"])


def test_ollama_raw_drops_thinking_and_timings(lp, monkeypatch, pk):
    reply = {"model": "m", "message": {"role": "assistant", "thinking": "private scratch",
                                       "content": "<think>private scratch</think>\nVisible."},
             "total_duration": 1, "context": [1, 2], "prompt_eval_count": 3}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, limit=-1):
            return json.dumps(reply).encode()

    monkeypatch.setattr(pk.ollama, "_urlopen", lambda request, timeout: _Resp())
    result = lp.OllamaProvider(SECRET, "m", "http://gpu.lan:11434").chat(
        [{"role": "user", "content": "hi"}])
    assert result["text"] == "Visible."
    assert result["raw"] == {"message": {"role": "assistant", "content": "Visible."}}
    _assert_private(result["raw"])


@pytest.mark.parametrize(("content", "visible"), [
    ("<think>cut off by the token limit", ""),
    ("<THINK>\nx\n</THINK>\nHi", "Hi"),
    ("Plain.", "Plain."),
])
def test_visible_text(pk, content, visible):
    assert pk.reasoning.visible_text(content) == visible


# ── Adapter guards and lifecycle ────────────────────────────────────────────

def test_cloud_adapter_without_credential_never_sends(lp):
    calls = []
    provider = _sdk(lp.OpenAIProvider, lambda **kw: calls.append(kw))
    provider.api_key = ""
    with pytest.raises(RuntimeError) as exc:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc.value.kind.value == "missing_credential"
    assert calls == []


def test_adapter_close_is_exactly_once_and_blocks_later_calls(lp):
    closes = []
    provider = _sdk(lp.GroqProvider, lambda **kw: None)
    provider._client.close = lambda: closes.append(1)
    assert provider.close() is True
    assert provider.close() is False
    assert closes == [1]
    with pytest.raises(RuntimeError) as exc:
        provider.chat([{"role": "user", "content": "hi"}])
    assert exc.value.kind.value == "provider_unavailable"


def test_adapter_repr_hides_credential(lp):
    provider = _sdk(lp.GroqProvider, lambda **kw: None)
    assert SECRET not in repr(provider)


# ── Destination and redirect policy (native Ollama over loopback) ──────────

class _Server:
    def __init__(self, handler):
        self.seen = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.seen.append((self.command, self.path,
                                   {k.lower(): v for k, v in self.headers.items()}))
                status, headers, body = handler(self)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _handle

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_redirect_to_another_origin_drops_the_credential(lp):
    ok = json.dumps({"message": {"content": "ok"}}).encode()
    target = _Server(lambda h: (200, {"Content-Type": "application/json"}, ok))
    source = _Server(lambda h: (302, {"Location": f"http://localhost:{target.port}/api/chat"}, b""))
    try:
        lp.OllamaProvider(SECRET, "m", f"http://127.0.0.1:{source.port}").chat(
            [{"role": "user", "content": "hi"}])
    finally:
        source.close()
        target.close()
    assert source.seen[0][2]["authorization"] == f"Bearer {SECRET}"
    assert "authorization" not in target.seen[0][2]


def test_redirect_to_cloud_metadata_is_refused(lp):
    source = _Server(lambda h: (302, {"Location": "http://169.254.169.254/latest/meta-data/"}, b""))
    try:
        with pytest.raises(RuntimeError) as exc:
            lp.OllamaProvider(SECRET, "m", f"http://127.0.0.1:{source.port}").chat(
                [{"role": "user", "content": "hi"}])
    finally:
        source.close()
    assert exc.value.kind.value == "invalid_endpoint"


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest", "http://[fe80::1]:11434", "http://metadata.google.internal",
    "http://[::ffff:169.254.169.254]/", "http://100.100.100.200/", "ftp://nas.lan/",
])
def test_blocked_destinations(pk, url):
    with pytest.raises(pk.errors.ProviderError):
        pk.destinations.check_url(url)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434", "http://localhost:11434", "http://192.168.1.20:11434",
    "http://10.0.4.5:8000/v1", "http://gpu.local:11434", "https://llm.example.com/v1",
    "http://[::1]:11434",
])
def test_private_and_lan_destinations_are_allowed(pk, url):
    pk.destinations.check_url(url)


def test_hostnames_resolving_to_link_local_are_refused(pk):
    def resolver(host, port, **kw):
        return [(2, 1, 6, "", ("169.254.169.254", port))]
    with pytest.raises(pk.errors.ProviderError):
        pk.destinations.check_url("http://sneaky.example/", resolve=True, resolver=resolver)
