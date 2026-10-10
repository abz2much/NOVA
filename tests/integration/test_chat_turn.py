"""PHACC-tier proof for a Chat tab turn (8.31.0), through the real conversation
stack. A Chat turn has the conversation id nova_chat_<user id>. It skips the
relevance gate and the dedup, is never spoken, finds the person from the logged
in user, falls back to household facts only when there is none, and can be used
only by its own user. Run in the integration venv like the other files here.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry

DOMAIN = "nova"
# The one text the relevance gate rejects (see test_relevance_gate_persistence).
AMBIENT_TEXT = "well, actually"


def _isolate_conversations_db(monkeypatch, tmp_path):
    from custom_components.nova import database
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "conversations.db")
    return database


async def _setup_nova(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id("conversation", DOMAIN, entry.entry_id)
    assert entity_id is not None
    return entity_id


async def _converse(hass, entity_id, text, conversation_id, user_id=None):
    from homeassistant.components import conversation as conversation_component
    return await conversation_component.async_converse(
        hass, text, conversation_id, Context(user_id=user_id), agent_id=entity_id)


def _speech(result) -> str:
    return result.response.speech.get("plain", {}).get("speech", "")


def _engine(reply="Right away."):
    """The local engine declines and the agent answers, so no provider is called."""
    seen = []

    async def run_agent(*a, **k):
        from custom_components.nova import policy
        seen.append(policy._chat_user())
        return reply
    return seen, patch("custom_components.nova.local_engine.try_local",
                       new=AsyncMock(return_value=SimpleNamespace(handled=False, text=""))), \
        patch("custom_components.nova.agent.run_agent", new=run_agent)


async def test_a_chat_turn_skips_the_relevance_gate(hass, tmp_path, monkeypatch):
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    seen, local, agent = _engine()
    with local, agent:
        ambient = await _converse(hass, entity_id, AMBIENT_TEXT, "some-voice-thread")
        chat = await _converse(hass, entity_id, AMBIENT_TEXT, "nova_chat_u1", user_id="u1")
    assert _speech(ambient) == ""                       # a voice thread still drops it
    assert _speech(chat) == "Right away."               # a chat turn is always answered
    assert database.get_recent_messages(hours=1, device_id="some-voice-thread") == []
    assert any(r["role"] == "user" for r in database.get_recent_messages(
        hours=1, device_id="nova_chat_u1"))


async def test_a_chat_turn_skips_the_dedup(hass, tmp_path, monkeypatch):
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    seen, local, agent = _engine()
    with local, agent:
        first = await _converse(hass, entity_id, "turn the lamp on", "nova_chat_u1", user_id="u1")
        second = await _converse(hass, entity_id, "turn the lamp on", "nova_chat_u1", user_id="u1")
    assert _speech(first) == _speech(second) == "Right away."
    users = [r for r in database.get_recent_messages(hours=1, device_id="nova_chat_u1")
             if r["role"] == "user"]
    assert len(users) == 2


async def test_the_gate_knows_it_is_a_chat_turn_while_tools_run(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    from custom_components.nova import policy
    seen, local, agent = _engine()
    with local, agent:
        await _converse(hass, entity_id, "unlock the door", "nova_chat_u1", user_id="u1")
        await _converse(hass, entity_id, "Nova, unlock the door", "plain-assist-thread", user_id="u1")
    assert seen == ["u1", None]               # only the chat turn is marked, with its user
    assert policy._chat_user() is None         # and the mark is gone afterwards


async def test_a_chat_turn_is_never_spoken(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    seen, local, agent = _engine("Is there anything else I can do for you?")
    with local, agent, patch("custom_components.nova.conversation.async_announce",
                             new=AsyncMock()) as announce:
        result = await _converse(hass, entity_id, "good evening", "nova_chat_u1", user_id="u1")
    announce.assert_not_called()
    assert _speech(result) == "Is there anything else I can do for you?"
    assert not getattr(result, "continue_conversation", False)


async def test_the_person_comes_from_the_logged_in_user(hass, tmp_path, monkeypatch):
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    hass.states.async_set("person.abi", "home", {"user_id": "u1", "friendly_name": "Abi"})
    hass.states.async_set("person.sam", "home", {"user_id": "u2", "friendly_name": "Sam"})
    seen, local, agent = _engine()
    with local, agent:
        await _converse(hass, entity_id, "hello there", "nova_chat_u1", user_id="u1")
        await _converse(hass, entity_id, "hello there", "nova_chat_u2", user_id="u2")
    import sqlite3
    with sqlite3.connect(str(database._db_path())) as conn:
        rows = dict(conn.execute(
            "SELECT device_id, subject FROM conversations WHERE role = 'user'").fetchall())
    assert rows == {"nova_chat_u1": "abi", "nova_chat_u2": "sam"}


async def test_with_no_matching_person_chat_has_household_facts_only(hass, tmp_path, monkeypatch):
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    # One person is home, but that person's user is not the one typing.
    hass.states.async_set("person.sam", "home", {"user_id": "u2", "friendly_name": "Sam"})
    seen, local, agent = _engine()
    with local, agent:
        result = await _converse(hass, entity_id, "hello there", "nova_chat_u9", user_id="u9")
    assert _speech(result) == "Right away."
    import sqlite3
    with sqlite3.connect(str(database._db_path())) as conn:
        rows = conn.execute(
            "SELECT subject FROM conversations WHERE device_id = 'nova_chat_u9'").fetchall()
    assert rows and all(r[0] is None for r in rows)    # no personal memory attributed


async def test_another_users_thread_cannot_be_read_or_written(hass, tmp_path, monkeypatch):
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    seen, local, agent = _engine()
    with local, agent:
        mine = await _converse(hass, entity_id, "private note", "nova_chat_u1", user_id="u1")
        theirs = await _converse(hass, entity_id, "read it back", "nova_chat_u1", user_id="u2")
        nobody = await _converse(hass, entity_id, "read it back", "nova_chat_u1", user_id=None)
    assert _speech(mine) == "Right away."
    assert "belongs to someone else" in _speech(theirs)
    assert "belongs to someone else" in _speech(nobody)
    rows = database.get_recent_messages(hours=1, device_id="nova_chat_u1")
    assert [r["content"] for r in rows if r["role"] == "user"] == ["private note"]
    assert seen == ["u1"]


async def test_new_chat_deletes_only_that_users_rows(hass, tmp_path, monkeypatch):
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    seen, local, agent = _engine()
    with local, agent:
        await _converse(hass, entity_id, "one", "nova_chat_u1", user_id="u1")
        await _converse(hass, entity_id, "two", "nova_chat_u2", user_id="u2")
    from custom_components.nova import conversation
    conversation.clear_chat_thread("nova_chat_u1")
    removed = database.delete_conversation("nova_chat_u1")
    assert removed >= 1
    assert database.get_recent_messages(hours=1, device_id="nova_chat_u1") == []
    assert database.get_recent_messages(hours=1, device_id="nova_chat_u2") != []
