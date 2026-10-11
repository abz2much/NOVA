"""Nova Voice ID 8.37.0 on a real Home Assistant: voice decides who is
speaking, only with the switch on.

Through the real conversation stack: the STT entity sets the result, the turn
reads it once, identity uses it, the VOICE line shows the outcome, and the
agent gets the fenced hint and the identity. With the switch off the turn is
exactly 8.36.0's. A typed Chat turn never uses voice.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from homeassistant.core import Context

from .test_chat_turn import _setup_nova, _speech
from .test_voice_id_phase2b import (  # noqa: F401 - the fixture is used by name
    DEVICE, TEXT, VoiceApp, _log, _set_app, _voice_turn, isolated,
)

BOB = {"v": 1, "status": "known", "person": "person.bob", "score": 0.84,
       "speech_s": 2.6, "sample_id": "s1", "model": "m", "ms": 70}


class _Agent:
    def __init__(self):
        self.calls = []
        self.enrollment = 0

    def patches(self):
        async def local(*a, **k):
            return SimpleNamespace(handled=False, text="")

        async def agent(*a, **k):
            self.calls.append(k)
            return "From the agent."

        def enroll(*a, **k):
            self.enrollment += 1
        return [patch("custom_components.nova.local_engine.try_local", new=local),
                patch("custom_components.nova.agent.run_agent", new=agent),
                patch("custom_components.nova.voice_recognition.maybe_fire_enrollment",
                      new=enroll)]


def _people(hass):
    hass.states.async_set("person.alice", "home", {"friendly_name": "Alice"})
    hass.states.async_set("person.bob", "home", {"friendly_name": "Bob Jones"})


def _switch(hass, on):
    from custom_components.nova import nova_config
    entry = hass.config_entries.async_entries("nova")[0]
    entry.runtime_data.runtime_config["voice_id_identity"] = on
    nova_config.set("voice_id_identity", on)


@pytest.fixture
def no_switch(hass):
    yield
    from custom_components.nova import identity, nova_config
    nova_config.delete("voice_id_identity")
    identity.clear_voice_carry()


async def _turn(hass, monkeypatch, conv, block, on):
    _people(hass)
    _switch(hass, on)
    lines = _log(monkeypatch)
    spy = _Agent()
    async with VoiceApp(blocks=[block]) as app:
        await _set_app(hass, app.uri)
        ps = spy.patches()
        for p in ps:
            p.start()
        try:
            _heard, result = await _voice_turn(hass, conv, TEXT, DEVICE)
        finally:
            for p in ps:
                p.stop()
    return result, spy, [m for c, m in lines if c == "VOICE"]


async def test_switch_on_voice_decides_and_reaches_the_agent(
        hass, isolated, no_switch, monkeypatch, socket_enabled):
    conv = await _setup_nova(hass)
    result, spy, voice = await _turn(hass, monkeypatch, conv, BOB, on=True)
    assert _speech(result) == "From the agent."
    # Other signals (a face another test left) may join; voice must be there.
    assert len(voice) == 1 and " → Bob Jones (" in voice[0]
    assert "voice" in voice[0].split(" → ", 1)[1]
    assert TEXT not in voice[0]
    call = spy.calls[0]
    assert call["identification"].person == "Bob Jones"
    assert call["speaker_hint"] == "Speaker (by voice, a hint, not proof of identity): Bob."
    # the hint is never in the user's words or the history
    assert all("Speaker" not in m["content"] for m in call["messages"])
    assert call["user_input"].text == TEXT
    assert call["user_input"].context.user_id is None      # never set from a voice
    assert spy.enrollment == 0                              # a Voice ID turn never fires it


async def test_switch_off_is_exactly_8_36(hass, isolated, no_switch, monkeypatch, socket_enabled):
    conv = await _setup_nova(hass)
    result, spy, voice = await _turn(hass, monkeypatch, conv, BOB, on=False)
    assert _speech(result) == "From the agent."
    assert voice == ["status=known person=person.bob score=0.84 second=- margin=- "
                     "speech_s=2.60 ms=70 device=dev-kitchen"]
    call = spy.calls[0]
    assert call["speaker_hint"] == "" and call["identification"] is None
    from custom_components.nova import identity
    assert identity._VOICE_CARRY == {}


async def test_a_typed_chat_turn_never_uses_voice(hass, isolated, no_switch, monkeypatch):
    from homeassistant.components import conversation as conversation_component
    from custom_components.nova import policy, voice_id
    conv = await _setup_nova(hass)
    _people(hass)
    _switch(hass, True)
    voice_id.set_turn_voice(voice_id.parse_block(BOB))      # as if a voice result lingered
    spy = _Agent()
    ps = spy.patches()
    for p in ps:
        p.start()
    try:
        user = await hass.auth.async_create_user("Chat user")
        await conversation_component.async_converse(
            hass, "hello", policy.chat_conversation_id(user.id), Context(user_id=user.id),
            agent_id=conv)
    finally:
        for p in ps:
            p.stop()
        voice_id.VOICE_RESULT.set(None)
    call = spy.calls[0]
    assert call["speaker_hint"] == "" and call["identification"] is None


async def test_a_not_sure_turn_gets_the_not_sure_hint(
        hass, isolated, no_switch, monkeypatch, socket_enabled):
    conv = await _setup_nova(hass)
    unsure = {"v": 1, "status": "not_sure", "score": 0.82, "speech_s": 1.0,
              "sample_id": "s2", "model": "m", "ms": 60}
    _result, spy, voice = await _turn(hass, monkeypatch, conv, unsure, on=True)
    call = spy.calls[0]
    assert call["speaker_hint"] == "Speaker: not sure."
    assert call["user_input"].context.user_id is None
    assert len(voice) == 1 and voice[0].startswith("status=not_sure")
