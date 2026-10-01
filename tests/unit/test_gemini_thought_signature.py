"""Gemini 3 thought signatures survive a tool call round trip.

Gemini 3 attaches a signature to each function call (``extra_content`` on the
OpenAI compatible wire) and rejects the follow up request when it is missing.
These tests use fake SDK replies only; nothing contacts Google.
"""
import copy
import sys
import types

import pytest

SIG = "Cj0Bsig/with+base64=="


@pytest.fixture
def lp(load):
    return load("llm_provider")


@pytest.fixture
def pk(lp):
    names = ("models", "openai_wire", "openai_compatible")
    return types.SimpleNamespace(**{n: sys.modules[f"jc.providers.{n}"] for n in names})


def _tc(extra=None, model_extra=None, call_id="c1", name="control_device",
        args='{"entity_id": "light.a"}'):
    tc = types.SimpleNamespace(id=call_id, function=types.SimpleNamespace(
        name=name, arguments=args))
    if extra is not None:
        tc.extra_content = extra
    if model_extra is not None:
        tc.model_extra = model_extra
    return tc


def _completion(*tool_calls, content=""):
    message = types.SimpleNamespace(content=content, tool_calls=list(tool_calls))
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)],
                                 usage=None)


def _provider(lp, cls_name, create):
    provider = object.__new__(getattr(lp, cls_name))
    provider.model, provider.api_key, provider.base_url = "m", "k", None
    provider._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    return provider


GOOGLE = {"google": {"thought_signature": SIG}}


# ── capture ─────────────────────────────────────────────────────────────────

def test_signature_is_captured_from_the_reply(pk):
    resp = pk.openai_wire.parse_openai_completion(
        _completion(_tc(extra=GOOGLE)), provider="gemini", model="m")
    call = resp.tool_calls[0]
    assert call.extra_content == GOOGLE
    assert resp.assistant_message()["tool_calls"][0]["extra_content"] == GOOGLE
    assert resp.raw["tool_calls"][0]["extra_content"] == GOOGLE


def test_signature_is_read_from_model_extra_too(pk):
    resp = pk.openai_wire.parse_openai_completion(
        _completion(_tc(model_extra={"extra_content": GOOGLE})),
        provider="gemini", model="m")
    assert resp.tool_calls[0].extra_content == GOOGLE


def test_pydantic_style_objects_are_read(pk):
    class _Dump:
        def __init__(self, data):
            self._d = data

        def model_dump(self):
            return self._d

    resp = pk.openai_wire.parse_openai_completion(
        _completion(_tc(extra=_Dump({"google": _Dump({"thought_signature": SIG})}))),
        provider="gemini", model="m")
    assert resp.tool_calls[0].extra_content == GOOGLE


@pytest.mark.parametrize("extra", [
    None, {}, "text", {"google": {}}, {"google": {"thought_signature": ""}},
    {"google": {"thought_signature": 5}}, {"other": {"thought_signature": SIG}},
    {"google": {"thought_signature": "x" * 70000}},
])
def test_anything_but_a_real_signature_is_dropped(pk, extra):
    resp = pk.openai_wire.parse_openai_completion(
        _completion(_tc(extra=extra)), provider="gemini", model="m")
    assert resp.tool_calls[0].extra_content is None
    assert "extra_content" not in resp.assistant_message()["tool_calls"][0]


def test_only_the_signature_is_kept_not_other_fields(pk):
    extra = {"google": {"thought_signature": SIG, "debug": "private"}, "x": 1}
    resp = pk.openai_wire.parse_openai_completion(
        _completion(_tc(extra=extra)), provider="gemini", model="m")
    assert resp.tool_calls[0].extra_content == GOOGLE


def test_replies_without_a_signature_are_unchanged(pk):
    resp = pk.openai_wire.parse_openai_completion(
        _completion(_tc()), provider="openai", model="m")
    assert resp.assistant_message() == {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "control_device", "arguments": '{"entity_id": "light.a"}'}}]}
    assert resp.to_legacy()["tool_calls"] == [
        {"id": "c1", "name": "control_device", "args": {"entity_id": "light.a"}}]


def test_the_signature_is_hidden_from_repr(pk):
    call = pk.models.ToolCall(id="c", name="t", args={}, extra_content=GOOGLE)
    assert SIG not in repr(call)


# ── replay ──────────────────────────────────────────────────────────────────

def _history(resp):
    return [
        {"role": "user", "content": "turn on the light"},
        resp.assistant_message(),
        {"role": "tool", "tool_call_id": "c1", "name": "control_device", "content": "ok"},
    ]


def test_gemini_sends_the_signature_back(lp, pk):
    seen = []
    provider = _provider(lp, "GeminiProvider",
                         lambda **kw: seen.append(kw) or _completion(_tc(extra=GOOGLE)))
    first = provider.chat([{"role": "user", "content": "turn on the light"}])
    assert first["tool_calls"][0]["id"] == "c1"
    resp = pk.openai_wire.parse_openai_completion(
        _completion(_tc(extra=GOOGLE)), provider="gemini", model="m")
    provider.chat(_history(resp))
    sent = seen[-1]["messages"][1]["tool_calls"][0]
    assert sent["extra_content"] == GOOGLE


@pytest.mark.parametrize("cls_name", ["OpenAIProvider", "GroqProvider", "CustomProvider"])
def test_other_providers_never_receive_it(lp, pk, cls_name):
    seen = []
    provider = _provider(lp, cls_name, lambda **kw: seen.append(kw) or _completion())
    if cls_name == "CustomProvider":
        provider.base_url = "http://192.168.1.20:8000/v1"
    resp = pk.openai_wire.parse_openai_completion(
        _completion(_tc(extra=GOOGLE)), provider="gemini", model="m")
    history = _history(resp)
    before = copy.deepcopy(history)
    provider.chat(history)
    sent_call = seen[-1]["messages"][1]["tool_calls"][0]
    assert "extra_content" not in sent_call
    assert sent_call["id"] == "c1" and sent_call["function"]["name"] == "control_device"
    assert history == before                      # the caller's history is untouched


def test_extra_content_is_removed_only_from_tool_calls(pk):
    msg = {"role": "assistant", "content": "hi", "tool_calls": [
        {"id": "a", "type": "function", "extra_content": GOOGLE,
         "function": {"name": "t", "arguments": "{}"}},
        {"id": "b", "type": "function", "function": {"name": "u", "arguments": "{}"}}]}
    cleaned = pk.openai_wire._without_extra_content(msg)
    assert "extra_content" not in cleaned["tool_calls"][0]
    assert cleaned["tool_calls"][1] == msg["tool_calls"][1]
    assert "extra_content" in msg["tool_calls"][0]          # original kept
    plain = {"role": "user", "content": "x"}
    assert pk.openai_wire._without_extra_content(plain) is plain


def test_a_history_without_signatures_is_sent_exactly_as_before(lp):
    seen = []
    provider = _provider(lp, "GeminiProvider", lambda **kw: seen.append(kw) or _completion())
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "c", "type": "function",
                    "function": {"name": "t", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c", "content": "ok"}]
    provider.chat(history)
    assert seen[-1]["messages"] == history
