"""The arrival briefing used to tell the LLM two things that collided:
"Begin with 'Good afternoon, sir.'" and, separately, "Alex just arrived
home" — producing redundant lines like "Good afternoon, sir. Alex has just
arrived home." when the person being addressed (via their own honorific,
see honorific.py) IS Alex. It also always included open doors/windows in
the prompt context, even though the arrival trigger IS a door opening —
stating the obvious ("the front door is open") on the very briefing that
door-open caused.

These tests call the real `_trigger_briefing("arrival", ...)` and inspect
what actually reached the LLM (captured via FakeProvider.last_messages),
stubbing only the network/IO seams (LLM provider, TTS, phone push) that
aren't part of the prompt-building logic under test.
"""
import sys
import types

import pytest


@pytest.fixture
def pb(load, monkeypatch, fake_hass):
    honorific_mod = load("honorific")
    mod = load("proactive_briefing")
    mod._STATE.hass = fake_hass
    mod._STATE.config = {}
    mod._STATE.running = True
    mod._STATE.last_briefing_time = 0.0

    # Force the honorific to belong to the one person who's home (Alex) —
    # simulates honorific.effective_honorific() resolving to his own
    # configured honorific because he's home alone.
    monkeypatch.setattr(honorific_mod, "effective_honorific", lambda hass: "sir")

    # conftest globally stubs build_system_prompt to a fixed string (it's a
    # shared stub used by unrelated reasoning_loop tests); here we need to
    # see the actual `task` text this module builds, so echo it back instead.
    monkeypatch.setattr(
        sys.modules["jc.directive_helper"], "build_system_prompt",
        lambda hass, honorific, task: task,
    )

    # Network/IO seams unrelated to the prompt-building logic under test.
    fake_provider = types.SimpleNamespace(
        chat=lambda messages, *a, **k: ({"text": "stubbed reply"}, messages)[0]
    )
    captured = {}

    def _fake_create_tier_provider(config, tier):
        return fake_provider

    llm_stub = types.ModuleType("jc.llm_provider")
    llm_stub.create_provider = lambda *a, **k: fake_provider
    llm_stub.create_tier_provider = _fake_create_tier_provider
    monkeypatch.setitem(sys.modules, "jc.llm_provider", llm_stub)

    def _capture_chat(messages, *a, **k):
        captured["messages"] = messages
        return {"text": "stubbed reply"}
    fake_provider.chat = _capture_chat

    mod._captured = captured
    return mod


def test_arrival_briefing_does_not_restate_name_when_addressing_directly(pb, fake_hass):
    import asyncio
    asyncio.run(pb._trigger_briefing("arrival", person_name="Alex"))
    system_msg = pb._captured["messages"][0]["content"]
    user_msg = pb._captured["messages"][1]["content"]
    assert "Welcome home, sir." in system_msg
    assert "Alex just arrived home" not in system_msg
    assert "Alex just arrived home" not in user_msg


def test_arrival_briefing_drops_open_door_from_context(pb, fake_hass):
    import asyncio
    fake_hass.states.set("binary_sensor.front_door", "on",
                          friendly_name="Front Door Contact Sensor", device_class="door")
    fake_hass.states.set("lock.garage", "unlocked", friendly_name="Garage")
    asyncio.run(pb._trigger_briefing("arrival", person_name="Alex"))
    user_msg = pb._captured["messages"][1]["content"]
    assert "Front Door Contact Sensor is open" not in user_msg
    assert "Garage is unlocked" in user_msg
