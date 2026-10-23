"""What a turn writes the moment it arrives (PHACC), through the real
conversation stack.

The Nova Log gets one CONV line with the first 60 characters of the request
and the device it came from. Home Assistant's own log gets only the length of
the request and the device, never the words. Run in the integration venv.
"""
import logging

from homeassistant.components import conversation as conversation_component
from homeassistant.core import Context

from .test_chat_turn import _engine, _isolate_conversations_db, _setup_nova, _speech

LONG_TEXT = ("turn on the kitchen lamp and then close the blinds in the living room "
             "and put the porch light on")


def _record_log(monkeypatch):
    """Every Nova Log line as (category, message)."""
    from custom_components.nova import websocket, ws_log
    lines = []
    real = ws_log.nova_log

    def record(category, message):
        lines.append((category, message))
        real(category, message)

    monkeypatch.setattr(websocket, "nova_log", record)
    monkeypatch.setattr(ws_log, "nova_log", record)
    return lines


async def _speak(hass, entity_id, text, device_id=None):
    return await conversation_component.async_converse(
        hass, text, None, Context(), agent_id=entity_id, device_id=device_id)


async def test_the_nova_log_gets_the_first_sixty_characters_and_the_device(
        hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    lines = _record_log(monkeypatch)
    seen, local, agent = _engine()
    with local, agent:
        result = await _speak(hass, entity_id, LONG_TEXT, device_id="dev-kitchen")
    assert _speech(result) == "Right away."
    entry = [m for c, m in lines if c == "CONV" and m.startswith("ENTRY")]
    assert entry == [f"ENTRY text='{LONG_TEXT[:60]}' device=dev-kitchen"]
    assert len(LONG_TEXT[:60]) == 60 and LONG_TEXT[:60] != LONG_TEXT


async def test_a_turn_with_no_device_is_logged_as_none(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    lines = _record_log(monkeypatch)
    seen, local, agent = _engine()
    with local, agent:
        await _speak(hass, entity_id, "Nova, turn on the lamp")
    entry = [m for c, m in lines if c == "CONV" and m.startswith("ENTRY")]
    assert entry == ["ENTRY text='Nova, turn on the lamp' device=None"]


async def test_the_entry_line_is_the_first_thing_the_turn_logs(hass, tmp_path, monkeypatch):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    lines = _record_log(monkeypatch)
    seen, local, agent = _engine()
    with local, agent:
        await _speak(hass, entity_id, "Nova, turn on the lamp", device_id="dev-a")
    assert lines[0] == ("CONV", "ENTRY text='Nova, turn on the lamp' device=dev-a")


async def test_home_assistants_log_gets_only_the_length_and_the_device(
        hass, tmp_path, monkeypatch, caplog):
    _isolate_conversations_db(monkeypatch, tmp_path)
    entity_id = await _setup_nova(hass)
    seen, local, agent = _engine()
    text = "Nova, turn on the lamp"
    with local, agent, caplog.at_level(logging.DEBUG,
                                       logger="custom_components.nova.conversation"):
        await _speak(hass, entity_id, text, device_id="dev-a")
    entry = [r for r in caplog.records
             if r.getMessage().startswith("Nova async_process ENTRY")]
    assert [(r.levelno, r.getMessage()) for r in entry] == [
        (logging.DEBUG,
         f"Nova async_process ENTRY: {len(text)} chars, device_id='dev-a'")]
    assert all(text not in r.getMessage() for r in entry)
