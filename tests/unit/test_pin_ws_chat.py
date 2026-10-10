"""The Chat tab's commands in ws_chat.py (8.31.0): nova/chat and nova/chat_new.

The decorators are stubbed to pass through, so the handler bodies run with a
recording connection and a fake conversation seam. What matters: any signed in
user can send (there is no admin gate), the conversation id is built from the
user and never taken from the message, two users get separate threads, NEW CHAT
clears only the caller's own thread, the length, rate and one in flight limits
hold with no agent call, and nothing raises.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from fakes import FakeHass
from test_pin_ws_update_config import _Conn, _stub_ws_api


class _UserConn(_Conn):
    """A connection for one Home Assistant user."""

    def __init__(self, user_id="u1"):
        super().__init__()
        self.user_id = user_id

    def context(self, msg):
        return types.SimpleNamespace(user_id=self.user_id, msg_id=msg["id"])


@pytest.fixture
def chat(load, monkeypatch):
    _stub_ws_api(monkeypatch)
    for name in ("jc.ws_chat", "jc.websocket"):
        sys.modules.pop(name, None)
    load("websocket")
    mod = load("ws_chat")
    mod._SENT.clear()
    mod._IN_FLIGHT.clear()
    yield mod
    mod._SENT.clear()
    mod._IN_FLIGHT.clear()
    for name in ("jc.ws_chat", "jc.websocket"):
        sys.modules.pop(name, None)


async def _call(handler, user_id="u1", hass=None, **msg):
    conn = _UserConn(user_id)
    await handler(hass or FakeHass(), conn, {"id": 4, **msg})
    return conn


def _agent(load, monkeypatch, agent_id):
    """ws_chat does `from . import bootstrap`, so patch the module it gets."""
    monkeypatch.setattr(load("bootstrap"), "_find_nova_agent", lambda hass: agent_id)


def _install_conversation(monkeypatch, load, response, seen, gate=None):
    """A fake Home Assistant conversation.async_converse and a found agent."""
    async def async_converse(hass, text, conversation_id, context, agent_id=None, **kw):
        seen.append({"text": text, "conversation_id": conversation_id,
                     "context": context, "agent_id": agent_id, "extra": kw})
        if gate is not None:
            await gate.wait()
        if isinstance(response, Exception):
            raise response
        return types.SimpleNamespace(
            response=types.SimpleNamespace(as_dict=lambda: response))
    conv = types.ModuleType("homeassistant.components.conversation")
    conv.async_converse = async_converse
    monkeypatch.setitem(sys.modules, "homeassistant.components.conversation", conv)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "conversation", conv, raising=False)
    _agent(load, monkeypatch, "conversation.nova")


DONE = {"response_type": "action_done", "speech": {"plain": {"speech": "Done."}}}


# ── who may use it, and which thread ────────────────────────────────────────

def test_chat_is_not_admin_gated(chat):
    import ast
    import pathlib
    src = pathlib.Path(chat.__file__).read_text()
    assert "require_admin" not in src.replace("no admin gate", "")
    names = {n.name for n in ast.parse(src).body if isinstance(n, ast.AsyncFunctionDef)}
    assert {"ws_chat", "ws_chat_new"} <= names


async def test_a_non_admin_user_can_send(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, DONE, seen)
    conn = await _call(chat.ws_chat, user_id="plain-user", text="turn on the lamp")
    assert conn.results == [(4, {"ok": True, "reply": "Done."})] and conn.errors == []
    assert seen[0]["text"] == "turn on the lamp"


async def test_the_conversation_id_comes_from_the_user_not_the_message(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, DONE, seen)
    # The schema does not allow the field; if one reached the handler it must be ignored.
    await _call(chat.ws_chat, user_id="u1", text="hi", conversation_id="nova_chat_someone_else")
    assert seen[0]["conversation_id"] == "nova_chat_u1"


async def test_two_users_get_separate_threads(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, DONE, seen)
    await _call(chat.ws_chat, user_id="u1", text="one")
    await _call(chat.ws_chat, user_id="u2", text="two")
    assert [(s["conversation_id"], s["text"]) for s in seen] == [
        ("nova_chat_u1", "one"), ("nova_chat_u2", "two")]


async def test_only_the_text_and_the_users_context_go_to_the_agent(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, DONE, seen)
    await _call(chat.ws_chat, user_id="u1", text=" hello ")
    assert seen[0]["text"] == "hello" and seen[0]["extra"] == {}
    assert seen[0]["context"].user_id == "u1" and seen[0]["agent_id"] == "conversation.nova"


async def test_a_connection_with_no_user_is_refused_without_an_agent_call(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, DONE, seen)
    conn = await _call(chat.ws_chat, user_id=None, text="hi")
    assert conn.results == [(4, {"ok": False, "error": "Chat needs a signed in user."})] and seen == []


# ── limits ──────────────────────────────────────────────────────────────────

def test_the_length_limit_is_declared_and_enforced_by_the_schema(chat):
    import pathlib
    assert chat.CHAT_MAX_CHARS == 1000
    assert "vol.Length(min=1, max=CHAT_MAX_CHARS)" in pathlib.Path(chat.__file__).read_text()


async def test_blank_text_is_refused_without_calling_the_agent(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, DONE, seen)
    conn = await _call(chat.ws_chat, text="   ")
    assert conn.results == [(4, {"ok": False, "error": "Type a message first."})] and seen == []


async def test_the_twenty_first_message_in_a_minute_is_refused(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, DONE, seen)
    for i in range(20):
        conn = await _call(chat.ws_chat, text=f"m{i}")
        assert conn.results[0][1]["ok"] is True
    conn = await _call(chat.ws_chat, text="one too many")
    assert conn.results == [(4, {"ok": False, "code": "rate_limited",
                                 "error": "You're sending too fast, wait a moment"})]
    assert len(seen) == 20            # no agent call for the refused one


async def test_the_limit_is_per_user(chat, load, monkeypatch):
    seen = []
    _install_conversation(monkeypatch, load, DONE, seen)
    for i in range(20):
        await _call(chat.ws_chat, user_id="u1", text=f"m{i}")
    conn = await _call(chat.ws_chat, user_id="u2", text="mine")
    assert conn.results[0][1]["ok"] is True


def test_the_window_slides(chat):
    for i in range(20):
        assert chat._take_slot("u1", now=100.0 + i) is True
        chat._release("u1")
    assert chat._take_slot("u1", now=130.0) is False
    chat._release("u1")
    assert chat._take_slot("u1", now=161.0) is True      # the first sends are over a minute old


async def test_only_one_message_per_user_in_flight(chat, load, monkeypatch):
    seen = []
    gate = asyncio.Event()
    _install_conversation(monkeypatch, load, DONE, seen, gate=gate)
    first = asyncio.ensure_future(_call(chat.ws_chat, user_id="u1", text="slow"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    second = await _call(chat.ws_chat, user_id="u1", text="while waiting")
    assert second.results == [(4, {"ok": False, "code": "rate_limited",
                                   "error": "You're sending too fast, wait a moment"})]
    other = asyncio.ensure_future(_call(chat.ws_chat, user_id="u2", text="someone else"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [s["text"] for s in seen] == ["slow", "someone else"]
    gate.set()
    assert (await first).results[0][1]["ok"] is True
    assert (await other).results[0][1]["ok"] is True
    # The slot is free again once the reply is back.
    again = await _call(chat.ws_chat, user_id="u1", text="next")
    assert again.results[0][1]["ok"] is True


# ── never raises ────────────────────────────────────────────────────────────

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


async def test_chat_failure_is_a_safe_error_result_and_frees_the_slot(chat, load, monkeypatch):
    _install_conversation(monkeypatch, load, RuntimeError("token=abc"), [])
    conn = await _call(chat.ws_chat, text="hello")
    assert conn.errors == []
    assert conn.results == [(4, {"ok": False,
                                 "error": "RuntimeError (details are in the Home Assistant log)"})]
    assert "u1" not in chat._IN_FLIGHT


# ── NEW CHAT ────────────────────────────────────────────────────────────────

async def test_new_chat_clears_only_the_callers_thread(chat, load, monkeypatch):
    cleared = []
    deleted = []
    fake = types.ModuleType("jc.conversation")
    fake.clear_chat_thread = lambda cid: cleared.append(cid)
    monkeypatch.setitem(sys.modules, "jc.conversation", fake)
    monkeypatch.setattr(sys.modules["jc"], "conversation", fake, raising=False)
    db = load("database")
    monkeypatch.setattr(db, "delete_conversation", lambda cid: deleted.append(cid) or 3)

    class Hass(FakeHass):
        async def async_add_executor_job(self, func, *args):
            return func(*args)
    conn = await _call(chat.ws_chat_new, user_id="u1", hass=Hass())
    assert conn.results == [(4, {"ok": True, "removed": 3})]
    assert cleared == ["nova_chat_u1"] and deleted == ["nova_chat_u1"]


async def test_new_chat_needs_a_user(chat):
    conn = await _call(chat.ws_chat_new, user_id=None)
    assert conn.results == [(4, {"ok": False, "error": "Chat needs a signed in user."})]


async def test_new_chat_never_raises(chat, load, monkeypatch):
    fake = types.ModuleType("jc.conversation")

    def boom(cid):
        raise RuntimeError("token=abc")
    fake.clear_chat_thread = boom
    monkeypatch.setitem(sys.modules, "jc.conversation", fake)
    monkeypatch.setattr(sys.modules["jc"], "conversation", fake, raising=False)
    conn = await _call(chat.ws_chat_new, user_id="u1")
    assert conn.errors == []
    assert conn.results == [(4, {"ok": False,
                                 "error": "RuntimeError (details are in the Home Assistant log)"})]
