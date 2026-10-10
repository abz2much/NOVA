"""The Chat tab's command in ws_chat.py (8.31.0).

nova/chat forwards one typed message to Nova's conversation agent. The
decorators are stubbed to pass through, so the handler body runs with a
recording connection and a fake conversation seam. What matters: the text and
conversation id are the only things forwarded (never a device_id), a failure
answers with safe text, and typed chat is not treated as a voice request by
the authorisation gate.
"""
from __future__ import annotations

import sys
import types

import pytest

from fakes import FakeHass
from test_pin_ws_update_config import _Conn, _stub_ws_api


class _CtxConn(_Conn):
    def context(self, msg):
        return ("ctx", msg["id"])


@pytest.fixture
def chat(load, monkeypatch):
    _stub_ws_api(monkeypatch)
    for name in ("jc.ws_chat", "jc.websocket"):
        sys.modules.pop(name, None)
    load("websocket")
    mod = load("ws_chat")
    yield mod
    for name in ("jc.ws_chat", "jc.websocket"):
        sys.modules.pop(name, None)


async def _call(handler, **msg):
    conn = _CtxConn()
    await handler(FakeHass(), conn, {"id": 4, **msg})
    return conn


def _agent(load, monkeypatch, agent_id):
    """ws_chat does `from . import bootstrap`, so patch the module it gets."""
    monkeypatch.setattr(load("bootstrap"), "_find_nova_agent", lambda hass: agent_id)


def _install_conversation(monkeypatch, load, response, seen):
    """A fake Home Assistant conversation.async_converse and a found agent."""
    async def async_converse(hass, text, conversation_id, context, agent_id=None, **kw):
        seen.append({"text": text, "conversation_id": conversation_id,
                     "context": context, "agent_id": agent_id, "extra": kw})
        if isinstance(response, Exception):
            raise response
        return types.SimpleNamespace(
            conversation_id="conv-1",
            response=types.SimpleNamespace(as_dict=lambda: response))
    conv = types.ModuleType("homeassistant.components.conversation")
    conv.async_converse = async_converse
    monkeypatch.setitem(sys.modules, "homeassistant.components.conversation", conv)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "conversation", conv, raising=False)
    _agent(load, monkeypatch, "conversation.nova")


async def test_chat_returns_the_reply_and_conversation_id(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load,
                          {"response_type": "action_done", "speech": {"plain": {"speech": "Done."}}}, seen)
    conn = await _call(chat.ws_chat, text=" turn on the lamp ", conversation_id=None)
    assert conn.results == [(4, {"ok": True, "reply": "Done.", "conversation_id": "conv-1"})]
    # Only the text, the id and the caller's context go through; no device_id.
    assert seen == [{"text": "turn on the lamp", "conversation_id": None,
                     "context": ("ctx", 4), "agent_id": "conversation.nova", "extra": {}}]


async def test_chat_keeps_the_conversation_id_the_panel_sends(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load,
                          {"response_type": "query_answer", "speech": {"plain": {"speech": "Yes."}}}, seen)
    await _call(chat.ws_chat, text="is it dark?", conversation_id="conv-1")
    assert seen[0]["conversation_id"] == "conv-1"


async def test_chat_blank_text_is_refused_without_calling_the_agent(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, {}, seen)
    conn = await _call(chat.ws_chat, text="   ")
    assert conn.results == [(4, {"ok": False, "error": "Type a message first."})] and seen == []


@pytest.mark.parametrize("response,error", [
    ({"response_type": "error", "speech": {"plain": {"speech": "Sorry."}}}, "Sorry."),
    ({"response_type": "error", "speech": {}}, "Nova returned an error."),
    ({"response_type": "action_done", "speech": {}}, "Nova replied with no text."),
])
async def test_chat_reports_an_error_or_empty_reply(chat, load, monkeypatch, response, error):
    _install_conversation(monkeypatch, load, response, [])
    conn = await _call(chat.ws_chat, text="hello")
    assert conn.results == [(4, {"ok": False, "error": error})]


async def test_chat_without_an_agent_says_so(chat, load, monkeypatch):
    _install_conversation(monkeypatch, load, {}, [])
    _agent(load, monkeypatch, None)
    conn = await _call(chat.ws_chat, text="hello")
    assert conn.results == [(4, {"ok": False, "error": "Nova's conversation agent was not found."})]


async def test_chat_failure_is_a_safe_error_result_not_a_raise(chat, load, monkeypatch):
    _install_conversation(monkeypatch, load, RuntimeError("token=abc"), [])
    conn = await _call(chat.ws_chat, text="hello")
    assert conn.errors == []
    assert conn.results == [(4, {"ok": False,
                                 "error": "RuntimeError (details are in the Home Assistant log)"})]


def test_the_message_length_limit_is_declared(chat):
    assert chat.CHAT_MAX_CHARS == 1000


# ── the authorisation gate treats typed chat like any non voice request ─────

def test_typed_chat_has_no_device_so_it_is_not_a_voice_request(load):
    pol = load("policy")
    # The command passes no device_id, so the gate's own voice check is False
    # and the source defaults to chat rather than voice.
    assert pol._voice_satellite_request(FakeHass(), "") is False
    assert pol.AuthorityRequest("lock", "unlock", "lock.front").source == ""
    assert pol.SOURCE_CHAT in pol.SOURCES


def test_chat_cannot_unlock_without_the_gate_deciding(load, monkeypatch):
    pol = load("policy")
    vc = load("voice_confirm")
    monkeypatch.setattr(vc, "action_is_protected", lambda hass, d, s, e: True)
    dec = pol.authorize_now(FakeHass(), pol.AuthorityRequest("lock", "unlock", "lock.front"))
    assert (dec.allowed, dec.approval, dec.source) == (False, "deferred", pol.SOURCE_CHAT)
