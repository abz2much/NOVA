"""Native Gemini adapter (Interactions API): request building, replay of the
model's recorded steps, response parsing, error mapping and the transport.

Fakes only; nothing here contacts Google."""
from __future__ import annotations

import copy
import io
import json
import sys
import types
from urllib.error import HTTPError, URLError

import pytest

SECRET = "AIza-SECRET-KEY-123"
SIG = "Cj0Bsig/with+base64=="
THOUGHT = {"type": "thought", "signature": SIG}


@pytest.fixture
def lp(load):
    return load("llm_provider")


@pytest.fixture
def pk(lp):
    names = ("models", "errors", "registry", "gemini", "gemini_wire", "openai_wire",
             "capabilities")
    return types.SimpleNamespace(**{n: sys.modules[f"jc.providers.{n}"] for n in names})


@pytest.fixture(autouse=True)
def pauses(pk, monkeypatch):
    """Retries never really wait; the waits asked for are recorded."""
    waits: list[float] = []
    monkeypatch.setattr(pk.gemini, "_pause", waits.append)
    monkeypatch.setattr(pk.gemini.random, "uniform", lambda a, b: 0.0)
    return waits


def _request(pk, messages, tools=None, max_tokens=64):
    return pk.models.ChatRequest.from_legacy(messages, tools, max_tokens, 0.7)


def _tool(name="control_device"):
    return {"type": "function", "function": {
        "name": name, "description": "Do it.",
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"entity_id": {"type": "string"}}}}}


def _completed(steps, **extra):
    return {"id": "v1_x", "status": "completed", "steps": steps, **extra}


def _call_step(call_id="c1", name="control_device", args=None):
    return {"type": "function_call", "id": call_id, "name": name,
            "arguments": args if args is not None else {"entity_id": "light.a"}}


def _text_step(text):
    return {"type": "model_output", "content": [{"type": "text", "text": text}]}


# ── Request building ────────────────────────────────────────────────────────

def test_request_is_stateless_with_system_instruction_and_steps(pk):
    request = _request(pk, [
        {"role": "system", "content": "You are Nova."},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": [{"type": "text", "text": "again"}]},
    ])
    body = pk.gemini_wire.build_request(request, "gemini-3.6-flash")
    assert body["store"] is False
    assert body["model"] == "gemini-3.6-flash"
    assert body["system_instruction"] == "You are Nova."
    assert body["input"] == [
        {"type": "user_input", "content": [{"type": "text", "text": "hello"}]},
        {"type": "model_output", "content": [{"type": "text", "text": "hi there"}]},
        {"type": "user_input", "content": [{"type": "text", "text": "again"}]},
    ]
    assert "temperature" not in json.dumps(body)


def test_thinking_models_get_a_low_level_and_room_for_the_thoughts(pk):
    request = _request(pk, [{"role": "user", "content": "x"}], max_tokens=64)
    for model in ("gemini-2.5-flash", "gemini-3.6-flash", "gemini-3.8-flash", "gemini-4-argon"):
        config = pk.gemini_wire.build_request(request, model)["generation_config"]
        assert config["thinking_level"] == "low"
        assert config["max_output_tokens"] == 64 + pk.gemini_wire.THINKING_TOKEN_ROOM


def test_other_models_get_neither_a_level_nor_extra_room(pk):
    request = _request(pk, [{"role": "user", "content": "x"}], max_tokens=64)
    for model in ("gemini-2.0-flash", "gemma-4-31b-it"):
        config = pk.gemini_wire.build_request(request, model)["generation_config"]
        assert config == {"max_output_tokens": 64, "temperature": 0.7}


def test_thinking_can_be_left_out_for_the_retry(pk):
    request = _request(pk, [{"role": "user", "content": "x"}])
    config = pk.gemini_wire.build_request(
        request, "gemini-3.6-flash", thinking=False)["generation_config"]
    assert "thinking_level" not in config


def test_tools_become_function_declarations_without_unsupported_keys(pk):
    request = _request(pk, [{"role": "user", "content": "x"}], tools=[_tool()])
    tools = pk.gemini_wire.build_request(request, "gemini-3.6-flash")["tools"]
    assert tools == [{
        "type": "function", "name": "control_device", "description": "Do it.",
        "parameters": {"type": "object",
                       "properties": {"entity_id": {"type": "string"}}}}]


def test_an_inline_image_becomes_an_image_part(pk):
    request = _request(pk, [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        {"type": "text", "text": "what is this?"}]}])
    steps = pk.gemini_wire.build_request(request, "gemini-3.6-flash")["input"]
    assert steps[0]["content"] == [
        {"type": "image", "data": "QUJD", "mime_type": "image/jpeg"},
        {"type": "text", "text": "what is this?"}]


@pytest.mark.parametrize("url", ["https://cam.example/x.jpg", "data:image/png,rawbytes", ""])
def test_a_remote_or_non_base64_image_is_refused_before_any_call(pk, url):
    request = _request(pk, [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": url}}]}])
    with pytest.raises(ValueError) as exc:
        pk.gemini_wire.build_request(request, "gemini-3.6-flash")
    assert exc.value.kind.value == "invalid_request"


# ── Replay of the model's own steps ─────────────────────────────────────────

def _first_turn(pk, steps):
    return pk.gemini_wire.parse_interaction(_completed(steps), model="gemini-3.6-flash")


def _history(response, extra=()):
    return [
        {"role": "user", "content": "turn on the light"},
        response.assistant_message(),
        {"role": "tool", "tool_call_id": "c1", "name": "control_device", "content": "ok"},
        *extra,
    ]


def test_recorded_steps_are_replayed_exactly_as_received(pk):
    steps = [THOUGHT, _text_step("On it."), _call_step()]
    response = _first_turn(pk, steps)
    _system, sent = pk.gemini_wire.build_input(_history(response))
    assert sent == [
        {"type": "user_input", "content": [{"type": "text", "text": "turn on the light"}]},
        THOUGHT, _text_step("On it."), _call_step(),
        {"type": "function_result", "call_id": "c1", "name": "control_device",
         "result": "ok"},
    ]


def test_parallel_calls_keep_one_ordered_step_list(pk):
    steps = [THOUGHT, _call_step("a", "one"), {"type": "thought", "signature": "S2"},
             _call_step("b", "two")]
    response = _first_turn(pk, steps)
    assert [c.extra_content is not None for c in response.tool_calls] == [True, False]
    history = [
        {"role": "user", "content": "x"}, response.assistant_message(),
        {"role": "tool", "tool_call_id": "a", "content": "1"},
        {"role": "tool", "tool_call_id": "b", "content": "2"}]
    _system, sent = pk.gemini_wire.build_input(history)
    assert sent[1:5] == steps
    assert [s["name"] for s in sent[5:]] == ["one", "two"]


def test_history_from_another_provider_is_sent_as_plain_function_calls(pk):
    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "control_device", "arguments": '{"entity_id": "light.a"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    _system, sent = pk.gemini_wire.build_input(history)
    assert sent[1] == _call_step()
    assert sent[2]["name"] == "control_device" and sent[2]["call_id"] == "c1"


def test_recorded_steps_that_no_longer_match_the_calls_are_not_replayed(pk):
    response = _first_turn(pk, [THOUGHT, _call_step()])
    message = copy.deepcopy(response.assistant_message())
    message["tool_calls"][0]["id"] = "other"
    _system, sent = pk.gemini_wire.build_input([{"role": "user", "content": "x"}, message])
    assert THOUGHT not in sent and sent[1]["id"] == "other"


def test_flattened_history_has_no_function_steps(pk):
    response = _first_turn(pk, [THOUGHT, _call_step()])
    _system, sent = pk.gemini_wire.build_input(_history(response), flatten_tools=True)
    assert {s["type"] for s in sent} == {"user_input", "model_output"}
    assert "control_device" in json.dumps(sent) and SIG not in json.dumps(sent)


def test_other_providers_never_receive_the_recorded_steps(pk):
    response = _first_turn(pk, [THOUGHT, _call_step()])
    history = _history(response)
    before = copy.deepcopy(history)
    request = _request(pk, history)
    kwargs = pk.openai_wire.build_openai_kwargs(request, "m")
    assert "extra_content" not in kwargs["messages"][1]["tool_calls"][0]
    assert SIG not in json.dumps(kwargs)
    assert history == before          # the caller's history is untouched


# ── Response parsing ────────────────────────────────────────────────────────

def test_text_answer_and_usage_with_thought_tokens(pk):
    response = pk.gemini_wire.parse_interaction(_completed(
        [THOUGHT, _text_step("It is on.")],
        usage={"total_input_tokens": 7, "total_output_tokens": 20,
               "total_thought_tokens": 22, "total_tokens": 49}), model="m")
    assert response.text == "It is on."
    assert response.tool_calls == ()
    assert (response.usage.input_tokens, response.usage.output_tokens) == (7, 42)
    assert response.provider == "gemini"
    assert SIG not in json.dumps(response.raw) and "thought" not in json.dumps(response.raw)


def test_usage_is_none_when_not_reported(pk):
    response = pk.gemini_wire.parse_interaction(_completed([_text_step("x")]), model="m")
    assert (response.usage.input_tokens, response.usage.output_tokens) == (None, None)


def test_thought_summaries_are_never_visible_text(pk):
    thought = {"type": "thought", "signature": SIG,
               "summary": [{"type": "text", "text": "secret reasoning"}]}
    response = pk.gemini_wire.parse_interaction(
        _completed([thought, _text_step("Done.")]), model="m")
    assert response.text == "Done."


def test_leaked_think_envelope_is_removed(pk):
    response = pk.gemini_wire.parse_interaction(
        _completed([_text_step("<think>hmm</think>\nAnswer")]), model="m")
    assert response.text == "Answer"


def test_function_calls_carry_the_recorded_steps_on_the_first_call(pk):
    response = _first_turn(pk, [THOUGHT, _call_step("a", "one"), _call_step("b", "two")])
    first, second = response.tool_calls
    assert (first.id, first.name, dict(first.args)) == ("a", "one", {"entity_id": "light.a"})
    assert first.extra_content == {"google": {"steps": [
        THOUGHT, _call_step("a", "one"), _call_step("b", "two")]}}
    assert second.extra_content is None
    assert SIG not in repr(first)
    assert response.to_legacy()["tool_calls"][0] == {
        "id": "a", "name": "one", "args": {"entity_id": "light.a"}}


def test_string_arguments_are_parsed(pk):
    response = _first_turn(pk, [_call_step(args='{"x": 1}')])
    assert dict(response.tool_calls[0].args) == {"x": 1}


@pytest.mark.parametrize("arguments", ["not json", "[1]", 5])
def test_bad_arguments_are_a_malformed_response(pk, arguments):
    with pytest.raises(pk.errors.ProviderError) as exc:
        _first_turn(pk, [_call_step(args=arguments)])
    assert exc.value.kind is pk.errors.ProviderErrorKind.MALFORMED_RESPONSE


def test_oversized_recorded_steps_are_dropped_not_truncated(pk):
    big = {"type": "thought", "signature": "x" * (pk.gemini_wire.MAX_RECORDED_CHARS + 1)}
    response = _first_turn(pk, [big, _call_step()])
    assert response.tool_calls[0].extra_content is None


def test_an_incomplete_empty_reply_is_an_empty_response(pk):
    response = pk.gemini_wire.parse_interaction(
        {"status": "incomplete", "steps": [THOUGHT]}, model="m")
    assert response.text == "" and response.tool_calls == ()


@pytest.mark.parametrize("errors,kind,phrase", [
    ([{"code": "https://x/SAFETY_BLOCK", "message": "blocked"}], "invalid_request", "safety"),
    ([{"message": "malformed function call"}], "malformed_response", "tool call validation failed"),
    ([], "provider_unavailable", "interaction failed"),
])
def test_a_failed_interaction_maps_to_a_fixed_message(pk, errors, kind, phrase):
    with pytest.raises(pk.errors.ProviderError) as exc:
        pk.gemini_wire.parse_interaction(
            {"status": "failed", "steps": [], "errors": errors}, model="m")
    assert exc.value.kind.value == kind and phrase in str(exc.value)
    assert "SAFETY_BLOCK" not in str(exc.value)


@pytest.mark.parametrize("data,kind", [
    ({"status": "cancelled", "steps": []}, "cancelled"),
    ({"status": "in_progress", "steps": []}, "provider_unavailable"),
    ({"status": "completed"}, "malformed_response"),
    ([], "malformed_response"),
    (None, "malformed_response"),
])
def test_other_unusable_replies(pk, data, kind):
    with pytest.raises(pk.errors.ProviderError) as exc:
        pk.gemini_wire.parse_interaction(data, model="m")
    assert exc.value.kind.value == kind


# ── The adapter and its transport ───────────────────────────────────────────

class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        data = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return data if limit < 0 else data[:limit]


def _http_error(code, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return HTTPError("https://x", code, "err", {}, io.BytesIO(raw))


def _google_error(code, status, message):
    return _http_error(code, {"error": {"code": code, "status": status, "message": message}})


def _provider(lp, pk, monkeypatch, *replies, model="gemini-3.6-flash"):
    """A provider whose transport returns, or raises, each reply in turn."""
    seen: list = []
    queue = list(replies)

    def _urlopen(request, timeout):
        seen.append((request, timeout))
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, HTTPError):
            # A fresh error each time: a body can only be read once.
            raise HTTPError(reply.url, reply.code, reply.msg, reply.headers,
                            io.BytesIO(reply.fp.getvalue()))
        if isinstance(reply, BaseException):
            raise reply
        return _Response(reply)

    monkeypatch.setattr(pk.gemini, "_urlopen", _urlopen)
    return lp.GeminiProvider(SECRET, model), seen


def test_the_request_goes_to_google_with_the_key_in_a_header(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch, _completed([_text_step("pong")]))
    result = provider.chat([{"role": "user", "content": "ping"}], max_tokens=32)
    request, timeout = seen[0]
    assert request.full_url == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert request.get_method() == "POST"
    assert request.get_header("X-goog-api-key") == SECRET
    assert SECRET not in request.full_url and timeout == pk.gemini.GEMINI_TIMEOUT
    assert json.loads(request.data)["input"][0]["type"] == "user_input"
    assert result["text"] == "pong"


def test_a_saved_models_prefix_is_removed_and_a_per_call_model_wins(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch, _completed([_text_step("x")]),
                               model="models/gemini-3.6-flash")
    provider.chat([{"role": "user", "content": "a"}])
    provider.chat([{"role": "user", "content": "a"}], model_override="models/gemini-2.5-flash")
    assert [json.loads(r.data)["model"] for r, _ in seen] == [
        "gemini-3.6-flash", "gemini-2.5-flash"]


def test_a_tool_round_trip_replays_the_thought_signature(lp, pk, monkeypatch):
    first = _completed([THOUGHT, _call_step()])
    provider, seen = _provider(lp, pk, monkeypatch, first, _completed([_text_step("Done.")]))
    turn = provider.chat([{"role": "user", "content": "turn on the light"}], tools=[_tool()])
    assert turn["tool_calls"][0]["id"] == "c1"
    # What the agent loop keeps: the typed response's assistant message.
    typed = pk.gemini_wire.parse_interaction(first, model="m")
    history = [{"role": "user", "content": "turn on the light"}, typed.assistant_message(),
               {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    final = provider.chat(history, tools=[_tool()])
    assert final["text"] == "Done."
    sent = json.loads(seen[-1][0].data)["input"]
    assert THOUGHT in sent and sent.index(THOUGHT) < sent.index(_call_step())


def test_a_base_url_that_is_not_googles_is_ignored(lp, pk, monkeypatch):
    provider = lp.GeminiProvider(SECRET, "gemini-3.6-flash", "http://evil.example/v1")
    assert provider.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert lp.create_provider("gemini", SECRET, "gemini-3.6-flash").base_url == provider.base_url


def test_the_key_never_appears_in_repr_or_errors(lp, pk, monkeypatch):
    provider, _seen = _provider(lp, pk, monkeypatch, _google_error(
        400, "INVALID_ARGUMENT", f"API key not valid {SECRET}"))
    assert SECRET not in repr(provider)
    with pytest.raises(pk.errors.ProviderError) as exc:
        provider.chat([{"role": "user", "content": "x"}])
    assert SECRET not in str(exc.value) and SECRET not in repr(exc.value)


@pytest.mark.parametrize("error,kind", [
    (_google_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."),
     "authentication_failed"),
    (_google_error(403, "PERMISSION_DENIED", "denied"), "access_denied"),
    (_google_error(404, "NOT_FOUND", "models/nope is not found for API version v1beta"),
     "model_not_found"),
    (_google_error(429, "RESOURCE_EXHAUSTED", "quota"), "rate_limited"),
    (_google_error(500, "INTERNAL", "boom"), "provider_unavailable"),
    (_google_error(503, "UNAVAILABLE", "overloaded"), "provider_unavailable"),
    (_google_error(504, "DEADLINE_EXCEEDED", "slow"), "timeout"),
    (_google_error(400, "INVALID_ARGUMENT", "bad field"), "invalid_request"),
    (_http_error(301, b""), "invalid_endpoint"),
    (_http_error(400, b"not json"), "invalid_request"),
    (URLError(TimeoutError("timed out")), "timeout"),
    (URLError(ConnectionRefusedError()), "connection_failed"),
])
def test_http_failures_map_to_the_existing_error_kinds(lp, pk, monkeypatch, error, kind):
    provider, _seen = _provider(lp, pk, monkeypatch, error)
    with pytest.raises(pk.errors.ProviderError) as exc:
        provider.chat([{"role": "user", "content": "x"}])
    assert exc.value.kind.value == kind


def test_the_status_and_code_are_in_the_message_and_the_body_is_not(lp, pk, monkeypatch):
    provider, _seen = _provider(lp, pk, monkeypatch, _google_error(
        429, "RESOURCE_EXHAUSTED", "You exceeded your current quota, secret-detail"))
    with pytest.raises(pk.errors.ProviderError) as exc:
        provider.chat([{"role": "user", "content": "x"}])
    text = str(exc.value)
    assert "HTTP 429" in text and "RESOURCE_EXHAUSTED" in text and "rate limit" in text
    assert "secret-detail" not in text and exc.value.retryable


@pytest.mark.parametrize("message", [
    "The input token count (2000000) exceeds the maximum number of tokens allowed",
    "Request payload is too large"])
def test_an_oversized_prompt_reads_as_too_large_to_the_agent(lp, pk, monkeypatch, message):
    provider, _seen = _provider(lp, pk, monkeypatch, _google_error(400, "INVALID_ARGUMENT", message))
    with pytest.raises(pk.errors.ProviderError) as exc:
        provider.chat([{"role": "user", "content": "x"}])
    loop_text = pk.errors.error_text(exc.value).lower()
    assert "context length" in loop_text          # the marker the agent loop retries slim on


def test_a_safety_block_has_its_own_clear_message(lp, pk, monkeypatch):
    provider, _seen = _provider(lp, pk, monkeypatch, _google_error(
        400, "INVALID_ARGUMENT", "Request blocked: PROHIBITED_CONTENT"))
    with pytest.raises(pk.errors.ProviderError) as exc:
        provider.chat([{"role": "user", "content": "x"}])
    assert "blocked the request (safety)" in str(exc.value)
    assert "PROHIBITED_CONTENT" not in str(exc.value)


def test_a_model_that_rejects_a_thinking_level_is_retried_without_it(lp, pk, monkeypatch):
    provider, seen = _provider(
        lp, pk, monkeypatch,
        _google_error(400, "INVALID_ARGUMENT", "thinking_level is not supported by this model"),
        _completed([_text_step("ok")]))
    assert provider.chat([{"role": "user", "content": "x"}])["text"] == "ok"
    first, second = (json.loads(r.data)["generation_config"] for r, _ in seen)
    assert "thinking_level" in first and "thinking_level" not in second


def test_a_signature_refusal_retries_once_with_the_tool_turns_as_text(lp, pk, monkeypatch):
    provider, seen = _provider(
        lp, pk, monkeypatch,
        _google_error(400, "INVALID_ARGUMENT", "Function call is missing a thought signature"),
        _completed([_text_step("ok")]))
    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    assert provider.chat(history)["text"] == "ok"
    first, second = (json.loads(r.data)["input"] for r, _ in seen)
    assert any(s["type"] == "function_call" for s in first)
    assert not any(s["type"] in ("function_call", "function_result") for s in second)


def test_a_tool_result_is_sent_as_a_plain_string(pk):
    """A list of content parts is a multimodal function response, which
    Gemini 2.5 models refuse with HTTP 400."""
    response = _first_turn(pk, [THOUGHT, _call_step()])
    _system, sent = pk.gemini_wire.build_input(_history(response))
    result = [s for s in sent if s["type"] == "function_result"][0]
    assert result["result"] == "ok"


def test_a_multimodal_refusal_retries_once_with_the_tool_turns_as_text(lp, pk, monkeypatch):
    provider, seen = _provider(
        lp, pk, monkeypatch,
        _google_error(400, "invalid_request",
                      "Multimodal function responses are not supported for this model."),
        _completed([_text_step("ok")]))
    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    assert provider.chat(history)["text"] == "ok"
    assert len(seen) == 2
    first, second = (json.loads(r.data)["input"] for r, _ in seen)
    assert any(s["type"] == "function_result" for s in first)
    assert not any(s["type"] in ("function_call", "function_result") for s in second)


def test_a_rejected_key_with_tool_turns_is_not_retried(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch, _google_error(
        400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key."))
    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    with pytest.raises(pk.errors.ProviderError):
        provider.chat(history)
    assert len(seen) == 1


def test_no_retry_for_an_ordinary_bad_request(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch,
                               _google_error(400, "INVALID_ARGUMENT", "bad field"))
    with pytest.raises(pk.errors.ProviderError):
        provider.chat([{"role": "user", "content": "x"}])
    assert len(seen) == 1


def test_invalid_oversized_and_empty_bodies(lp, pk, monkeypatch):
    for payload, kind in ((b"not json", "malformed_response"),
                          (b"x" * (pk.gemini.GEMINI_MAX_RESPONSE_BYTES + 1), "malformed_response")):
        provider, _seen = _provider(lp, pk, monkeypatch, payload)
        with pytest.raises(pk.errors.ProviderError) as exc:
            provider.chat([{"role": "user", "content": "x"}])
        assert exc.value.kind.value == kind


def test_a_redirect_is_never_followed(pk):
    handler = pk.gemini._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_a_missing_key_fails_before_any_network_call(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch, _completed([_text_step("x")]))
    provider.api_key = ""
    with pytest.raises(pk.errors.ProviderError) as exc:
        provider.chat([{"role": "user", "content": "x"}])
    assert exc.value.kind.value == "missing_credential" and not seen


# ── Registration ────────────────────────────────────────────────────────────

def test_gemini_is_a_native_provider_on_googles_own_api(lp, pk):
    descriptor = pk.registry.DESCRIPTORS["gemini"]
    assert descriptor.adapter is pk.gemini.GeminiProvider is lp.PROVIDERS["gemini"]
    assert descriptor.default_base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert descriptor.default_model == "gemini-3.6-flash"
    assert descriptor.concurrency is pk.capabilities.GEMINI_POLICY
    assert descriptor.credential_field == "gemini_api_key" and descriptor.legacy_shared_key
    openai_compatible = sys.modules["jc.providers.openai_compatible"]
    assert not issubclass(pk.gemini.GeminiProvider, openai_compatible.OpenAICompatibleProvider)


@pytest.mark.parametrize("model,vision", [
    ("gemini-3.6-flash", True), ("gemini-4-argon", True), ("gemini-2.5-pro", True),
    ("gemma-4-31b-it", False)])
def test_vision_capability(lp, model, vision):
    provider = object.__new__(lp.GeminiProvider)
    provider.model = model
    assert provider.capabilities().vision is vision
    assert provider.capabilities("gemini-3.6-flash").vision is True


def test_a_remote_image_fails_before_any_network_call(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch, _completed([_text_step("x")]))
    with pytest.raises(pk.errors.ProviderError) as exc:
        provider.chat([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://cam.example/x.jpg"}}]}])
    assert exc.value.kind.value == "invalid_request" and not seen
    assert "inline data URLs" in str(exc.value)


def test_a_vision_call_sends_the_picture_and_reads_the_description(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch, _completed(
        [_text_step("A parcel on the step.")],
        usage={"total_input_tokens": 268, "total_output_tokens": 12}))
    result = provider.chat([
        {"role": "system", "content": "Describe the scene."},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
            {"type": "text", "text": "What is there?"}]}], max_tokens=300)
    body = json.loads(seen[0][0].data)
    assert body["input"][0]["content"][0]["type"] == "image"
    assert body["system_instruction"] == "Describe the scene."
    assert result["text"] == "A parcel on the step."
    assert result["usage"] == {"input_tokens": 268, "output_tokens": 12}


# ── An output limit used up by thinking ─────────────────────────────────────

def _incomplete(steps=(), **extra):
    return {"id": "v1_x", "status": "incomplete", "steps": list(steps), **extra}


def test_an_empty_incomplete_reply_is_retried_once_with_a_bigger_budget(lp, pk, monkeypatch):
    provider, seen = _provider(
        lp, pk, monkeypatch,
        _incomplete([THOUGHT], usage={"total_input_tokens": 10, "total_output_tokens": 0,
                                      "total_thought_tokens": 2000}),
        _completed([_text_step("pong")], usage={"total_input_tokens": 10, "total_output_tokens": 2,
                                                "total_thought_tokens": 50}))
    result = provider.chat([{"role": "user", "content": "ping"}], max_tokens=32)
    first, second = (json.loads(r.data)["generation_config"]["max_output_tokens"] for r, _ in seen)
    assert second > first and second >= 2048 + 0
    assert result["text"] == "pong" and len(seen) == 2
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 2052}   # both calls counted


def test_an_incomplete_reply_with_some_text_is_not_retried(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch, _incomplete([_text_step("Half an")]))
    assert provider.chat([{"role": "user", "content": "x"}])["text"] == "Half an"
    assert len(seen) == 1


def test_a_second_empty_incomplete_reply_is_returned_empty_without_a_third_call(lp, pk, monkeypatch):
    provider, seen = _provider(lp, pk, monkeypatch, _incomplete([THOUGHT]))
    result = provider.chat([{"role": "user", "content": "x"}])
    assert result["text"] == "" and result["tool_calls"] == [] and len(seen) == 2


# ── Temperature ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-2.0-flash", "gemma-4-31b-it"])
def test_temperature_is_sent_to_models_that_take_one(pk, model):
    request = _request(pk, [{"role": "user", "content": "x"}])
    config = pk.gemini_wire.build_request(request, model)["generation_config"]
    assert config["temperature"] == 0.7


@pytest.mark.parametrize("model", [
    "gemini-3-flash-preview", "gemini-3.6-flash", "gemini-3.8-flash", "gemini-4-argon",
    "gemini-flash-latest", "models/gemini-3.6-flash"])
def test_gemini_3_and_later_are_left_at_googles_default_temperature(pk, model):
    request = _request(pk, [{"role": "user", "content": "x"}])
    body = pk.gemini_wire.build_request(request, model)
    assert "temperature" not in body["generation_config"]


# ── Retries ─────────────────────────────────────────────────────────────────

def test_a_busy_google_is_retried_and_the_answer_is_used(lp, pk, monkeypatch, pauses):
    provider, seen = _provider(lp, pk, monkeypatch,
                               _google_error(503, "UNAVAILABLE", "overloaded"),
                               _google_error(429, "RESOURCE_EXHAUSTED", "quota"),
                               _completed([_text_step("pong")]))
    result = provider.chat([{"role": "user", "content": "ping"}], max_tokens=32)
    assert result["text"] == "pong"
    assert len(seen) == 3 and pauses == [1.0, 2.0]


def test_a_dropped_connection_is_retried(lp, pk, monkeypatch, pauses):
    provider, seen = _provider(lp, pk, monkeypatch,
                               URLError(ConnectionResetError()),
                               _completed([_text_step("ok")]))
    assert provider.chat([{"role": "user", "content": "x"}])["text"] == "ok"
    assert len(seen) == 2 and pauses == [1.0]


def test_there_are_two_retries_and_then_the_error_is_raised(lp, pk, monkeypatch, pauses):
    provider, seen = _provider(lp, pk, monkeypatch,
                               _google_error(503, "UNAVAILABLE", "overloaded"))
    with pytest.raises(pk.errors.ProviderError) as exc:
        provider.chat([{"role": "user", "content": "x"}])
    assert exc.value.kind.value == "provider_unavailable"
    assert len(seen) == 3


@pytest.mark.parametrize("error", [
    _google_error(400, "INVALID_ARGUMENT", "API key not valid."),
    _google_error(401, "UNAUTHENTICATED", "no"),
    _google_error(403, "PERMISSION_DENIED", "denied"),
    _google_error(404, "NOT_FOUND", "no such model"),
    _google_error(504, "DEADLINE_EXCEEDED", "slow"),
    URLError(TimeoutError("timed out")),
])
def test_other_failures_are_not_retried(lp, pk, monkeypatch, pauses, error):
    provider, seen = _provider(lp, pk, monkeypatch, error)
    with pytest.raises(pk.errors.ProviderError):
        provider.chat([{"role": "user", "content": "x"}])
    assert len(seen) == 1 and pauses == []


def test_retry_after_is_used_but_never_waited_past_the_cap(lp, pk, monkeypatch, pauses):
    def slow(seconds):
        error = _google_error(429, "RESOURCE_EXHAUSTED", "quota")
        error.headers = {"Retry-After": seconds}
        return error
    provider, _seen = _provider(lp, pk, monkeypatch, slow("3"), slow("120"),
                                _completed([_text_step("ok")]))
    provider.chat([{"role": "user", "content": "x"}])
    assert pauses == [3.0, pk.gemini.RETRY_AFTER_CAP]


def test_no_retry_starts_past_the_total_time_budget(lp, pk, monkeypatch, pauses):
    clock = iter([0.0, pk.gemini.RETRY_TOTAL_BUDGET + 1])
    monkeypatch.setattr(pk.gemini, "time", types.SimpleNamespace(
        monotonic=lambda: next(clock, 999.0), sleep=lambda s: None))
    provider, seen = _provider(lp, pk, monkeypatch,
                               _google_error(503, "UNAVAILABLE", "overloaded"))
    with pytest.raises(pk.errors.ProviderError):
        provider.chat([{"role": "user", "content": "x"}])
    assert len(seen) == 1 and pauses == []
