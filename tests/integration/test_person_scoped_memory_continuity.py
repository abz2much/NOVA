"""PHACC-tier behavioral proof for Phase 2 — person-scoped episodic
continuity and coherent exchange retrieval, against a real conversation
stack. Companion to tests/unit/test_conversation_person_scoped_memory.py
(the few structural guards that are hard to observe behaviorally) and
tests/unit/test_memory_person_scoped.py (retrieval/pairing mechanics in
isolation).

Real conversation turns are driven through the actual
NovaAgent._handle_message_impl / async_process path; identity.resolve() is
mocked per-turn to simulate a resolved/unresolved person without needing
real presence/face-recognition signals. `agent.run_agent` is mocked to
capture the exact `persona` string Nova would have sent the LLM — the most
direct way to observe what context a turn actually assembled, without
depending on the LLM's own response content.
"""
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry

DOMAIN = "nova"


def _isolate_memory_stores(monkeypatch, tmp_path):
    from custom_components.nova import database, memory
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "conversations.db")
    monkeypatch.setattr(memory, "DB_PATH", str(tmp_path / "nova.db"))
    monkeypatch.setattr(memory, "MEMORY_DIR", str(tmp_path / "nova_memory"))
    monkeypatch.setattr(memory, "_chromadb_available", False)
    monkeypatch.setattr(memory, "_collection", None)
    monkeypatch.setattr(memory, "_fts_available", False)
    return database, memory


async def _setup_nova(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "conversation", {})
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "conversation", DOMAIN, entry.entry_id)
    assert entity_id is not None
    return entity_id


async def _converse(hass, entity_id, text, conversation_id):
    from homeassistant.components import conversation as conversation_component
    return await conversation_component.async_converse(
        hass, text, conversation_id, Context(), agent_id=entity_id,
    )


def _speech(result) -> str:
    return result.response.speech.get("plain", {}).get("speech", "")


def _known_identity(name: str):
    class _Ident:
        person = name
        known = True
    return _Ident()


def _unknown_identity():
    class _Ident:
        person = "unknown"
        known = False
    return _Ident()


async def _ask_and_capture_persona(hass, entity_id, text, conversation_id):
    """Send a turn that must escalate to the LLM agent, and return the exact
    `persona` string it was called with — the most direct observable proxy
    for "what context did this turn actually assemble"."""
    mock_run_agent = AsyncMock(return_value="a generic reply")
    with patch("custom_components.nova.agent.run_agent", new=mock_run_agent), \
         patch("custom_components.nova.local_engine.try_local", new=AsyncMock(return_value=None)), \
         patch("custom_components.nova.connectivity.allow_request", return_value=True):
        await _converse(hass, entity_id, text, conversation_id=conversation_id)
    assert mock_run_agent.await_args is not None, "run_agent was never called"
    return mock_run_agent.await_args.kwargs.get("persona", "")


async def test_resolved_person_continuity_across_conversation_id_rotation(
    hass, tmp_path, monkeypatch,
):
    """The core Phase 2 goal: a resolved person's earlier exchange must
    still be recallable after Home Assistant rotates the conversation_id."""
    _isolate_memory_stores(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch("custom_components.nova.identity.resolve", return_value=_known_identity("alice")):
        await _converse(hass, entity_id, "the wifi password is hunter2",
                         conversation_id="conv-A")

        persona = await _ask_and_capture_persona(
            hass, entity_id, "what did I just tell you the wifi password was?",
            conversation_id="conv-B",  # a DIFFERENT conversation_id, same person
        )

    assert "hunter2" in persona


async def test_different_resolved_person_cannot_retrieve_the_first_persons_history(
    hass, tmp_path, monkeypatch,
):
    _isolate_memory_stores(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch("custom_components.nova.identity.resolve", return_value=_known_identity("alice")):
        await _converse(hass, entity_id, "the wifi password is hunter2",
                         conversation_id="conv-A")

    with patch("custom_components.nova.identity.resolve", return_value=_known_identity("bob")):
        persona = await _ask_and_capture_persona(
            hass, entity_id, "what's the wifi password?", conversation_id="conv-B",
        )

    assert "hunter2" not in persona


async def test_unresolved_identity_gets_no_person_scoped_fallback(
    hass, tmp_path, monkeypatch,
):
    _isolate_memory_stores(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch("custom_components.nova.identity.resolve", return_value=_known_identity("alice")):
        await _converse(hass, entity_id, "the wifi password is hunter2",
                         conversation_id="conv-A")

    with patch("custom_components.nova.identity.resolve", return_value=_unknown_identity()):
        persona = await _ask_and_capture_persona(
            hass, entity_id, "what's the wifi password?", conversation_id="conv-B",
        )

    assert "hunter2" not in persona


async def test_current_user_message_not_retrieved_by_its_own_turns_search(
    hass, tmp_path, monkeypatch,
):
    """A brand-new conversation's very first message must not become its
    own top search result — if the self-retrieval bug were present, the
    persona would contain a "## Relevant past conversations" section
    quoting the message that was just said, since nothing else could
    possibly be in an empty, fresh conversation."""
    _isolate_memory_stores(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    persona = await _ask_and_capture_persona(
        hass, entity_id, "please remember the garage code is 4471",
        conversation_id="conv-brand-new",
    )

    assert "## Relevant past conversations" not in persona


async def test_identity_resolution_failure_keeps_the_request_working(
    hass, tmp_path, monkeypatch,
):
    database, memory = _isolate_memory_stores(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch("custom_components.nova.identity.resolve", side_effect=RuntimeError("boom")):
        result = await _converse(hass, entity_id, "Nova, what time is it?",
                                  conversation_id="conv-identity-fail")

    assert _speech(result) != ""
    rows = database.get_recent_messages(hours=1, device_id="conv-identity-fail")
    assert any(r["role"] == "user" for r in rows)
    assert all(r.get("subject") is None for r in rows)


async def test_unresolved_identity_never_writes_primary_as_episodic_subject(
    hass, tmp_path, monkeypatch,
):
    database, memory = _isolate_memory_stores(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch("custom_components.nova.identity.resolve", return_value=_unknown_identity()):
        await _converse(hass, entity_id, "Nova, what time is it?",
                         conversation_id="conv-unresolved")

    rows = database.get_recent_messages(hours=1, device_id="conv-unresolved")
    assert any(r["role"] == "user" for r in rows)
    for r in rows:
        assert r.get("subject") != "primary"
        assert r.get("subject") is None


async def test_coherent_pairing_end_to_end(hass, tmp_path, monkeypatch):
    """A later recall of an earlier exchange returns both the question and
    the answer together, not just one half."""
    _isolate_memory_stores(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch("custom_components.nova.identity.resolve", return_value=_known_identity("alice")):
        mock_run_agent_1 = AsyncMock(return_value="It's 4471.")
        with patch("custom_components.nova.agent.run_agent", new=mock_run_agent_1), \
             patch("custom_components.nova.local_engine.try_local", new=AsyncMock(return_value=None)), \
             patch("custom_components.nova.connectivity.allow_request", return_value=True):
            await _converse(hass, entity_id, "what's the garage code again",
                             conversation_id="conv-A")

        persona = await _ask_and_capture_persona(
            hass, entity_id, "remind me what you said about the garage code",
            conversation_id="conv-B",
        )

    assert "what's the garage code again" in persona
    assert "4471" in persona


async def test_migration_runs_cleanly_through_real_setup_and_reload(hass, tmp_path, monkeypatch):
    """The subject-column migration must not break normal setup/reload —
    mirrors test_wiring_smoke.py::test_unload_then_resetup_entry."""
    database, memory = _isolate_memory_stores(monkeypatch, tmp_path)
    assert await async_setup_component(hass, "homeassistant", {})
    entry = _make_entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert database.health()["ok"] is True
