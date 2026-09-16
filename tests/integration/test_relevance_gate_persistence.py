"""PHACC-tier behavioral proof for the Phase 1 relevance-gate fix (real
conversation stack — see tests/unit/test_conversation_relevance_gate_persistence.py
for the source-level ordering/formula guards, which are the primary,
always-runnable coverage; this file is the real end-to-end companion for
environments where pytest-homeassistant-custom-component is installed, per
tests/integration/conftest.py — NOT part of the normal local dev loop, run
in a separate `.venv-integration` per test_wiring_smoke.py's docstring.

Presence-gate and dedup suppression (requirement 6) are deliberately NOT
re-verified here — Phase 1 does not touch that code at all, and the unit
test file already proves, byte-for-byte, that those blocks are untouched and
still precede the relevance decision. Re-deriving their real-world trigger
conditions (device/area registry state, dedup timing) here would add risk
without adding proof value for what Phase 1 actually changed.
"""
from unittest.mock import AsyncMock, patch

from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .test_wiring_smoke import _make_entry

DOMAIN = "nova"

# An utterance with no command verb, question word, device keyword, or the
# name "Nova", and <=3 words all of which are pure discourse filler — the
# one case conversation.py's _is_addressed_to_nova() heuristic reliably
# rejects (its default is to PASS ambiguous input, so this is deliberately
# chosen, not an arbitrary "sounds ambient" guess).
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


async def test_irrelevant_ambient_speech_writes_to_no_reasoning_memory_store(
    hass, tmp_path, monkeypatch,
):
    """Requirements 1 & 7: rejected ambient speech must not update
    _last_seen, must not call _maybe_seed_history() or its backing loader
    (memory_thread.load_recent), must not touch the in-memory conversation
    history (self._history()), must not reach the conversation DB or
    semantic memory, and must not reach the local engine or the LLM agent."""
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    from custom_components.nova.conversation import NovaAgent

    with patch.object(NovaAgent, "_maybe_seed_history") as mock_seed, \
         patch.object(NovaAgent, "_history") as mock_history, \
         patch("custom_components.nova.memory_thread.load_recent") as mock_load_recent, \
         patch("custom_components.nova.memory.store_memory") as mock_store, \
         patch("custom_components.nova.memory.get_conversation_context") as mock_recall, \
         patch("custom_components.nova.local_engine.try_local") as mock_local, \
         patch("custom_components.nova.agent.run_agent") as mock_agent:
        result = await _converse(
            hass, entity_id, AMBIENT_TEXT, conversation_id="phase1-test-1",
        )

    assert _speech(result) == ""

    # _last_seen[cid] is only ever written inside _maybe_seed_history() — so
    # proving that method was never called is direct proof _last_seen was
    # not touched for this turn.
    mock_seed.assert_not_called()
    # self._history(cid) is the only thing that creates/returns the
    # in-memory history list this turn would have been appended to — never
    # called means the in-memory history was structurally untouched.
    mock_history.assert_not_called()
    mock_load_recent.assert_not_called()
    mock_store.assert_not_called()
    mock_recall.assert_not_called()
    mock_local.assert_not_called()
    mock_agent.assert_not_called()
    assert database.get_recent_messages(hours=1, device_id="phase1-test-1") == []


async def test_pending_offer_lookup_failure_does_not_break_an_addressed_command(
    hass, tmp_path, monkeypatch,
):
    """The pending-offer capture is fail-open: if cognitive_core can't be
    imported or get_pending_offer() raises, an ordinary addressed command
    must still be treated as relevant (via is_addressed) and still persist
    and route normally, not crash or get silently dropped."""
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch(
        "custom_components.nova.cognitive_core.get_pending_offer",
        side_effect=RuntimeError("boom"),
    ):
        result = await _converse(
            hass, entity_id, "Nova, what time is it?",
            conversation_id="phase1-test-6",
        )

    assert _speech(result) != ""
    rows = database.get_recent_messages(hours=1, device_id="phase1-test-6")
    assert any(
        r["role"] == "user" and "what time is it" in r["content"].lower()
        for r in rows
    )


async def test_ordinary_accepted_command_still_writes_normally(
    hass, tmp_path, monkeypatch,
):
    """Requirement 2: a normal command must still persist to the
    conversation DB exactly as it does today."""
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    await _converse(
        hass, entity_id, "Nova, what time is it?",
        conversation_id="phase1-test-2",
    )

    rows = database.get_recent_messages(hours=1, device_id="phase1-test-2")
    assert any(
        r["role"] == "user" and "what time is it" in r["content"].lower()
        for r in rows
    )


async def test_pending_offer_acceptance_stays_relevant_and_persists(
    hass, tmp_path, monkeypatch,
):
    """Requirement 3: replying to a pending offer must still be persisted,
    end to end through the real conversation stack."""
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch(
        "custom_components.nova.cognitive_core.get_pending_offer",
        return_value={"action": "turn_off_kitchen_lights"},
    ), patch(
        "custom_components.nova.cognitive_core.accept_pending_offer",
        new=AsyncMock(return_value={"ok": True, "approvals": 1}),
    ):
        result = await _converse(
            hass, entity_id, "yes", conversation_id="phase1-test-3",
        )

    assert "done" in _speech(result).lower()
    rows = database.get_recent_messages(hours=1, device_id="phase1-test-3")
    assert any(
        r["role"] == "user" and r["content"].strip().lower() == "yes"
        for r in rows
    )


async def test_pending_offer_rejection_stays_relevant_and_persists(
    hass, tmp_path, monkeypatch,
):
    """Requirement 4: declining a pending offer must also still be
    persisted."""
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    with patch(
        "custom_components.nova.cognitive_core.get_pending_offer",
        return_value={"action": "turn_off_kitchen_lights"},
    ), patch(
        "custom_components.nova.cognitive_core.decline_pending_offer",
    ):
        result = await _converse(
            hass, entity_id, "no", conversation_id="phase1-test-4",
        )

    assert "understood" in _speech(result).lower()
    rows = database.get_recent_messages(hours=1, device_id="phase1-test-4")
    assert any(
        r["role"] == "user" and r["content"].strip().lower() == "no"
        for r in rows
    )


async def test_disabling_relevance_gate_preserves_persistence(
    hass, tmp_path, monkeypatch,
):
    """Requirement 5: with the relevance gate forced off, input that would
    otherwise be rejected still persists — the `not gate_enabled` arm of the
    relevance formula, exercised end to end."""
    database = _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)

    from custom_components.nova.conversation import NovaAgent
    original_opt = NovaAgent._opt

    def _opt_gate_off(self, key, default=None):
        if key == "relevance_gate":
            return False
        return original_opt(self, key, default)

    monkeypatch.setattr(NovaAgent, "_opt", _opt_gate_off)

    await _converse(
        hass, entity_id, AMBIENT_TEXT, conversation_id="phase1-test-5",
    )

    rows = database.get_recent_messages(hours=1, device_id="phase1-test-5")
    assert any(r["role"] == "user" for r in rows)
