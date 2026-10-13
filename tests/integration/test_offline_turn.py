"""PHACC-tier proof for a turn while the agent cannot be reached (8.49.0).

With the connection marked down, an exact command, a status question and
"repeat that" are answered locally through the real conversation stack,
and a request that needs interpretation gets an honest offline answer.
The agent is never called. Run in the integration venv like the other
files here.
"""
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import async_mock_service

from .test_chat_turn import _converse, _isolate_conversations_db, _setup_nova, _speech


def _offline():
    from custom_components.nova import connectivity
    connectivity.reset()
    connectivity.record_failure()
    connectivity.record_failure()
    assert not connectivity.allow_request()
    return connectivity


async def test_exact_requests_are_answered_offline(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    hass.states.async_set("light.porch", "off", {"friendly_name": "Porch"})
    hass.states.async_set("climate.hall", "heat",
                          {"friendly_name": "Hall", "current_temperature": 20})
    calls = async_mock_service(hass, "light", "turn_on")
    from custom_components.nova import spoken_history
    monkeypatch.setattr(spoken_history, "_last", {
        "id": 1, "text": "The back door is open.", "source": "alert",
        "speakers": [], "repeat_of_id": None})
    connectivity = _offline()
    agent = AsyncMock(return_value="agent")
    try:
        with patch("custom_components.nova.agent.run_agent", new=agent):
            temp = await _converse(hass, entity_id, "what's the temperature",
                                   "nova_chat_u1", user_id="u1")
            again = await _converse(hass, entity_id, "repeat that", "nova_chat_u1",
                                    user_id="u1")
            await _converse(hass, entity_id, "turn on the porch light", "nova_chat_u1",
                            user_id="u1")
            poem = await _converse(hass, entity_id, "write me a poem about the sea",
                                   "nova_chat_u1", user_id="u1")
    finally:
        connectivity.reset()
    agent.assert_not_called()
    assert "Hall 20°" in _speech(temp)
    assert _speech(again) == "The back door is open."
    assert [c.data.get("entity_id") for c in calls] in (["light.porch"], [["light.porch"]])
    assert "offline" in _speech(poem)
