"""The Anthropic adapter: the request it builds for the Messages API and how
it reads the reply.

The SDK client is replaced by a recorder, so these pin the exact body that
would be sent: system text, message blocks, tool format, model and token
settings, the one retry without temperature, and what comes back.
"""
import sys
import types

import pytest


@pytest.fixture
def mod(load):
    return load("providers.anthropic")


@pytest.fixture
def errors(load):
    return load("providers.errors")


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for key, value in kw.items():
            setattr(self, key, value)


def _reply(blocks=(), usage=(7, 8)):
    ns = types.SimpleNamespace(content=list(blocks))
    if usage is not None:
        ns.usage = types.SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1])
    return ns


@pytest.fixture
def make(mod):
    """provider, sent = make(reply_or_error, ...): a provider whose client
    returns (or raises) each item in turn and records every call."""
    def _make(*replies, model="claude-base"):
        provider = object.__new__(mod.AnthropicProvider)
        provider.model, provider.api_key, provider.base_url = model, "sk-ant", None
        queue, sent = list(replies), []

        def create(**kwargs):
            sent.append(dict(kwargs))
            item = queue.pop(0) if queue else _reply([_Block("text", text="ok")])
            if isinstance(item, Exception):
                raise item
            return item

        provider._client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
        return provider, sent
    return _make


def _body(make, messages, **options):
    provider, sent = make()
    provider.chat(messages, **options)
    return sent[0]


# ── construction ─────────────────────────────────────────────────────────────

def test_the_adapter_declares_what_it_is(mod, load):
    cls = mod.AnthropicProvider
    assert cls.name == "anthropic" and cls.requires_credential is True
    assert cls.concurrency is load("providers.capabilities").SDK_CLIENT_POLICY


@pytest.fixture
def sdk(monkeypatch):
    made = []

    class Anthropic:
        def __init__(self, **kwargs):
            made.append(kwargs)

    module = types.ModuleType("anthropic")
    module.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return made


def test_the_client_is_built_from_the_key_alone(mod, sdk):
    provider = mod.AnthropicProvider("sk-1", "claude-x")
    assert sdk == [{"api_key": "sk-1"}]
    assert (provider.api_key, provider.model, provider.base_url) == ("sk-1", "claude-x", None)


def test_a_base_url_is_passed_on_when_given(mod, sdk):
    mod.AnthropicProvider("sk-1", "claude-x", base_url="http://proxy.test")
    mod.AnthropicProvider("sk-2", "claude-x", base_url="")
    assert sdk == [{"api_key": "sk-1", "base_url": "http://proxy.test"},
                   {"api_key": "sk-2"}]


def test_a_missing_sdk_is_a_configuration_error(mod, errors, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(errors.ProviderConfigurationError) as caught:
        mod.AnthropicProvider("sk-1", "claude-x")
    assert caught.value.kind is errors.ProviderErrorKind.PROVIDER_UNAVAILABLE
    assert "the anthropic package is not installed" in caught.value.diagnostic_text()
    assert isinstance(caught.value.__cause__, ImportError)


# ── system text ──────────────────────────────────────────────────────────────

def test_system_messages_are_lifted_out_and_joined(make):
    body = _body(make, [
        {"role": "system", "content": "You are Nova."},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": [
            {"type": "text", "text": "Be brief."}, {"type": "image_url"},
            {"type": "text", "text": "Be kind."}, "stray", {"type": "text"}]},
    ])
    assert body["system"] == "You are Nova.\n\nBe brief. Be kind. "
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_an_empty_first_system_message_adds_no_separator(make):
    body = _body(make, [{"role": "system", "content": ""},
                        {"role": "system", "content": "Second."},
                        {"role": "user", "content": "x"}])
    assert body["system"] == "Second."


def test_a_blank_system_message_after_text_still_adds_the_separator(make):
    body = _body(make, [{"role": "system", "content": "First."},
                        {"role": "system", "content": ""},
                        {"role": "user", "content": "x"}])
    assert body["system"] == "First.\n\n"


def test_with_no_system_message_the_system_field_is_empty_text(make):
    assert _body(make, [{"role": "user", "content": "x"}])["system"] == ""


# ── conversation blocks ──────────────────────────────────────────────────────

def test_plain_turns_pass_through_and_roles_are_kept(make):
    body = _body(make, [{"role": "user", "content": "a"},
                        {"role": "assistant", "content": "b"},
                        {"role": "user", "content": "c"}])
    assert body["messages"] == [{"role": "user", "content": "a"},
                                {"role": "assistant", "content": "b"},
                                {"role": "user", "content": "c"}]


def test_content_that_is_neither_text_nor_a_list_is_left_alone(make):
    body = _body(make, [{"role": "user", "content": None}])
    assert body["messages"] == [{"role": "user", "content": None}]


def test_an_assistant_turn_with_calls_becomes_text_and_tool_use_blocks(make):
    body = _body(make, [
        {"role": "assistant", "content": "On it.", "tool_calls": [
            {"id": "a", "function": {"name": "f", "arguments": '{"x": 1}'}},
            {"id": "b", "function": {"name": "g", "arguments": {"y": 2}}},
            {"id": "c", "function": {"name": "h", "arguments": ""}},
            {"id": "d", "function": {"name": "i", "arguments": "{broken"}},
            {"id": "e", "function": {"name": "j", "arguments": None}},
            {"id": "f", "function": {"name": "k"}},
            {"function": None},
            {},
        ]}])
    assert body["messages"] == [{"role": "assistant", "content": [
        {"type": "text", "text": "On it."},
        {"type": "tool_use", "id": "a", "name": "f", "input": {"x": 1}},
        {"type": "tool_use", "id": "b", "name": "g", "input": {"y": 2}},
        {"type": "tool_use", "id": "c", "name": "h", "input": {}},
        {"type": "tool_use", "id": "d", "name": "i", "input": {}},
        {"type": "tool_use", "id": "e", "name": "j", "input": {}},
        {"type": "tool_use", "id": "f", "name": "k", "input": {}},
        {"type": "tool_use", "id": "", "name": "", "input": {}},
        {"type": "tool_use", "id": "", "name": "", "input": {}},
    ]}]


@pytest.mark.parametrize("text", ["", None])
def test_an_assistant_turn_with_no_text_has_no_text_block(make, text):
    body = _body(make, [{"role": "assistant", "content": text, "tool_calls": [
        {"id": "a", "function": {"name": "f", "arguments": "{}"}}]}])
    assert body["messages"][0]["content"] == [
        {"type": "tool_use", "id": "a", "name": "f", "input": {}}]


def test_tool_results_in_a_row_share_one_user_turn(make):
    body = _body(make, [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "a", "content": "one"},
        {"role": "tool", "tool_call_id": "b", "content": "two"},
        {"role": "tool", "tool_call_id": "c", "content": "three"},
    ])
    assert body["messages"] == [
        {"role": "user", "content": "go"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "one"},
            {"type": "tool_result", "tool_use_id": "b", "content": "two"},
            {"type": "tool_result", "tool_use_id": "c", "content": "three"}]}]


def test_a_tool_result_does_not_join_a_user_turn_that_has_other_content(make):
    body = _body(make, [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "tool", "tool_call_id": "a", "content": "r"},
    ])
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "r"}]}]


def test_a_tool_result_after_an_assistant_turn_starts_its_own_user_turn(make):
    body = _body(make, [
        {"role": "assistant", "content": "x"},
        {"role": "tool", "tool_call_id": "a", "content": "r"}])
    assert [m["role"] for m in body["messages"]] == ["assistant", "user"]


def test_a_tool_result_with_nothing_in_it_still_goes_through(make):
    body = _body(make, [{"role": "tool"}, {"role": "tool", "content": None}])
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "", "content": ""},
        {"type": "tool_result", "tool_use_id": "", "content": ""}]}]


def test_a_tool_result_first_in_the_history_is_fine(make):
    body = _body(make, [{"role": "tool", "tool_call_id": "z", "content": "r"}])
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "z", "content": "r"}]}]


# ── images and other parts ───────────────────────────────────────────────────

def _parts(make, *parts):
    return _body(make, [{"role": "user", "content": list(parts)}])["messages"][0]["content"]


def test_an_inline_image_becomes_a_base64_source(make):
    assert _parts(make, {"type": "image_url",
                         "image_url": {"url": "data:image/png;base64,QUJD"}}) == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "QUJD"}}]


@pytest.mark.parametrize("url, media, data", [
    ("data:image/webp,AAAA", "image/webp", "AAAA"),
    ("data:;base64,AAAA", "image/jpeg", "AAAA"),
    ("data:image/gif;charset=x;base64,AAAA", "image/gif", "AAAA"),
    ("data:image/png;base64,AA,AA", "image/png", "AA,AA"),
    ("data:image/png;base64", "image/jpeg", ""),
    ("data:nonsense,AAAA", "nonsense", "AAAA"),
])
def test_an_inline_image_url_is_split_into_type_and_data(make, url, media, data):
    out = _parts(make, {"type": "image_url", "image_url": {"url": url}})
    assert out == [{"type": "image", "source": {
        "type": "base64", "media_type": media, "data": data}}]


def test_a_remote_image_becomes_a_url_source(make):
    assert _parts(make, {"type": "image_url",
                         "image_url": {"url": "https://example.test/a.jpg"}}) == [
        {"type": "image", "source": {"type": "url", "url": "https://example.test/a.jpg"}}]


@pytest.mark.parametrize("image_url", [{"url": ""}, {}, None, {"url": None}])
def test_an_image_with_no_address_is_dropped(make, image_url):
    part = {"type": "image_url", "image_url": image_url}
    assert _parts(make, part, {"type": "text", "text": "x"}) == [
        {"type": "text", "text": "x"}]


def test_text_parts_are_kept_and_other_parts_pass_through_as_they_are(make):
    other = {"type": "input_audio", "data": "zz"}
    assert _parts(make, {"type": "text", "text": "hello"}, {"type": "text"}, other,
                  "stray string", 5, None) == [
        {"type": "text", "text": "hello"}, {"type": "text", "text": ""}, other]


def test_an_assistant_turn_without_calls_has_its_parts_converted_too(make):
    body = _body(make, [{"role": "assistant", "content": [
        {"type": "text", "text": "see"},
        {"type": "image_url", "image_url": {"url": "https://example.test/b.png"}}]}])
    assert body["messages"][0]["content"] == [
        {"type": "text", "text": "see"},
        {"type": "image", "source": {"type": "url", "url": "https://example.test/b.png"}}]


# ── the request body ─────────────────────────────────────────────────────────

TOOLS = [{"type": "function", "function": {
    "name": "call_service", "description": "Call a service",
    "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}}}}}]


def test_the_request_body_has_exactly_these_fields_in_this_order(make):
    body = _body(make, [{"role": "user", "content": "hi"}], tools=TOOLS,
                 max_tokens=99, temperature=0.25)
    assert list(body) == ["model", "system", "messages", "max_tokens",
                          "temperature", "tools"]
    assert body["model"] == "claude-base"
    assert body["max_tokens"] == 99 and body["temperature"] == 0.25
    assert body["tools"] == [{
        "name": "call_service", "description": "Call a service",
        "input_schema": TOOLS[0]["function"]["parameters"]}]


def test_without_tools_the_body_has_no_tools_field(make):
    body = _body(make, [{"role": "user", "content": "hi"}])
    assert list(body) == ["model", "system", "messages", "max_tokens", "temperature"]
    assert body["max_tokens"] == 512 and body["temperature"] == 0.7


def test_a_model_override_is_used_for_that_call_only(make):
    provider, sent = make()
    provider.chat([{"role": "user", "content": "hi"}], model_override="claude-vision")
    provider.chat([{"role": "user", "content": "hi"}])
    assert [s["model"] for s in sent] == ["claude-vision", "claude-base"]


# ── the reply ────────────────────────────────────────────────────────────────

def test_text_and_tool_use_blocks_are_read_and_nothing_else(make):
    provider, _ = make(_reply([
        _Block("thinking", thinking="private"),
        _Block("text", text="Let me "), _Block("redacted_thinking", data="x"),
        _Block("text", text="check."),
        _Block("tool_use", id="t1", name="get_state", input={"entity_id": "lock.front"}),
        _Block("tool_use", id=2, name=3, input="not a dict"),
        types.SimpleNamespace(),
    ]))
    result = provider.chat([{"role": "user", "content": "q"}])
    assert result["text"] == "Let me check."
    assert result["tool_calls"] == [
        {"id": "t1", "name": "get_state", "args": {"entity_id": "lock.front"}},
        {"id": "2", "name": "3", "args": {}}]
    assert result["usage"] == {"input_tokens": 7, "output_tokens": 8}


def test_leaked_reasoning_is_not_part_of_the_text(make):
    provider, _ = make(_reply([_Block("text", text="<think>hidden</think>Shown.")]))
    assert provider.chat([{"role": "user", "content": "q"}])["text"] == "Shown."


def test_a_reply_with_no_usage_reports_none(make):
    provider, _ = make(_reply([_Block("text", text="x")], usage=None))
    assert provider.chat([{"role": "user", "content": "q"}])["usage"] == {
        "input_tokens": None, "output_tokens": None}


def test_partial_usage_is_kept(make):
    reply = _reply([_Block("text", text="x")])
    reply.usage = types.SimpleNamespace(input_tokens=5)
    provider, _ = make(reply)
    assert provider.chat([{"role": "user", "content": "q"}])["usage"] == {
        "input_tokens": 5, "output_tokens": None}


def test_the_raw_value_is_rebuilt_from_the_visible_reply(make):
    provider, _ = make(_reply([
        _Block("thinking", thinking="private"), _Block("text", text="Done."),
        _Block("tool_use", id="t1", name="f", input={"a": 1})]))
    assert provider.chat([{"role": "user", "content": "q"}])["raw"] == {
        "role": "assistant", "content": [
            {"type": "text", "text": "Done."},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}]}


def test_anthropic_raw_leaves_out_empty_text(mod, load):
    call = load("providers.models").ToolCall(id="t", name="f", args={"a": 1})
    assert mod.anthropic_raw("", (call,)) == {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t", "name": "f", "input": {"a": 1}}]}
    assert mod.anthropic_raw("", ()) == {"role": "assistant", "content": []}


def test_the_response_names_the_provider_and_the_model_used(make, load):
    provider, _ = make(_reply([_Block("text", text="x")]))
    request = load("providers.models").ChatRequest.from_legacy(
        [{"role": "user", "content": "q"}], model_override="claude-vision")
    response = provider.complete(request)
    assert (response.provider, response.model) == ("anthropic", "claude-vision")


# ── the retry without temperature ────────────────────────────────────────────

def test_a_temperature_deprecation_is_retried_once_without_it(make):
    provider, sent = make(
        RuntimeError("`temperature` is DEPRECATED for this model"),
        _reply([_Block("text", text="ok")]))
    result = provider.chat([{"role": "user", "content": "hi"}], temperature=0.3,
                           tools=TOOLS)
    assert result["text"] == "ok"
    assert "temperature" in sent[0] and "temperature" not in sent[1]
    assert {k: v for k, v in sent[0].items() if k != "temperature"} == sent[1]


@pytest.mark.parametrize("message", [
    "temperature is out of range", "this model is deprecated", "rate limited"])
def test_any_other_error_is_not_retried(make, message):
    boom = RuntimeError(message)
    provider, sent = make(boom, _reply([_Block("text", text="never")]))
    with pytest.raises(Exception) as caught:
        provider.chat([{"role": "user", "content": "hi"}])
    assert len(sent) == 1
    assert caught.value is boom or caught.value.__cause__ is boom


def test_a_second_failure_after_the_retry_is_raised(make):
    second = RuntimeError("still broken")
    provider, sent = make(RuntimeError("temperature deprecated"), second)
    with pytest.raises(Exception) as caught:
        provider.chat([{"role": "user", "content": "hi"}])
    assert len(sent) == 2
    assert caught.value is second or caught.value.__cause__ is second


# ── capabilities ─────────────────────────────────────────────────────────────

def test_every_claude_model_is_treated_as_seeing_images(make):
    provider, _ = make()
    assert provider.capabilities().vision is True
    assert provider.capabilities("claude-anything").vision is True
